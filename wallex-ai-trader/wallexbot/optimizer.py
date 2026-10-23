"""چرخهٔ بهینه‌سازی خودکار: بک‌تست → تحلیل → ساخت سناریوهای جدید → اعتبارسنجی → پیشنهاد برای تأیید انسان.

اصول طراحی (قابل دفاع برای داوری):
1) **سه پنجرهٔ زمانی پیوسته**: آموزش (train) / اعتبارسنجی (validation) / آزمون (test).
   انتخاب پارامتر فقط با پنجرهٔ آموزش انجام می‌شود؛ رتبه‌بندی نهایی فقط با اعتبارسنجی.
   پنجرهٔ آزمون **یک‌بار** و بعد از انتخاب، فقط برای گزارش اجرا می‌شود و به AI هم نشان داده نمی‌شود.
2) **شکاف purge** بین پنجره‌ها تا اثر معاملات بسته‌شده در مرز، به پنجرهٔ بعد نشت نکند.
   سیگنال فقط روی کندل‌های بسته و بدون نگاه به آینده محاسبه می‌شود (همان کد live/backtest).
3) **چند سناریو، نه جست‌وجوی کور**: بسته‌های نام‌دار (محافظه‌کار، فعال، فقط dip، فقط breakout، استاپ تنگ/گشاد …)
   و در دورهای بعد، جهش‌های کوچک حول بهترین گزینه و/یا پیشنهادهای AI.
4) **AI تحلیل‌گر است، نه حاکم**: پارامترها را پیشنهاد می‌دهد، ولی هر پیشنهاد با کران‌های سخت
   (`learner.bounds_for` + جدول `ALLOWED`) و `Config.validate()` سنجیده می‌شود. پیشنهاد بیرون کران حذف می‌شود.
   خطای AI چرخه را متوقف نمی‌کند (به جست‌وجوی محلی برمی‌گردد).
5) **هیچ چیزی خودکار اعمال نمی‌شود**: خروجی `status="pending_approval"` است؛ اعمال فقط با
   `python -m wallexbot optimize --approve <run>` یا دکمهٔ پنل، و در همان لحظه `WTB_LIVE_ACK` باطل می‌شود.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import statistics
from datetime import datetime, timezone

from .backtest import run_backtest
from .learner import bounds_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ROOT = os.path.join("logs", "optimizer")
MIN_TRADE_BARS = 40          # حداقل کندل قابل‌معامله در هر پنجره
MIN_TRAIN_BARS = 180         # آموزش باید دست‌کم ۱۲۰ کندل گرم‌شدن + ۶۰ کندل معامله داشته باشد
PURGE_BARS = 8               # شکاف ۲ ساعته (۱۵ دقیقه‌ای) بین پنجره‌ها


class OptimizerError(RuntimeError):
    """خطای قابل‌نمایش (داده کم است، گزارش نیست، …)."""


class OptimizerStopped(RuntimeError):
    """توقف درخواستی کاربر."""


# --------------------------------------------------------------------------- داده و پنجره‌ها
def data_fingerprint(data) -> str:
    """اثر انگشت داده برای بازتولید: نماد، تعداد کندل، اولین/آخرین زمان و قیمت."""
    h = hashlib.sha256()
    for s in sorted(data):
        cs = data[s]
        if not cs:
            continue
        h.update(f"{s}:{len(cs)}:{int(cs[0]['t'])}:{int(cs[-1]['t'])}:"
                 f"{cs[0]['o']:.8g}:{cs[-1]['c']:.8g}".encode())
    return h.hexdigest()[:16]


def _slice(data, lo=None, hi=None):
    return {s: [c for c in cs if (lo is None or c["t"] >= lo) and (hi is None or c["t"] < hi)]
            for s, cs in data.items()}


def split_windows(data, split=(0.6, 0.2, 0.2), purge_bars=PURGE_BARS,
                  min_trade_bars=MIN_TRADE_BARS, min_train_bars=MIN_TRAIN_BARS):
    """سه پنجرهٔ پیوسته با شکاف purge. برای هر پنجره، دادهٔ «زمینه» (تا پایان پنجره) و
    `trade_from` برمی‌گردد: اندیکاتورها روی کل تاریخچه تا آن لحظه محاسبه می‌شوند، ولی معامله فقط
    داخل پنجره انجام می‌شود (بدون هدر رفتن ۱۲۰ کندل گرم‌شدن و بدون نشت از آینده).
    """
    if not data:
        raise OptimizerError("دادهٔ ورودی خالی است")
    if abs(sum(split) - 1.0) > 1e-6:
        raise OptimizerError("جمع نسبت‌های split باید ۱ باشد")
    ts = sorted({c["t"] for cs in data.values() for c in cs})
    n = len(ts)
    need = min_train_bars + 2 * min_trade_bars + 2 * purge_bars
    if n < need:
        raise OptimizerError(
            f"برای تقسیم سه‌بخشی حداقل {need} کندل لازم است، اما فقط {n} کندل موجود است. "
            f"با `python -m wallexbot fetch --days 30` دادهٔ بلندتر بگیرید (یا برای تست دود از --synthetic استفاده کنید).")
    avail = n - 2 * purge_bars
    n1 = int(avail * split[0])
    n2 = int(avail * split[1])
    n3 = avail - n1 - n2
    if n1 < min_train_bars or n2 < min_trade_bars or n3 < min_trade_bars:
        raise OptimizerError(f"پنجره‌ها کوچک‌اند (train={n1}, validation={n2}, test={n3})؛ دادهٔ بیشتری لازم است")

    train_end = ts[n1 - 1] + _step(ts)
    val_start = ts[n1 + purge_bars]
    val_end = ts[n1 + purge_bars + n2 - 1] + _step(ts)
    test_start = ts[n1 + purge_bars + n2 + purge_bars]

    windows = {
        "train": {"trade_from": ts[0], "t0": ts[0], "t1": train_end, "trade_bars": n1,
                  "context": _slice(data, hi=train_end), "trade": _slice(data, hi=train_end)},
        "validation": {"trade_from": val_start, "t0": val_start, "t1": val_end, "trade_bars": n2,
                       "context": _slice(data, hi=val_end), "trade": _slice(data, lo=val_start, hi=val_end)},
        "test": {"trade_from": test_start, "t0": test_start, "t1": ts[-1] + _step(ts), "trade_bars": n3,
                 "context": _slice(data, hi=ts[-1] + _step(ts)), "trade": _slice(data, lo=test_start)},
    }
    for name, w in windows.items():
        rows = w["trade"]
        if not rows:
            raise OptimizerError(f"پنجرهٔ {name} خالی است")
        shortest = min(len(cs) for cs in rows.values())
        if shortest < (min_trade_bars if name != "train" else min_train_bars):
            raise OptimizerError(f"پنجرهٔ {name}: کوتاه‌ترین نماد {shortest} کندل دارد (کمتر از حد لازم)")
    meta = {"bars": n, "purge_bars": purge_bars, "split": list(split),
            "windows": {k: {"t0": v["t0"], "t1": v["t1"], "trade_bars": v["trade_bars"],
                            "symbols": len(v["trade"])} for k, v in windows.items()}}
    return windows, meta


def _step(ts):
    return (ts[1] - ts[0]) if len(ts) > 1 else 900


# --------------------------------------------------------------------------- سنجش نتیجه
def _stability_pct(curve):
    """انحراف معیار بازده روزانه (٪) — معیار «پایداری» در داوری."""
    if not curve:
        return None
    by_day = {}
    for t, eq in curve:
        by_day[int(t // 86400)] = eq
    days = sorted(by_day)
    if len(days) < 2:
        return None
    rets = []
    for a, b in zip(days, days[1:]):
        if by_day[a]:
            rets.append(100 * (by_day[b] / by_day[a] - 1))
    return round(statistics.pstdev(rets), 3) if len(rets) >= 2 else None


def metrics_of(rep, curve=None, cfg=None, min_trades=2):
    """متریک‌های قابل‌مقایسه + امتیاز مرکب + دلیل رد (اگر غیرقابل‌قبول باشد)."""
    ret = float(rep.get("return_pct") or 0.0)
    mdd = float(rep.get("max_drawdown_pct") or 0.0)
    n = int(rep.get("trades") or 0)
    pf = rep.get("profit_factor")
    pf = float(pf) if isinstance(pf, (int, float)) else None
    stab = _stability_pct(curve or [])
    dd_cap = 100 * float(getattr(cfg, "max_drawdown", 0.05) or 0.05) if cfg is not None else 5.0
    reason = None
    if n < min_trades:
        reason = f"insufficient_trades({n}<{min_trades})"
    elif mdd > dd_cap + 1e-9:
        reason = f"drawdown_over_limit({mdd:.2f}>{dd_cap:.2f})"
    score = None
    if reason is None:
        score = (ret
                 - 2.0 * mdd
                 + 1.5 * (min(pf, 3.0) - 1.0 if pf is not None else 0.0)
                 + 0.2 * min(n, 10)
                 - 0.5 * (stab or 0.0))
        score = round(score, 3)
    return {"return_pct": round(ret, 2), "max_drawdown_pct": round(mdd, 2), "trades": n,
            "win_rate_pct": rep.get("win_rate_pct"), "profit_factor": pf,
            "stability_pct": stab, "score": score, "reject": reason,
            "final_tmn": rep.get("final_tmn"), "dd_cap_pct": round(dd_cap, 2)}


# --------------------------------------------------------------------------- فضای مجاز پارامترها
def _bounds(cfg):
    b = bounds_for(cfg)
    return {
        "min_atr_pct": ("float", b["min_atr_pct"][0], b["min_atr_pct"][1]),
        "rsi_buy": ("float", b["rsi_buy"][0], b["rsi_buy"][1]),
        "max_trend_dev": ("float", b["max_trend_dev"][0], b["max_trend_dev"][1]),
        "sl_atr": ("float", b["sl_atr"][0], b["sl_atr"][1]),
        "tp_atr": ("float", b["tp_atr"][0], b["tp_atr"][1]),
        "bb_max_pos": ("float", 0.05, 0.60),
        "min_score": ("float", 40.0, 80.0),
        "rules_min_score": ("float", 40.0, 85.0),
        "max_trades_per_day": ("int", 3, 40),
        "risk_per_trade": ("float", 0.002, 0.02),
        "max_pos_frac": ("float", 0.10, 0.35),
        "max_positions": ("int", 1, 4),
        "max_hold_h": ("float", 8.0, 72.0),
        "trail_activate": ("float", 0.01, 0.10),
        "trail_dist": ("float", 0.01, 0.06),
        "cooldown_min": ("float", 0.0, 120.0),
        "kinds": ("list", None, None),
    }


def allowed_space(cfg):
    """جدول قابل‌نمایش برای AI و برای گزارش."""
    b = _bounds(cfg)
    out = {}
    for k, (kind, lo, hi) in b.items():
        if kind == "list":
            out[k] = {"type": "list", "choices": ["dip", "breakout"]}
        else:
            out[k] = {"type": kind, "min": lo, "max": hi}
    return out


def sanitize_params(cfg, raw, *, base=None):
    """پیشنهاد (از AI یا هر جای دیگر) را به پارامترهای مجاز تبدیل می‌کند.

    -> (params, cfg_obj, issues). هر کلید ناشناخته/بیرون‌کران/نوع اشتباه در issues می‌آید.
    اگر issues خالی نباشد، params معتبر نیست.
    """
    issues = []
    if not isinstance(raw, dict):
        return {}, None, ["params must be an object"]
    bounds = _bounds(cfg)
    clean = {}
    for k, v in raw.items():
        if k not in bounds:
            issues.append(f"unknown parameter: {k}")
            continue
        kind, lo, hi = bounds[k]
        try:
            if kind == "list":
                if isinstance(v, str):
                    v = [x.strip() for x in v.split(",") if x.strip()]
                if not isinstance(v, list) or not v:
                    issues.append(f"{k}: باید لیستی از dip/breakout باشد")
                    continue
                bad = [x for x in v if x not in ("dip", "breakout")]
                if bad:
                    issues.append(f"{k}: مقدار نامجاز {bad}")
                    continue
                clean[k] = sorted(set(v))
            elif kind == "int":
                if isinstance(v, bool):
                    issues.append(f"{k}: boolean نامعتبر است")
                    continue
                iv = int(round(float(v)))
                if not (lo <= iv <= hi):
                    issues.append(f"{k}={iv} خارج از [{lo}, {hi}]")
                    continue
                clean[k] = iv
            else:
                fv = float(v)
                if isinstance(v, bool) or not math.isfinite(fv) or not (lo <= fv <= hi):
                    issues.append(f"{k}={v} خارج از [{lo}, {hi}]")
                    continue
                clean[k] = round(fv, 6)
        except (TypeError, ValueError):
            issues.append(f"{k}={v!r} عدد معتبر نیست")
    if issues:
        return clean, None, issues
    # پایداری قواعد: min_score <= rules_min_score و min_score <= آستانهٔ بدون AI
    cand = dict(clean)
    if "rules_min_score" in cand:
        cand.setdefault("min_score", min(cfg.min_score, cand["rules_min_score"]))
        if cand["min_score"] > cand["rules_min_score"]:
            cand["min_score"] = cand["rules_min_score"]
    try:
        obj = dataclasses.replace(base or cfg, **cand)
    except TypeError as e:
        return clean, None, [f"dataclasses.replace failed: {e}"]
    errs = obj.validate()
    if errs:
        return clean, None, [f"config invalid: {e}" for e in errs]
    return cand, obj, []


# --------------------------------------------------------------------------- سناریوها
def _clamp(cfg, key, val):
    """مقدار را داخل کران مجاز همان پارامتر نگه می‌دارد (سناریوهای اولیه هم باید معتبر باشند)."""
    kind, lo, hi = _bounds(cfg)[key]
    if kind == "int":
        return int(max(lo, min(hi, round(val))))
    if kind == "list":
        return val
    return round(max(lo, min(hi, float(val))), 6)


def seed_scenarios(cfg):
    """بسته‌های نام‌دار. اعداد نسبت به تنظیمات پایه ساخته می‌شوند تا با تغییر پیش‌فرض‌ها کهنه نشوند."""
    def f(x, nd=6):
        return round(float(x), nd)
    c = lambda k, v: _clamp(cfg, k, v)          # noqa: E731

    return [
        {"name": "baseline", "source": "seed", "note": "تنظیمات فعلی بدون تغییر", "params": {}},
        {"name": "conservative", "source": "seed", "note": "فیلتر نوسان سخت‌تر، آستانه بالاتر",
         "params": {"min_atr_pct": c("min_atr_pct", cfg.min_atr_pct * 1.6), "rsi_buy": c("rsi_buy", cfg.rsi_buy - 5),
                    "bb_max_pos": c("bb_max_pos", cfg.bb_max_pos * 0.7), "min_score": c("min_score", cfg.min_score + 8),
                    "rules_min_score": c("rules_min_score", cfg.rules_min_score + 8),
                    "max_trades_per_day": c("max_trades_per_day", cfg.max_trades_per_day * 0.6)}},
        {"name": "active", "source": "seed", "note": "آستانه پایین‌تر، معاملهٔ بیشتر",
         "params": {"min_atr_pct": c("min_atr_pct", cfg.min_atr_pct * 0.6), "rsi_buy": c("rsi_buy", cfg.rsi_buy + 5),
                    "bb_max_pos": c("bb_max_pos", cfg.bb_max_pos * 1.3),
                    "min_score": c("min_score", cfg.min_score - 5),
                    "rules_min_score": c("rules_min_score", cfg.rules_min_score - 5),
                    "max_trades_per_day": c("max_trades_per_day", cfg.max_trades_per_day * 1.5)}},
        {"name": "dip_only", "source": "seed", "note": "فقط الگوی بازگشت از افت",
         "params": {"kinds": ["dip"]}},
        {"name": "breakout_only", "source": "seed", "note": "فقط شکست سقف با حجم",
         "params": {"kinds": ["breakout"]}},
        {"name": "tight_stops", "source": "seed", "note": "استاپ/تارگت تنگ‌تر، خروج سریع‌تر",
         "params": {"sl_atr": c("sl_atr", cfg.sl_atr * 0.7), "tp_atr": c("tp_atr", cfg.tp_atr * 0.8),
                    "trail_activate": c("trail_activate", cfg.trail_activate * 0.75),
                    "max_hold_h": c("max_hold_h", cfg.max_hold_h * 0.5)}},
        {"name": "wide_swing", "source": "seed", "note": "استاپ/تارگت گشادتر، نگه‌داری بلندتر",
         "params": {"sl_atr": c("sl_atr", cfg.sl_atr * 1.25), "tp_atr": c("tp_atr", cfg.tp_atr * 1.15),
                    "trail_activate": c("trail_activate", cfg.trail_activate * 1.25),
                    "max_hold_h": c("max_hold_h", cfg.max_hold_h * 1.2)}},
        {"name": "fewer_bigger", "source": "seed", "note": "تمرکز روی بهترین سیگنال‌ها با حجم بزرگ‌تر",
         "params": {"max_positions": c("max_positions", cfg.max_positions - 1),
                    "max_pos_frac": c("max_pos_frac", cfg.max_pos_frac * 1.3),
                    "risk_per_trade": c("risk_per_trade", cfg.risk_per_trade * 0.75),
                    "max_trades_per_day": c("max_trades_per_day", cfg.max_trades_per_day * 0.4)}},
    ]


def mutate_scenarios(cfg, best_params, limit=6):
    """جست‌وجوی محلی قطعی: یک پارامتر در هر سناریو، یک گام کوچک، حول بهترین گزینه."""
    steps = [("min_atr_pct", 0.8), ("min_atr_pct", 1.25), ("rsi_buy", -3.0), ("rsi_buy", 3.0),
             ("bb_max_pos", -0.05), ("bb_max_pos", 0.05), ("rules_min_score", -5.0),
             ("rules_min_score", 5.0), ("sl_atr", -0.3), ("sl_atr", 0.3), ("tp_atr", -0.5),
             ("tp_atr", 0.5), ("max_trades_per_day", -5), ("max_trades_per_day", 5)]
    base = dict(best_params or {})
    out = []
    for key, delta in steps:
        cur = base.get(key, getattr(cfg, key))
        if key == "min_atr_pct":                 # delta اینجا ضریب است
            val, suffix = cur * delta, f"×{delta:g}"
        elif key == "max_trades_per_day":
            val, suffix = int(round(cur + delta)), f"{delta:+d}"
        else:
            val, suffix = cur + delta, f"{delta:+g}"
        params = dict(base)
        params[key] = round(val, 6) if isinstance(val, float) else val
        if "rules_min_score" not in params:
            params["rules_min_score"] = cfg.rules_min_score
        if "min_score" not in params:
            params["min_score"] = min(cfg.min_score, params["rules_min_score"])
        out.append({"name": f"{key}{suffix}", "source": "local_search",
                    "note": f"جهش حول بهترین گزینه: {key}", "params": params})
        if len(out) >= limit:
            break
    return out


def _signature(params):
    return json.dumps(params, sort_keys=True, default=str)


def _dedupe(scenarios, seen):
    out = []
    for s in scenarios:
        sig = _signature(s.get("params") or {})
        if sig in seen:
            continue
        seen.add(sig)
        out.append(s)
    return out


# --------------------------------------------------------------------------- AI
def _ai_summary(windows, results, cfg, baseline, failures):
    """آنچه به AI داده می‌شود؛ پنجرهٔ آزمون عمداً حذف است تا مدل روی آن تیون نشود."""
    rows = []
    for r in results[-14:]:
        rows.append({"name": r["name"], "source": r["source"], "params": r["params"],
                     "train": {k: r["train"].get(k) for k in ("return_pct", "max_drawdown_pct", "trades",
                                                              "win_rate_pct", "profit_factor", "score")},
                     "validation": {k: r["validation"].get(k) for k in ("return_pct", "max_drawdown_pct", "trades",
                                                                        "win_rate_pct", "profit_factor", "score")},
                     "reject": r["validation"].get("reject")})
    return {
        "goal": "choose strategy/risk parameters for a spot-only bot on TMN pairs; round-trip cost ≈0.7%",
        "windows": {"train": windows["train"], "validation": windows["validation"]},
        "baseline": baseline,
        "allowed_parameters": allowed_space(cfg),
        "results": rows,
        "failure_reasons": failures,
        "constraints": ["only parameters in allowed_parameters",
                        "min_score must stay <= rules_min_score",
                        "prefer scenarios that survive validation, not only train",
                        "the test window is hidden by design"],
    }


def _ask_ai(ai, payload):
    """-> (answer|None, error|None). هر استثنایی به خطا تبدیل می‌شود (چرخه متوقف نمی‌شود)."""
    if ai is None:
        return None, "AI disabled"
    try:
        fn = getattr(ai, "optimize", None)
        out = fn(payload) if callable(fn) else ai(payload)
    except Exception as e:                      # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"[:300]
    if out is None:
        err = getattr(ai, "last_error", None) or "no valid AI answer"
        return None, str(err)[:300]
    return out, None


def proposals_from_ai(answer, cfg, base_cfg, limit=4):
    """پیشنهادهای AI را می‌سنجد؛ هر کدام که بیرون کران/نامعتبر باشد با دلیل رد می‌شود."""
    accepted, rejected = [], []
    if not isinstance(answer, dict):
        return accepted, [{"reason": "answer is not a JSON object"}]
    props = answer.get("proposals")
    if not isinstance(props, list):
        props = []
    for p in props[:limit]:
        if not isinstance(p, dict):
            rejected.append({"reason": "proposal is not an object"})
            continue
        name = str(p.get("name") or f"ai-{len(accepted) + 1}")[:40]
        params, obj, issues = sanitize_params(cfg, p.get("params") or {}, base=base_cfg)
        if issues or obj is None:
            rejected.append({"name": name, "params": p.get("params"), "issues": issues})
            continue
        if not params:
            rejected.append({"name": name, "reason": "empty params"})
            continue
        accepted.append({"name": f"ai:{name}", "source": "ai", "params": params,
                         "note": str(p.get("rationale") or p.get("note") or "AI proposal")[:200]})
    return accepted, rejected


# --------------------------------------------------------------------------- حلقهٔ اصلی
def _evaluate(scenario, windows, base_cfg, name, min_trades, progress, stage, idx, total, should_stop):
    params, cfg_obj, issues = sanitize_params(base_cfg, scenario.get("params") or {}, base=base_cfg)
    row = {"name": scenario["name"], "source": scenario.get("source", "seed"), "note": scenario.get("note", ""),
           "params": params or scenario.get("params") or {}, "issues": issues, "train": {}, "validation": {},
           "score": None, "status": "ok"}
    if issues or cfg_obj is None:
        row["status"] = "invalid"
        row["validation"]["reject"] = "; ".join(issues[:3])
        return row
    for win in ("train", "validation"):
        if should_stop and should_stop():
            raise OptimizerStopped("stopped by user")
        w = windows[win]
        try:
            rep, _trades, curve, _events = run_backtest(w["context"], cfg_obj, learn=False,
                                                        label=f"optimizer:{win}", trade_from=w["trade_from"])
        except Exception as e:                  # noqa: BLE001
            row["status"] = "error"
            row[win] = {"reject": f"{type(e).__name__}: {e}"[:200]}
            return row
        row[win] = metrics_of(rep, curve, cfg_obj, min_trades=min_trades)
        row[win]["funnel_top"] = sorted(rep.get("funnel", {}).items(), key=lambda kv: -kv[1])[:6]
        progress(stage, idx, total, f"{scenario['name']} · {win}")
    tr, va = row["train"], row["validation"]
    if row["status"] == "ok":
        if tr.get("reject"):
            row["status"] = "rejected_train"
        elif va.get("reject"):
            row["status"] = "rejected_validation"
        else:
            row["score"] = round((tr["score"] + 2.0 * va["score"]) / 3.0, 3)   # وزن بیشتر به اعتبارسنجی
    return row


def run_optimizer(data, base_cfg, *, data_label="real", split=(0.6, 0.2, 0.2), purge_bars=PURGE_BARS,
                  min_trades=2, rounds=2, ai=None, out_dir=None, progress=None, should_stop=None,
                  scenarios=None, run_id=None, min_trade_bars=MIN_TRADE_BARS, max_scenarios=60):
    """چرخهٔ کامل. خروجی = گزارش JSON (همان که در فایل نوشته می‌شود)."""
    progress = progress or (lambda *a, **k: None)
    should_stop = should_stop or (lambda: False)
    run_id = run_id or "opt-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = out_dir or os.path.join(ROOT, OUT_ROOT, run_id)

    progress("split", 0, 1, "تقسیم داده به سه پنجره")
    windows, wmeta = split_windows(data, split=split, purge_bars=purge_bars, min_trade_bars=min_trade_bars)

    seeds = scenarios if scenarios is not None else seed_scenarios(base_cfg)
    seen = set()
    pool = _dedupe(seeds, seen)
    rows, ai_trace, rounds_log = [], [], []
    best = None
    rounds = max(1, int(rounds))

    for rnd in range(rounds):
        progress("round", rnd, rounds, f"دور {rnd + 1}: {len(pool)} سناریو")
        total = len(pool)
        round_rows = []
        for i, sc in enumerate(pool, 1):
            if should_stop():
                raise OptimizerStopped("stopped by user")
            row = _evaluate(sc, windows, base_cfg, sc["name"], min_trades, progress, "evaluate", i, total, should_stop)
            row["round"] = rnd + 1
            rows.append(row)
            round_rows.append(row)
        ok = [r for r in round_rows if r["status"] == "ok"]
        ok.sort(key=lambda r: -r["score"])
        best = ok[0] if ok else best
        rounds_log.append({"round": rnd + 1, "scenarios": len(round_rows),
                           "valid": len(ok), "best": ok[0]["name"] if ok else None,
                           "best_score": ok[0]["score"] if ok else None})
        if rnd + 1 >= rounds:
            break
        # ---- تولید نامزدهای دور بعد: پیشنهاد AI + جهش محلی
        candidates = []
        if ai is not None:
            progress("ai", rnd, rounds, "تحلیل نتایج توسط AI")
            payload = _ai_summary(wmeta["windows"], round_rows, base_cfg,
                                  baseline=next((r for r in rows if r["name"] == "baseline"), None),
                                  failures=_failure_digest(round_rows))
            answer, err = _ask_ai(ai, payload)
            accepted, rejected = proposals_from_ai(answer, base_cfg, base_cfg) if answer else ([], [])
            ai_trace.append({"round": rnd + 1, "ok": bool(answer), "error": err,
                             "diagnosis": (answer or {}).get("diagnosis"),
                             "failure_modes": (answer or {}).get("failure_modes"),
                             "recommend": (answer or {}).get("recommend"),
                             "proposals": accepted, "rejected": rejected})
            candidates += accepted
        if best is not None:
            candidates += mutate_scenarios(base_cfg, best["params"])
        pool = _dedupe(candidates, seen)[:max_scenarios]
        if not pool:
            break

    # ---- ارزیابی نهایی روی پنجرهٔ آزمون (یک‌بار، بعد از انتخاب)
    progress("test", 0, 2, "ارزیابی روی دادهٔ آزمون")
    final = {"best": None, "baseline": None}
    picks = [("best", best)] if best is not None else []
    base_row = next((r for r in rows if r["name"] == "baseline"), None)
    if base_row is not None and base_row["status"] == "ok":
        picks.append(("baseline", base_row))
    for i, (tag, row) in enumerate(picks):
        params, cfg_obj, _issues = sanitize_params(base_cfg, row["params"], base=base_cfg)
        if cfg_obj is None:
            continue
        w = windows["test"]
        rep, _t, curve, _e = run_backtest(w["context"], cfg_obj, learn=False,
                                          label="optimizer:test", trade_from=w["trade_from"])
        final[tag] = {"name": row["name"], "params": params,
                      **metrics_of(rep, curve, cfg_obj, min_trades=min_trades)}
        progress("test", i + 1, len(picks), row["name"])

    proposal = _build_proposal(run_id, base_cfg, best, final, wmeta, data, data_label, ai_trace, rounds_log)
    report = {"run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "data": {"label": data_label, "fingerprint": data_fingerprint(data), "symbols": sorted(data),
                       "bars": wmeta["bars"], "windows": wmeta["windows"], "purge_bars": purge_bars,
                       "split": list(split)},
              "config_base": {k: getattr(base_cfg, k) for k in
                              ("min_atr_pct", "rsi_buy", "bb_max_pos", "min_score", "rules_min_score",
                               "sl_atr", "tp_atr", "max_trend_dev", "max_trades_per_day", "risk_per_trade",
                               "max_pos_frac", "max_positions", "max_hold_h", "trail_activate", "trail_dist",
                               "kinds", "fee_rate", "slippage")},
              "config_fingerprint": base_cfg.fingerprint(),
              "min_trades": min_trades, "rounds": rounds,
              "ranking": [{k: r.get(k) for k in ("name", "source", "params", "score", "status")} |
                          {"train": r["train"].get("return_pct"), "validation": r["validation"].get("return_pct"),
                           "reject": r["validation"].get("reject")}
                          for r in sorted(rows, key=lambda r: -(r.get("score") or -1e9))[:20]],
              "scenarios": rows, "ai": ai_trace, "rounds_log": rounds_log,
              "final": final, "proposal": proposal,
              "guardrails": {"min_trades": min_trades, "drawdown_cap_pct": round(100 * base_cfg.max_drawdown, 2),
                             "purge_bars": purge_bars, "test_used_for_selection": False,
                             "auto_apply": False, "ai_can_change_params_directly": False},
              "status": "pending_approval" if proposal.get("params") else "no_candidate"}

    _write_run(out_dir, report)
    progress("done", 1, 1, "گزارش نوشته شد")
    return report


def _failure_digest(rows):
    """پرتکرارترین دلیل‌های رد/بی‌عملی برای تحلیل."""
    cnt = {}
    for r in rows:
        for w in ("train", "validation"):
            for k, v in (r[w].get("funnel_top") or []):
                cnt[k] = cnt.get(k, 0) + int(v)
    return dict(sorted(cnt.items(), key=lambda kv: -kv[1])[:10])


def _build_proposal(run_id, base_cfg, best, final, wmeta, data, data_label, ai_trace, rounds_log):
    test = final.get("best") or {}
    if not best or best.get("status") != "ok":
        return {"run_id": run_id, "status": "no_candidate", "params": {},
                "reason": "هیچ سناریویی شرط حداقل معامله/سقف افت را در آموزش و اعتبارسنجی پاس نکرد؛ "
                          "«معامله نکردن» یا ماندن روی تنظیمات فعلی گزینهٔ درست است.",
                "evidence": {"rounds": rounds_log}}
    base_params = {k: getattr(base_cfg, k) for k in best["params"]}
    diff = {k: {"from": base_params[k], "to": v} for k, v in best["params"].items()
            if base_params.get(k) != v}
    val_ret = (best["validation"] or {}).get("return_pct")
    test_ret = test.get("return_pct")
    recommend = bool(val_ret is not None and val_ret > 0 and (test_ret is None or test_ret > 0))
    warning = None
    if val_ret is None or val_ret <= 0:
        warning = (f"بازدهی پنجرهٔ اعتبارسنجی {val_ret}٪ است؛ این گزینه «بهترین از بدها» است و "
                   "اعمال آن توصیه نمی‌شود. تا وقتی هیچ سناریویی در اعتبارسنجی مثبت نیست، "
                   "ماندن روی تنظیمات فعلی یا معاملهٔ کمتر گزینهٔ درست‌تری است.")
    elif test_ret is not None and test_ret <= 0:
        warning = (f"اعتبارسنجی مثبت بود ({val_ret}٪) اما روی دادهٔ آزمون منفی شد ({test_ret}٪)؛ "
                   "نشانهٔ بیش‌برازش است و اعمال توصیه نمی‌شود.")
    return {
        "run_id": run_id, "status": "pending_approval",
        "selected": best["name"], "source": best["source"], "params": best["params"],
        "diff_vs_current": diff, "note": best.get("note", ""),
        "train": {k: best["train"].get(k) for k in ("return_pct", "max_drawdown_pct", "trades", "profit_factor", "stability_pct")},
        "validation": {k: best["validation"].get(k) for k in ("return_pct", "max_drawdown_pct", "trades", "profit_factor", "stability_pct")},
        "held_out_test": {k: test.get(k) for k in ("return_pct", "max_drawdown_pct", "trades", "profit_factor", "stability_pct")},
        "baseline_on_test": {k: (final.get("baseline") or {}).get(k) for k in ("return_pct", "max_drawdown_pct", "trades")},
        "score": best.get("score"),
        "recommend_apply": recommend, "warning": warning,
        "ai": {"used": bool(ai_trace), "rounds": len(ai_trace),
               "diagnosis": ai_trace[-1].get("diagnosis") if ai_trace else None},
        "data": {"label": data_label, "fingerprint": data_fingerprint(data), "bars": wmeta["bars"],
                 "train_bars": wmeta["windows"]["train"]["trade_bars"],
                 "validation_bars": wmeta["windows"]["validation"]["trade_bars"],
                 "test_bars": wmeta["windows"]["test"]["trade_bars"]},
        "approval": {"required": True,
                     "apply_cli": f"python -m wallexbot optimize --approve {run_id}",
                     "note": "اعمال، WTB_LIVE_ACK را باطل می‌کند؛ باید params و safetytests دوباره اجرا شوند."},
    }


# --------------------------------------------------------------------------- خروجی‌ها
def _write_run(out_dir, report):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, indent=1, ensure_ascii=False, default=str)
        f.write("\n")
    with open(os.path.join(out_dir, "proposal.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(report["proposal"], f, indent=1, ensure_ascii=False, default=str)
        f.write("\n")
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(render_summary(report))
    rows = [r for r in report["scenarios"] if r.get("train") or r.get("validation")]
    if rows:
        import csv
        with open(os.path.join(out_dir, "scenarios.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["round", "name", "source", "status", "score", "train_return_pct", "train_dd_pct",
                        "train_trades", "val_return_pct", "val_dd_pct", "val_trades", "val_pf", "reject", "params"])
            for r in rows:
                tr, va = r.get("train") or {}, r.get("validation") or {}
                w.writerow([r.get("round"), r["name"], r["source"], r["status"], r.get("score"),
                            tr.get("return_pct"), tr.get("max_drawdown_pct"), tr.get("trades"),
                            va.get("return_pct"), va.get("max_drawdown_pct"), va.get("trades"),
                            va.get("profit_factor"), va.get("reject"),
                            json.dumps(r.get("params"), ensure_ascii=False)])
    return out_dir


def _fa_ts(ts):
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "—"


def render_summary(report):
    p = report.get("proposal") or {}
    w = report["data"]["windows"]
    lines = [f"# گزارش چرخهٔ بهینه‌سازی — `{report['run_id']}`", "",
             f"- داده: **{report['data']['label']}** · {report['data']['bars']} کندل · نمادها: {', '.join(report['data']['symbols'])}",
             f"- پنجره‌ها: آموزش {w['train']['trade_bars']} کندل ({_fa_ts(w['train']['t0'])} → {_fa_ts(w['train']['t1'])}) · "
             f"اعتبارسنجی {w['validation']['trade_bars']} · آزمون {w['test']['trade_bars']} · شکاف purge {report['data']['purge_bars']} کندل",
             f"- اثر انگشت داده (fingerprint): `{report['data']['fingerprint']}`", "",
             "## تصمیم پیشنهادی", ""]
    if p.get("status") != "pending_approval":
        lines += [f"هیچ گزینه‌ای تأیید نشد. {p.get('reason', '')}", "",
                  "گزینهٔ «معامله نکردن» یا ماندن روی تنظیمات فعلی مجاز و محترم است.", ""]
    else:
        lines += [f"- سناریو: **{p['selected']}** (منبع: {p['source']}) — {p.get('note', '')}",
                  f"- پارامترها: `{json.dumps(p['params'], ensure_ascii=False)}`",
                  f"- تغییر نسبت به تنظیمات فعلی: `{json.dumps(p['diff_vs_current'], ensure_ascii=False)}`", "",
                  "| پنجره | بازدهی | حداکثر افت | معاملات | Profit Factor | پایداری (انحراف روزانه) |",
                  "|---|---|---|---|---|---|"]
        for tag, key in (("آموزش", "train"), ("اعتبارسنجی", "validation"), ("آزمون (دست‌نخورده)", "held_out_test")):
            m = p.get(key) or {}
            lines.append(f"| {tag} | {m.get('return_pct')}٪ | {m.get('max_drawdown_pct')}٪ | {m.get('trades')} | "
                         f"{m.get('profit_factor')} | {m.get('stability_pct')} |")
        if p.get("warning"):
            lines += [f"> ⚠️ {p['warning']}", ""]
        b = p.get("baseline_on_test") or {}
        lines += ["", f"مقایسهٔ مرجع در همان پنجرهٔ آزمون (تنظیمات فعلی): بازدهی {b.get('return_pct')}٪ · "
                      f"افت {b.get('max_drawdown_pct')}٪ · {b.get('trades')} معامله", ""]
        lines += ["### تأیید", "",
                  "این پیشنهاد **خودکار اعمال نشده** است. برای اعمال:",
                  "",
                  f"```bash\n{p['approval']['apply_cli']}\n```",
                  "",
                  "پس از اعمال، `WTB_LIVE_ACK` باطل می‌شود و باید `python -m wallexbot params` و "
                  "`python -m wallexbot safetytests` دوباره اجرا شوند.", ""]
    if report.get("ai"):
        lines += ["## تحلیل AI", ""]
        for a in report["ai"]:
            lines.append(f"- دور {a['round']}: " + ("موفق" if a["ok"] else f"ناموفق ({a.get('error')})"))
            if a.get("diagnosis"):
                lines.append(f"  - تشخیص: {a['diagnosis']}")
            for fm in (a.get("failure_modes") or [])[:4]:
                lines.append(f"  - الگوی خطا: {fm}")
            for pr in a.get("proposals") or []:
                lines.append(f"  - پیشنهاد: `{pr['name']}` → {json.dumps(pr['params'], ensure_ascii=False)}")
            for rj in (a.get("rejected") or [])[:3]:
                lines.append(f"  - رد شد: {rj}")
        lines.append("")
    lines += ["## جدول سناریوها", "",
              "| دور | سناریو | منبع | وضعیت | امتیاز | بازده آموزش | افت آموزش | معاملات | بازده اعتبارسنجی | افت اعتبارسنجی | معاملات |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(report["scenarios"], key=lambda r: (r.get("round") or 0, -(r.get("score") or -1e9))):
        tr, va = r.get("train") or {}, r.get("validation") or {}
        lines.append(f"| {r.get('round')} | {r['name']} | {r['source']} | {r['status']} | {r.get('score')} | "
                     f"{tr.get('return_pct')} | {tr.get('max_drawdown_pct')} | {tr.get('trades')} | "
                     f"{va.get('return_pct')} | {va.get('max_drawdown_pct')} | {va.get('trades')} |")
    lines += ["", "> امتیاز مرکب = بازدهی − ۲×حداکثر افت + ۱٫۵×(PF−۱) + ۰٫۲×تعداد معامله − ۰٫۵×نوسان روزانه.",
              "> انتخاب فقط با آموزش انجام و با اعتبارسنجی رتبه‌بندی می‌شود؛ پنجرهٔ آزمون در انتخاب نقشی ندارد.", ""]
    return "\n".join(lines)


def format_report(report):
    p = report.get("proposal") or {}
    out = [f"run: {report['run_id']} · data={report['data']['label']} ({report['data']['bars']} candles)",
           f"windows: train={report['data']['windows']['train']['trade_bars']} "
           f"val={report['data']['windows']['validation']['trade_bars']} "
           f"test={report['data']['windows']['test']['trade_bars']}"]
    for r in sorted(report["scenarios"], key=lambda r: (r.get("round") or 0, -(r.get("score") or -1e9)))[:15]:
        tr, va = r.get("train") or {}, r.get("validation") or {}
        out.append(f"  [{r.get('round')}] {r['name'][:34]:<34} {r['source']:<12} {r['status']:<20} "
                   f"train {tr.get('return_pct')}% / val {va.get('return_pct')}% / score {r.get('score')}")
    if p.get("status") == "pending_approval":
        out += ["", f"BEST: {p['selected']} ({p['source']}) · validation {p['validation']['return_pct']}% · "
                    f"test {p['held_out_test']['return_pct']}%",
                f"params: {json.dumps(p['params'], ensure_ascii=False)}"]
        if not p.get("recommend_apply", True):
            out.append("WARNING: validation return is not positive — applying is NOT recommended")
        out.append(f"apply after review: {p['approval']['apply_cli']}")
    else:
        out += ["", f"NO CANDIDATE: {p.get('reason', '')}"]
    return "\n".join(out)


# --------------------------------------------------------------------------- تأیید و اعمال
APPLY_KEYS = ("kinds", "min_atr_pct", "rsi_buy", "bb_max_pos", "min_score", "rules_min_score", "sl_atr", "tp_atr",
              "max_trend_dev", "max_trades_per_day", "risk_per_trade", "max_pos_frac", "max_positions",
              "max_hold_h", "trail_activate", "trail_dist", "cooldown_min")


def _run_created(run_id):
    try:
        p = os.path.join(ROOT, OUT_ROOT, run_id, "proposal.json")
        return datetime.fromtimestamp(os.path.getmtime(p), tz=timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return None


def list_runs(root=None):
    base = os.path.join(root or ROOT, OUT_ROOT)
    if not os.path.isdir(base):
        return []
    out = []
    for name in sorted(os.listdir(base), reverse=True):
        p = os.path.join(base, name, "proposal.json")
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                prop = json.load(f)
        except ValueError:
            continue
        out.append({"run_id": name, "status": prop.get("status"), "selected": prop.get("selected"),
                    "source": prop.get("source"), "score": prop.get("score"),
                    "train": prop.get("train"), "validation": prop.get("validation"),
                    "held_out_test": prop.get("held_out_test"), "params": prop.get("params"),
                    "diff_vs_current": prop.get("diff_vs_current") or prop.get("diff"),
                    "recommend_apply": prop.get("recommend_apply"), "warning": prop.get("warning"),
                    "created_at": prop.get("created_at") or _run_created(name)})
    return out


def load_run(run_id, root=None):
    base = os.path.join(root or ROOT, OUT_ROOT, run_id)
    rp, pp = os.path.join(base, "report.json"), os.path.join(base, "proposal.json")
    if not os.path.exists(pp):
        raise OptimizerError(f"گزارش اجرای {run_id} پیدا نشد")
    with open(pp, encoding="utf-8") as f:
        prop = json.load(f)
    rep = None
    if os.path.exists(rp):
        try:
            with open(rp, encoding="utf-8") as f:
                rep = json.load(f)
        except ValueError:
            rep = None
    return {"run_id": run_id, "dir": base, "proposal": prop, "report": rep}


def _drop_env_keys(path, keys):
    """کلیدهایی مثل WTB_LIVE_ACK را از .env حذف می‌کند (چون اثرانگشت پارامترها عوض شده)."""
    if not os.path.exists(path):
        return []
    kept, removed = [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k = s.split("=", 1)[0].strip().upper()
                if k in {x.upper() for x in keys}:
                    removed.append(k)
                    continue
            kept.append(line.rstrip("\n"))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(kept) + "\n")
    return removed


def apply_proposal(run_id, *, root=None, env_path=None, log_dir=None, confirm=False, cfg=None, force=False):
    """اعمال پیشنهاد پس از تأیید صریح انسان. هیچ‌چیز خودکار اعمال نمی‌شود."""
    from .config import save_env
    root = root or ROOT
    if not confirm:
        raise OptimizerError("اعمال نیازمند تأیید صریح است (confirm=true / --yes)")
    run = load_run(run_id, root=root)
    prop = run["proposal"]
    if prop.get("status") == "applied":
        raise OptimizerError(f"این پیشنهاد قبلاً اعمال شده است ({run_id})")
    if prop.get("status") != "pending_approval" or not prop.get("params"):
        raise OptimizerError("این اجرا پیشنهاد قابل‌اعمالی ندارد")
    if prop.get("recommend_apply") is False and not force:
        raise OptimizerError("این پیشنهاد در اعتبارسنجی مثبت نیست و اعمال توصیه نمی‌شود؛ "
                             "برای اعمال آگاهانه از --force (یا force=true) استفاده کنید: " + str(prop.get("warning") or ""))
    params = {k: v for k, v in prop["params"].items() if k in APPLY_KEYS}
    if not params:
        raise OptimizerError("هیچ پارامتر قابل‌اعمالی در پیشنهاد نیست")

    # اعتبارسنجی نهایی روی کانفیگ فعلی (ممکن است .env از زمان اجرا عوض شده باشد)
    from .config import Config
    cur = cfg or Config.from_env()
    _clean, obj, issues = sanitize_params(cur, params, base=cur)
    if issues or obj is None:
        raise OptimizerError(f"پیشنهاد با تنظیمات فعلی سازگار نیست: {'; '.join(issues[:3])}")

    env_path = env_path or os.path.join(os.getcwd(), ".env")
    before = {}
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    before[s.split("=", 1)[0].strip().upper()] = s.split("=", 1)[1]
    values = {f"WTB_{k.upper()}": (",".join(v) if isinstance(v, list) else v) for k, v in params.items()}
    save_env(env_path, values)
    cleared = _drop_env_keys(env_path, ["WTB_LIVE_ACK"])

    prop.update({"status": "applied", "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "applied_params": params})
    with open(os.path.join(run["dir"], "proposal.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(prop, f, indent=1, ensure_ascii=False, default=str)
        f.write("\n")
    log_dir = log_dir or os.path.join(root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    event = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": "OPTIMIZE_APPLY",
             "run_id": run_id, "selected": prop.get("selected"), "params": params,
             "changed_from": {k: before.get(f"WTB_{k.upper()}") for k in params},
             "validation": prop.get("validation"), "held_out_test": prop.get("held_out_test"),
             "live_ack_cleared": bool(cleared), "env": env_path}
    with open(os.path.join(log_dir, "decisions.jsonl"), "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    return {"ok": True, "run_id": run_id, "applied": params, "env": env_path,
            "live_ack_cleared": bool(cleared),
            "next": ["python -m wallexbot params   # اثرانگشت جدید را بگیرید",
                     "python -m wallexbot safetytests",
                     "پنل → دروازهٔ Live → بررسی دوباره"]}
