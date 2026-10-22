"""یادگیری از اشتباهات — قاعده‌مند، کران‌دار و قابل‌ممیزی.

چرا این شکل؟ در ۳ روز فقط ده‌ها معامله داریم؛ یادگیری آماری سنگین = overfitting. پس:
  1) هر معامله با ویژگی‌های ورود، MAE/MFE و برچسب «نوع اشتباه» ثبت می‌شود.
  2) هر `learn_every` معامله، الگوی غالب اشتباه‌ها شناسایی و *یک* پارامتر به‌اندازه یک گام کوچک و
     درون کران‌های سخت تغییر می‌کند (حداقل `learn_min_trades` معامله لازم است).
  3) اثر تغییر سنجیده می‌شود؛ اگر بدتر شد، برگردانده و آن پارامتر فریز می‌شود.
  4) نماد/نوع سیگنالی که پیاپی ضرر می‌دهد موقتاً غیرفعال می‌شود؛ آستانه امتیاز نوعِ ضررده بالا می‌رود.
  5) تحلیل متنی AI (lesson) در حافظه ذخیره و به پرامپت دروازه داده می‌شود (فقط متن؛ پارامتر عددی را AI تغییر نمی‌دهد).
همه تغییرات در خروجی events (و logs/decisions.jsonl) با شواهدشان ثبت می‌شوند => تکرارپذیر."""
from __future__ import annotations
import dataclasses
from collections import Counter

BOUNDS = {"sl_atr": (1.2, 2.4), "tp_atr": (1.8, 3.6), "rsi_buy": (25.0, 40.0),
          "max_trend_dev": (0.02, 0.06), "min_atr_pct": (0.005, 0.012)}
STEP = {"sl_atr": 0.1, "tp_atr": 0.1, "rsi_buy": 1.0, "max_trend_dev": 0.005, "min_atr_pct": 0.0005}


def make_record(pos, exit_ts, exit_price, net_pnl_pct, reason, bars):
    return {"ts": exit_ts, "symbol": pos.symbol, "kind": pos.kind, "pnl_pct": round(net_pnl_pct, 3),
            "reason": reason, "hold_bars": bars,
            "mae_pct": round(100 * (pos.low / pos.entry - 1), 3) if pos.low else 0.0,
            "mfe_pct": round(100 * (pos.high / pos.entry - 1), 3),
            "tp_pct": round(100 * pos.tp_pct, 3), "sl_pct": round(100 * pos.sl_pct, 3),
            "feat": {k: (pos.feat or {}).get(k) for k in ("rsi", "bb_pos", "trend_gap", "vol_ratio", "atr_pct")}}


def classify(r, fee_rate):
    if r["pnl_pct"] > 0:
        return "win"
    tp = max(r["tp_pct"], 1e-9)
    if r["mfe_pct"] >= 0.6 * tp:
        return "gave_back"                 # به ۶۰٪ تارگت رسید ولی برگشت => تارگت دور/تریلینگ شل
    if r["reason"] == "stop_loss" and r["hold_bars"] <= 3:
        return "fast_stop"                 # استاپ زودهنگام => نویز/استاپ تنگ
    if r["reason"] == "stop_loss" and r["mfe_pct"] < 0.25 * tp:
        return "never_worked"              # هیچ‌وقت کار نکرد => ورود بد
    if r["mfe_pct"] > 2 * fee_rate * 100 and r["pnl_pct"] <= 0:
        return "fee_drag"                  # سود ناخالص مثبت ولی کارمزد خورد
    if r["reason"] == "time_stop":
        return "stale"
    return "other_loss"


class Learner:
    def __init__(self, cfg, state: dict):
        self.cfg, self.s = cfg, state
        for k, v in (("trades", []), ("overrides", {}), ("frozen", {}), ("disabled", {}), ("bias", {}),
                     ("last_change", None), ("lessons", []), ("n_total", 0)):
            self.s.setdefault(k, v)
        for k in list(self.s["overrides"]):                      # کران‌ها را هنگام بارگذاری هم اعمال کن
            if k not in BOUNDS:
                del self.s["overrides"][k]
            else:
                lo, hi = BOUNDS[k]
                self.s["overrides"][k] = min(hi, max(lo, self.s["overrides"][k]))

    # ---- خروجی‌ها ----
    def effective_cfg(self):
        return dataclasses.replace(self.cfg, **self.s["overrides"]) if self.s["overrides"] else self.cfg

    def allowed(self, symbol, kind, now):
        until = self.s["disabled"].get(f"{symbol}|{kind}", 0)
        return (now >= until, f"{symbol}/{kind} disabled until {until:.0f}")

    def score_bias(self, kind):
        return float(self.s["bias"].get(kind, 0.0))

    def lessons(self, n=3):
        return [x["lesson"] for x in self.s["lessons"][-n:]]

    def add_lesson(self, lesson, ts):
        self.s["lessons"].append({"ts": ts, "lesson": str(lesson)[:400]})
        del self.s["lessons"][:-10]

    def summary(self):
        t = self.s["trades"][-self.cfg.learn_window:]
        tags = Counter(classify(r, self.cfg.fee_rate) for r in t)
        return {"n": len(t), "tags": dict(tags), "overrides": self.s["overrides"], "bias": self.s["bias"],
                "disabled": list(self.s["disabled"]), "recent": t[-6:]}

    # ---- ثبت و یادگیری ----
    def record(self, rec):
        c, s, ev = self.cfg, self.s, []
        s["trades"].append(rec)
        del s["trades"][:-200]
        s["n_total"] += 1
        rec["tag"] = classify(rec, c.fee_rate)
        if not c.learn_enabled:
            return ev
        # (الف) غیرفعال‌سازی نماد/نوع پس از ۳ ضرر پیاپی
        last = [t for t in s["trades"] if t["symbol"] == rec["symbol"] and t["kind"] == rec["kind"]][-3:]
        if len(last) == 3 and all(t["pnl_pct"] <= 0 for t in last):
            s["disabled"][f"{rec['symbol']}|{rec['kind']}"] = rec["ts"] + 12 * 3600
            ev.append({"event": "disable", "key": f"{rec['symbol']}|{rec['kind']}", "hours": 12,
                       "evidence": [t["pnl_pct"] for t in last]})
        # (ب) آستانه امتیاز هر نوع
        for kind in {"dip", "breakout"}:
            ks = [t["pnl_pct"] for t in s["trades"] if t["kind"] == kind][-20:]
            if len(ks) >= 6:
                shrunk = len(ks) / (len(ks) + 5) * (sum(ks) / len(ks))
                new = 5.0 if shrunk < 0 else 0.0
                if new != s["bias"].get(kind, 0.0):
                    s["bias"][kind] = new
                    ev.append({"event": "bias", "kind": kind, "bias": new, "shrunk_mean_pct": round(shrunk, 3), "n": len(ks)})
        # (پ) تنظیم پارامتر
        if s["n_total"] % c.learn_every == 0 and len(s["trades"]) >= c.learn_min_trades:
            ev += self._tune()
        return ev

    def _mean(self, ts):
        return sum(t["pnl_pct"] for t in ts) / len(ts) if ts else 0.0

    def _tune(self):
        c, s = self.cfg, self.s
        n = len(s["trades"])
        lc = s["last_change"]
        if lc:
            since = s["trades"][lc["n_at_change"]:]
            if len(since) < 2 * c.learn_every:
                return []                                         # صبر برای ارزیابی تغییر قبلی
            cur = self._mean(since)
            s["last_change"] = None
            if cur < lc["baseline"] - 0.3:
                s["overrides"][lc["param"]] = lc["old"]
                s["frozen"][lc["param"]] = s["n_total"] + 20
                return [{"event": "rollback", "param": lc["param"], "to": lc["old"],
                         "baseline_mean_pct": round(lc["baseline"], 3), "after_mean_pct": round(cur, 3), "n_after": len(since)}]
        window = s["trades"][-c.learn_window:]
        losses = [t for t in window if t["pnl_pct"] <= 0]
        if len(losses) < 3:
            return []
        tags = Counter(t.get("tag") or classify(t, c.fee_rate) for t in losses)
        share = lambda k: tags[k] / len(losses)
        never_kinds = Counter(t["kind"] for t in losses if (t.get("tag") or classify(t, c.fee_rate)) == "never_worked")
        cands = []
        if tags["gave_back"] >= 3 and share("gave_back") >= 0.4:
            cands.append(("tp_atr", -1, f"gave_back {tags['gave_back']}/{len(losses)} losses: take profit sooner"))
        if tags["fast_stop"] >= 3 and share("fast_stop") >= 0.4:
            cands.append(("sl_atr", +1, f"fast_stop {tags['fast_stop']}/{len(losses)} losses: stop too tight"))
        if tags["never_worked"] >= 3 and share("never_worked") >= 0.4:
            if never_kinds.get("dip", 0) >= never_kinds.get("breakout", 0):
                cands.append(("max_trend_dev", -1, "dip entries never worked: avoid dips in weak trend"))
            else:
                cands.append(("min_atr_pct", +1, "breakouts never worked: require more volatility"))
        if tags["fee_drag"] >= 2 and share("fee_drag") >= 0.3:
            cands.append(("min_atr_pct", +1, "fee drag: require larger moves"))
        eff = self.effective_cfg()
        for param, direction, why in cands:
            if s["frozen"].get(param, 0) > s["n_total"]:
                continue
            old = getattr(eff, param)
            lo, hi = BOUNDS[param]
            new = round(min(hi, max(lo, old + direction * STEP[param])), 6)
            if new == old:
                continue
            s["overrides"][param] = new
            s["last_change"] = {"param": param, "old": old, "new": new, "n_at_change": n, "baseline": self._mean(window)}
            return [{"event": "tune", "param": param, "old": old, "new": new, "why": why,
                     "loss_tags": dict(tags), "window_mean_pct": round(self._mean(window), 3)}]
        return []
