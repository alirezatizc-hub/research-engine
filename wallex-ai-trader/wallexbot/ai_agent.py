"""عامل AI: دروازه ورود، مرور رژیم بازار، و تحلیل پس از معامله (reflection).

قواعد طراحی:
- خروجی باید JSON با نوع‌های دقیق باشد؛ هر خطا/خروجی نامعتبر => None (fail-closed).
- AI هرگز پارامتر ریسک را عوض نمی‌کند و فقط می‌تواند «سخت‌گیرتر» شود (رد ورود، حجم ≤۱، حالت ریسک).
- دو provider پشتیبانی می‌شود: Anthropic و هر سرویس سازگار با OpenAI (OpenAI/OpenRouter/پروکسی‌های ایران).
- هر فراخوانی در logs/ai_calls.jsonl با مدل، تأخیر، هش پرامپت و پاسخ ثبت می‌شود (قابل ممیزی برای داوران).
"""
from __future__ import annotations
import hashlib
import json
import logging
import math
import os
import time

import requests

log = logging.getLogger("wtb")

SYS_GATE = """You are the decision agent of a spot-only crypto trading bot on the Iranian exchange Wallex (TMN pairs).
A rule-based engine proposed a LONG entry. Judge it using the numeric features, market snapshot, recent trades and "lessons"
(your own past post-trade analyses). Round-trip cost is about 0.7%, so reject weak-edge setups, falling-knife dips,
thin/wide books, signals contradicting the market snapshot, and setups resembling past mistakes in the lessons.
Rules you must respect: spot only, no leverage, one entry per signal, max size multiplier 1.0.
Reply with ONLY compact JSON: {"approve": true|false, "confidence": 0..1, "size_mult": 0.5..1.0, "reason": "<=40 words"}"""

SYS_REVIEW = """You supervise a spot crypto trading bot. Given the market snapshot, open positions, recent trades and equity,
choose a risk mode: "normal", "defensive" (halve position sizes; choppy/falling market or losing streak),
or "pause" (no new entries; sharp market-wide selloff or repeated stop-outs).
Reply with ONLY JSON: {"risk_mode": "normal|defensive|pause", "note": "<=40 words"}"""

SYS_OPTIMIZE = """You are the research director of a spot-only crypto trading bot on the Iranian exchange Wallex (TMN pairs).
You get: (a) results of several parameter scenarios backtested on a TRAIN window and ranked on a VALIDATION window,
(b) the hard parameter bounds in "allowed_parameters", (c) the most frequent rejection reasons.
Diagnose why the scenarios lose money after fees (bad entries, stop-outs, too few trades, drawdown, overfitting),
then propose at most 4 NEW parameter sets that are inside the bounds and likely to survive validation.
Never propose a parameter outside allowed_parameters. Prefer robustness over the best train number.
Do not assume any knowledge of a hidden test window. Reply with ONLY compact JSON:
{"diagnosis": "<=60 words", "failure_modes": ["<=8 words"], "proposals": [{"name": "short", "params": {"param": value}, "rationale": "<=30 words"}], "recommend": "scenario name", "confidence": 0..1}"""

SYS_REFLECT = """You are the post-trade analyst of a trading bot. You receive its recent closed trades with entry features,
mistake tags computed by the bot, per-kind statistics and parameter changes already made. Identify concrete, evidence-based
patterns (not noise: with few trades say so). Reply with ONLY JSON:
{"lesson": "<=60 words, actionable, referencing features/tags", "confidence": 0..1}"""


def _json_obj(text):
    i = text.find("{")
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[i:])
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def _is_num(x, lo, hi):
    return (isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and lo <= x <= hi)


def validate_gate(o):
    if not isinstance(o, dict) or not isinstance(o.get("approve"), bool):
        return None
    if not _is_num(o.get("confidence"), 0, 1) or not _is_num(o.get("size_mult"), 0.5, 1.0):
        return None
    if not isinstance(o.get("reason"), str) or not o["reason"].strip():
        return None
    return {"approve": o["approve"], "confidence": float(o["confidence"]), "size_mult": float(o["size_mult"]),
            "reason": o["reason"][:300]}


def validate_review(o):
    if not isinstance(o, dict) or o.get("risk_mode") not in ("normal", "defensive", "pause"):
        return None
    note = o.get("note", "")
    if not isinstance(note, str):
        return None
    return {"risk_mode": o["risk_mode"], "note": note[:300]}


def validate_optimize(o):
    """اسکلت پاسخ تحلیل بهینه‌سازی؛ اعتبار عددی پارامترها در optimizer.sanitize_params انجام می‌شود."""
    if not isinstance(o, dict):
        return None
    diag = o.get("diagnosis")
    if not isinstance(diag, str) or not diag.strip():
        return None
    fms = o.get("failure_modes") or []
    if not isinstance(fms, list):
        fms = []
    props = o.get("proposals") or []
    if not isinstance(props, list):
        props = []
    clean = []
    for p in props[:4]:
        if not isinstance(p, dict) or not isinstance(p.get("params"), dict) or not p["params"]:
            continue
        clean.append({"name": str(p.get("name") or "ai-proposal")[:40],
                      "params": p["params"],
                      "rationale": str(p.get("rationale") or p.get("note") or "")[:240]})
    conf = o.get("confidence")
    return {"diagnosis": diag[:400], "failure_modes": [str(x)[:120] for x in fms[:6]],
            "proposals": clean, "recommend": str(o.get("recommend") or "")[:60] or None,
            "confidence": float(conf) if _is_num(conf, 0, 1) else 0.5}


def validate_reflect(o):
    if not isinstance(o, dict) or not isinstance(o.get("lesson"), str) or not o["lesson"].strip():
        return None
    if not _is_num(o.get("confidence"), 0, 1):
        return None
    return {"lesson": o["lesson"][:400], "confidence": float(o["confidence"])}


class AIAgent:
    def __init__(self, cfg, transport=None):
        self.cfg = cfg
        self.enabled = bool(cfg.ai_enabled and cfg.ai_key())
        self._transport = transport or self._http
        self._cache = {}
        self.last_error = None
        self.last_meta = {}
        if not self.enabled:
            log.warning("AI غیرفعال/بدون کلید => rules-only (امتیاز >= %s)", cfg.rules_min_score)

    # ------------------------------------------------------------ providers
    @property
    def provider(self):
        return (self.cfg.ai_provider or "anthropic").lower()

    def _endpoint(self):
        if self.provider == "openai":
            base = (self.cfg.ai_base_url or "https://api.openai.com/v1").rstrip("/")
            return base + "/chat/completions"
        base = (self.cfg.ai_base_url or "https://api.anthropic.com").rstrip("/")
        return base + "/v1/messages"

    def _http(self, system, payload):
        """فراخوانی واقعی provider؛ خروجی متن خام مدل."""
        user = json.dumps(payload, ensure_ascii=False, default=str)
        if self.provider == "openai":
            body = {"model": self.cfg.ai_model, "temperature": 0, "max_tokens": self.cfg.ai_max_tokens,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            r = requests.post(
                self._endpoint(),
                headers={"Authorization": f"Bearer {self.cfg.ai_key()}", "Content-Type": "application/json"},
                json=body,
                timeout=self.cfg.ai_timeout_s,
            )
            if r.status_code == 400 and "max_tokens" in (r.text or ""):
                # مدل‌های جدید OpenAI به‌جای max_tokens از max_completion_tokens استفاده می‌کنند.
                body.pop("max_tokens", None)
                body["max_completion_tokens"] = self.cfg.ai_max_tokens
                r = requests.post(
                    self._endpoint(),
                    headers={"Authorization": f"Bearer {self.cfg.ai_key()}", "Content-Type": "application/json"},
                    json=body, timeout=self.cfg.ai_timeout_s,
                )
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {(r.text or '')[:200]}")
            data = r.json()
            msg = (data.get("choices") or [{}])[0].get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            return content or ""
        r = requests.post(
            self._endpoint(),
            headers={"x-api-key": self.cfg.ai_key(), "anthropic-version": self.cfg.ai_version,
                     "content-type": "application/json"},
            json={"model": self.cfg.ai_model, "max_tokens": self.cfg.ai_max_tokens, "temperature": 0,
                  "system": system, "messages": [{"role": "user", "content": user}]},
            timeout=self.cfg.ai_timeout_s,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {(r.text or '')[:200]}")
        return "".join(b.get("text", "") for b in r.json().get("content", []) if isinstance(b, dict))

    # ------------------------------------------------------------ audit log
    def _log_call(self, kind, payload, answer, latency_ms, error=None):
        path = self.cfg.ai_log_path
        if not path:
            return
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(), "kind": kind, "provider": self.provider, "model": self.cfg.ai_model,
                    "prompt_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16],
                    "latency_ms": round(latency_ms), "answer_chars": len(answer or ""),
                    "error": (str(error)[:300] if error else None),
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------ core
    def _ask(self, system, payload, validator, kind="unknown"):
        """None = خطا یا خروجی نامعتبر."""
        if not self.enabled:
            return None
        key = hashlib.sha256((system + json.dumps(payload, sort_keys=True, default=str)).encode()).hexdigest()
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] <= max(0, self.cfg.ai_cache_s):
            return hit[1]
        last = None
        for attempt in range(max(0, self.cfg.ai_retries) + 1):
            t0 = time.time()
            try:
                raw = self._transport(system, payload)
                out = validator(_json_obj(raw))
                latency = (time.time() - t0) * 1000
                self._log_call(kind, payload, raw, latency)
                self.last_meta = {"provider": self.provider, "model": self.cfg.ai_model,
                                  "latency_ms": round(latency), "attempt": attempt + 1}
                if out is None:
                    self.last_error = "invalid_model_output"
                    return None
                if self.cfg.ai_cache_s > 0:
                    self._cache[key] = (now, out)
                    if len(self._cache) > 256:
                        self._cache = dict(list(self._cache.items())[-128:])
                return out
            except Exception as e:                       # noqa: BLE001 - هر خطا => fail-closed
                last = e
                self._log_call(kind, payload, "", (time.time() - t0) * 1000, error=e)
                log.warning("AI call failed (%s/%s): %s", self.provider, self.cfg.ai_model, e)
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
        self.last_error = f"{type(last).__name__}: {last}"[:300]
        return None

    def healthcheck(self, timeout_s=20):
        """بررسی کلید/مدل/شبکه با یک درخواست کوچک. هرگز کلید را برنمی‌گرداند."""
        if not self.enabled:
            return {"ok": False, "error": "AI key/model not configured"}
        t0 = time.time()
        try:
            raw = self._transport("Reply with ONLY the JSON {\"ok\": true}.",
                                  {"task": "healthcheck", "reply": {"ok": True}})
            ok = _json_obj(raw) is not None
            return {"ok": ok, "provider": self.provider, "model": self.cfg.ai_model,
                    "latency_ms": round((time.time() - t0) * 1000), "raw_chars": len(raw or "")}
        except Exception as e:                           # noqa: BLE001
            return {"ok": False, "provider": self.provider, "model": self.cfg.ai_model, "error": f"{type(e).__name__}: {e}"[:300]}

    def decide(self, sig, ctx):
        payload = {"signal": {"symbol": sig.symbol, "kind": sig.kind, "score": sig.score,
                              "reasons": sig.reasons, "features": sig.features}, **ctx}
        out = self._ask(SYS_GATE, payload, validate_gate, kind="gate")
        if out is None:
            return None
        out["approve"] = out["approve"] and out["confidence"] >= self.cfg.min_ai_conf
        out.update({"source": "ai", **self.last_meta})
        return out

    def review(self, ctx):
        out = self._ask(SYS_REVIEW, ctx, validate_review, kind="review")
        return {**out, "source": "ai", **self.last_meta} if out else None

    def optimize(self, ctx):
        """تحلیل نتایج بک‌تست و پیشنهاد سناریوهای جدید. خروجی None = خطا/خروجی نامعتبر."""
        out = self._ask(SYS_OPTIMIZE, ctx, validate_optimize, kind="optimize")
        if out is None:
            return None
        return {**out, "source": "ai", **self.last_meta}

    def reflect(self, ctx):
        out = self._ask(SYS_REFLECT, ctx, validate_reflect, kind="reflect")
        return {**out, "source": "ai", **self.last_meta} if out else None
