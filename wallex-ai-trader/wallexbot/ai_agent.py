"""عامل AI: دروازه ورود، مرور رژیم بازار، و تحلیل پس از معامله (reflection).
قواعد: خروجی باید JSON با نوع‌های دقیق باشد؛ هر خطا/نامعتبر => None (fail-closed). AI فقط می‌تواند سخت‌گیرتر شود."""
from __future__ import annotations
import json, math, logging
import requests

log = logging.getLogger("wtb")

SYS_GATE = """You are the decision agent of a spot-only crypto trading bot on the Iranian exchange Wallex (TMN pairs).
A rule-based engine proposed a LONG entry. Judge it using the numeric features, market snapshot, recent trades and "lessons"
(your own past post-trade analyses). Round-trip cost is about 0.7%, so reject weak-edge setups, falling-knife dips,
thin/wide books, signals contradicting the market snapshot, and setups resembling past mistakes in the lessons.
Reply with ONLY compact JSON: {"approve": true|false, "confidence": 0..1, "size_mult": 0.5..1.0, "reason": "<=40 words"}"""

SYS_REVIEW = """You supervise a spot crypto trading bot. Given the market snapshot, open positions, recent trades and equity,
choose a risk mode: "normal", "defensive" (halve position sizes; choppy/falling market or losing streak),
or "pause" (no new entries; sharp market-wide selloff or repeated stop-outs).
Reply with ONLY JSON: {"risk_mode": "normal|defensive|pause", "note": "<=40 words"}"""

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


def validate_reflect(o):
    if not isinstance(o, dict) or not isinstance(o.get("lesson"), str) or not o["lesson"].strip():
        return None
    if not _is_num(o.get("confidence"), 0, 1):
        return None
    return {"lesson": o["lesson"][:400], "confidence": float(o["confidence"])}


class AIAgent:
    def __init__(self, cfg, transport=None):
        self.cfg = cfg
        self.enabled = bool(cfg.ai_enabled and cfg.anthropic_api_key)
        self._transport = transport or self._http
        if not self.enabled:
            log.warning("AI غیرفعال/بدون کلید => rules-only (امتیاز >= %s)", cfg.rules_min_score)

    def _http(self, system, payload):
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.cfg.anthropic_api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self.cfg.ai_model, "max_tokens": 300, "system": system,
                  "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]},
            timeout=self.cfg.ai_timeout_s)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []) if isinstance(b, dict))

    def _ask(self, system, payload, validator):
        """None = خطا یا خروجی نامعتبر."""
        if not self.enabled:
            return None
        try:
            return validator(_json_obj(self._transport(system, payload)))
        except Exception as e:
            log.warning("AI call failed: %s", e)
            return None

    def decide(self, sig, ctx):
        payload = {"signal": {"symbol": sig.symbol, "kind": sig.kind, "score": sig.score,
                              "reasons": sig.reasons, "features": sig.features}, **ctx}
        out = self._ask(SYS_GATE, payload, validate_gate)
        if out is None:
            return None
        out["approve"] = out["approve"] and out["confidence"] >= self.cfg.min_ai_conf
        out["source"] = "ai"
        return out

    def review(self, ctx):
        out = self._ask(SYS_REVIEW, ctx, validate_review)
        return {**out, "source": "ai"} if out else None

    def reflect(self, ctx):
        out = self._ask(SYS_REFLECT, ctx, validate_reflect)
        return {**out, "source": "ai"} if out else None
