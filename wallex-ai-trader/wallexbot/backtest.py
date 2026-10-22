"""Backtest با همان strategy/risk/learner کد زنده — بدون AI (rules-only، محافظه‌کارانه‌تر از live).
ورود در open کندل بعد از سیگنال؛ کارمزد و لغزش اعمال می‌شود؛ Kill-Switch: ورود جدید متوقف و پوزیشن‌ها
در open کندل بعدی با مدل محافظه‌کارانه تسویه می‌شوند: min(open, close قبلی) × (1 − slippage×kill_slip_mult)."""
from __future__ import annotations
import csv, json, os, random
from collections import Counter
from .strategy import evaluate, diagnose
from .risk import RiskManager, Position, check_exit
from .learner import Learner, make_record

MANIFEST = "manifest.json"
REAL_SOURCE = "api.wallex.ir/v1/udf/history"


def synthetic(n=4000, seed=1, start=1_000_000_000.0, res=15):
    """فقط برای تست دود؛ هیچ اعتبار آماری ندارد."""
    r = random.Random(seed)
    px, out, t, anchor = start, [], 1_700_000_000, start
    for i in range(n):
        anchor *= 1 + r.gauss(0.00003, 0.0015)
        o = px
        px = px + 0.08 * (anchor - px) + px * r.gauss(0, 0.004)
        h = max(o, px) * (1 + abs(r.gauss(0, 0.0015)))
        l = min(o, px) * (1 - abs(r.gauss(0, 0.0015)))
        v = abs(r.gauss(100, 30)) * (1 + 40 * abs(px / o - 1))
        out.append({"t": t + i * res * 60, "o": o, "h": h, "l": l, "c": px, "v": v})
    return out


def load_csv(path):
    with open(path, encoding="utf-8") as f:
        return [{k: float(v) for k, v in row.items()} for row in csv.DictReader(f)]


def save_csv(path, candles):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["t", "o", "h", "l", "c", "v"])
        w.writeheader(); w.writerows(candles)


def load_real_dataset(directory, symbols):
    mp = os.path.join(directory, MANIFEST)
    if not os.path.exists(mp):
        raise SystemExit(f"{mp} نیست. داده واقعی فقط با `python -m wallexbot fetch` (نیازمند اینترنت) ساخته می‌شود.")
    with open(mp, encoding="utf-8") as f:
        man = json.load(f)
    if man.get("source") != REAL_SOURCE:
        raise SystemExit("manifest منبع واقعی والکس را تأیید نمی‌کند؛ داده به‌عنوان واقعی پذیرفته نمی‌شود")
    if man.get("source_resolution_min") != 1 or not man.get("aggregated_client_side"):
        raise SystemExit("manifest نشان نمی‌دهد داده از ۱ دقیقه‌ای تجمیع شده؛ با `fetch` دوباره بسازید")
    data = {s: load_csv(os.path.join(directory, f"{s}.csv")) for s in symbols
            if os.path.exists(os.path.join(directory, f"{s}.csv"))}
    from .aggregate import validate_series, DataIntegrityError
    for s, cs in data.items():                       # اعتبارسنجی مجدد: شکاف/تکرار/ترتیب/هم‌ترازی
        try:
            validate_series(cs, int(man["resolution"]))
        except DataIntegrityError as e:
            raise SystemExit(f"داده {s} نامعتبر است: {e}")
    if not data:
        raise SystemExit("هیچ CSV برای نمادهای تنظیم‌شده نیست")
    return data, man


def run_backtest(data: dict, cfg, initial=5_000_000.0, learn=False, label="unlabeled"):
    first = next(iter(data))
    ts_all = sorted({c["t"] for c in data[first]})
    idx = {s: {c["t"]: i for i, c in enumerate(cs)} for s, cs in data.items()}
    cash, pos, trades, curve, pending = initial, {}, [], [], []
    risk = RiskManager(cfg, {})
    learner = Learner(cfg, {}) if learn else None
    cooldown, events = {}, []
    liquidate, killed_at = False, None
    fn, scores = Counter(), {"dip": [], "breakout": []}     # قیف تشخیصی؛ روی نتایج اثر ندارد

    def equity(t):
        e = cash
        for s, p in pos.items():
            i = idx[s].get(t)
            e += p.qty * (data[s][i]["c"] if i is not None else p.entry)
        return e

    def close(s, t, px, reason):
        nonlocal cash
        p = pos.pop(s)
        gross = p.qty * px
        cash += gross - gross * cfg.fee_rate
        pnl = gross * (1 - cfg.fee_rate) - p.cost_tmn
        pct = 100 * pnl / p.cost_tmn
        trades.append({"symbol": s, "kind": p.kind, "entry_t": p.entry_ts, "exit_t": t, "entry": p.entry,
                       "exit": px, "pnl_tmn": pnl, "pnl_pct": pct, "reason": reason})
        if pnl < 0:
            cooldown[s] = t + cfg.cooldown_min * 60
        if learner:
            events.extend(learner.record(make_record(p, t, px, pct, reason, int((t - p.entry_ts) / (cfg.resolution * 60)))))

    for t in ts_all:
        eff = learner.effective_cfg() if learner else cfg
        risk.cfg = eff
        # 0) تسویه Kill-Switch (open کندل جاری، مدل محافظه‌کارانه)
        if liquidate:
            for s in list(pos):
                i = idx[s].get(t)
                if i is None:
                    continue
                prev_close = data[s][i - 1]["c"] if i > 0 else data[s][i]["o"]
                close(s, t, min(data[s][i]["o"], prev_close) * (1 - cfg.slippage * cfg.kill_slip_mult), "kill_switch")
            liquidate = bool(pos)
        # 1) ورودهای معلق
        for sig in ([] if risk.killed else pending):
            s, i = sig.symbol, idx[sig.symbol].get(t)
            if i is None or s in pos or len(pos) >= cfg.max_positions:
                fn["entry_skipped:no_bar/in_position/max_positions"] += 1
                continue
            eq = equity(t)
            px = data[s][i]["o"] * (1 + cfg.slippage)
            stop, tp, sl_pct, tp_pct = risk.levels(px, sig.atr_pct)
            notional = risk.size(eq, cash, sl_pct) * cfg.bt_size_mult
            if notional < cfg.min_notional:
                fn["entry_skipped:notional<min"] += 1
                continue
            fn["entries_executed"] += 1
            fee = notional * cfg.fee_rate
            cash -= notional + fee
            pos[s] = Position(s, notional / px, px, t, stop, tp, px, sig.kind, notional + fee, low=px,
                              sl_pct=sl_pct, tp_pct=tp_pct, feat=sig.features, last_px=px)
        pending = []
        # 2) خروج‌های داخل کندل (استاپ قبل از تارگت)
        for s in list(pos):
            i = idx[s].get(t)
            if i is None:
                continue
            c = data[s][i]
            ex = check_exit(pos[s], c["c"], t, eff, high=c["h"], low=c["l"])
            if ex:
                reason, ref = ex
                px = (min(ref, c["o"]) if reason in ("stop_loss", "trail_stop") else ref) * (1 - cfg.slippage)
                close(s, t, px, reason)
        eq = equity(t)
        risk.update(eq, t)
        curve.append((t, eq))
        if risk.killed and killed_at is None:
            killed_at, liquidate, pending = t, bool(pos), []
        # 3) سیگنال روی کندل بسته‌شده
        blocked = risk.entry_block(eq, len(pos))
        if blocked:
            fn["bars_entry_blocked:" + blocked.split(":")[0]] += 1
        else:
            sigs = []
            for s, cs in data.items():
                i = idx[s].get(t)
                if i is None:
                    continue
                if i < 120:
                    fn["skip:warmup(<120 bars)"] += 1; continue
                if s in pos:
                    fn["skip:already_in_position"] += 1; continue
                if cooldown.get(s, 0) > t:
                    fn["skip:cooldown_after_loss"] += 1; continue
                sig, f = evaluate(s, cs[max(0, i - cfg.candle_count + 1): i + 1], eff)   # فقط داده تا کندل i
                fn["bars_evaluated"] += 1
                if f is None:
                    fn["no_features"] += 1; continue
                d = diagnose(f, eff) if "atr_pct" in f else None   # تشخیص هرگز نباید منطق معامله را بشکند
                if d is None:
                    fn["diagnostics_unavailable"] += 1
                if d and not d["atr_ok"]:
                    fn["rejected:atr_below_min_atr_pct"] += 1
                for pat in (("dip", "breakout") if d else ()):
                    fails = [k for k, ok in d[pat].items() if not ok]
                    for k in fails:
                        fn[f"{pat}_cond_failed:{k}"] += 1
                    if len(fails) == 1:
                        fn[f"{pat}_nearmiss(only {fails[0]} fails)"] += 1
                    if not fails:
                        fn[f"{pat}_pattern_matched"] += 1
                        if not d["atr_ok"]:
                            fn[f"{pat}_matched_but_blocked_by_atr"] += 1
                if not sig:
                    continue
                fn[f"signal:{sig.kind}"] += 1
                scores[sig.kind].append(sig.score)
                bias = learner.score_bias(sig.kind) if learner else 0.0
                if learner and not learner.allowed(s, sig.kind, t)[0]:
                    fn["rejected:learner_disabled"] += 1; continue
                thr = max(eff.rules_min_score, eff.min_score) + bias
                if sig.score >= thr:
                    fn[f"passed_score:{sig.kind}"] += 1
                    sigs.append(sig)
                else:
                    fn[f"rejected:score<{thr:g} ({sig.kind})"] += 1
            pending = sorted(sigs, key=lambda x: -x.score)[: cfg.max_positions - len(pos)]
            fn["pending_entries_queued"] += len(pending)
    last_t = ts_all[-1]
    for s in list(pos):                                    # تسویه نهایی به تومان
        close(s, last_t, data[s][-1]["c"] * (1 - cfg.slippage), "final_liquidation")

    # Include final liquidation in the equity curve so its last point agrees
    # with the cash balance and the report's final_tmn value.
    if curve:
        if curve[-1][0] == last_t:
            curve[-1] = (last_t, cash)
        else:
            curve.append((last_t, cash))
    rep = summarize(initial, cash, trades, curve)
    rep.update({"data_source": label, "kill_switch_at": killed_at, "learning_events": len(events),
                "final_overrides": learner.s["overrides"] if learner else {}, "learn": learn,
                "funnel": dict(sorted(fn.items())),
                "score_stats": {k: ({"n": len(v), "min": round(min(v), 1), "max": round(max(v), 1),
                                     "mean": round(sum(v) / len(v), 1)} if v else {"n": 0}) for k, v in scores.items()},
                "settings": {"max_drawdown": cfg.max_drawdown, "daily_loss_limit": cfg.daily_loss_limit,
                             "rules_min_score": cfg.rules_min_score, "min_score": cfg.min_score,
                             "max_drawdown_status": "تعیین‌نشده توسط کاربر (پیش‌فرض پروژه) — مقدار ۳٪ ذکرشده فقط ثبت شده و اعمال نشده؛ docs/DECISIONS.md"},
                "assumptions": {"ai": "not simulated (rules-only, stricter than live)", "fee_rate": cfg.fee_rate,
                                "slippage": cfg.slippage, "bt_size_mult": cfg.bt_size_mult,
                                "kill_liquidation": f"min(open, prev_close)*(1-{cfg.slippage}*{cfg.kill_slip_mult})"}})
    return rep, trades, curve, events


def format_funnel(rep):
    lines = ["── قیف سیگنال (هر خط = تعداد کندل-نماد) ──"]
    for k, v in rep.get("funnel", {}).items():
        lines.append(f"{v:>7}  {k}")
    lines.append("امتیاز سیگنال‌ها: " + json.dumps(rep.get("score_stats", {}), ensure_ascii=False))
    return "\n".join(lines)


def summarize(initial, final, trades, curve):
    peak, mdd = initial, 0.0
    for _, e in curve:
        peak = max(peak, e); mdd = max(mdd, 1 - e / peak)
    wins = [t["pnl_tmn"] for t in trades if t["pnl_tmn"] > 0]
    loss = [-t["pnl_tmn"] for t in trades if t["pnl_tmn"] <= 0]
    return {"return_pct": round(100 * (final / initial - 1), 2), "final_tmn": round(final), "trades": len(trades),
            "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else 0,
            "profit_factor": round(sum(wins) / sum(loss), 2) if loss and sum(loss) else None,
            "avg_win_tmn": round(sum(wins) / len(wins)) if wins else 0,
            "avg_loss_tmn": round(sum(loss) / len(loss)) if loss else 0,
            "max_drawdown_pct": round(100 * mdd, 2)}


def write_outputs(out_dir, rep, trades, curve, events):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "backtest_report.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if trades:
        with open(os.path.join(out_dir, "backtest_trades.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0])); w.writeheader(); w.writerows(trades)
    with open(os.path.join(out_dir, "backtest_equity.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["t", "equity"]); w.writerows(curve)
    if events:
        with open(os.path.join(out_dir, "backtest_learning.jsonl"), "w", encoding="utf-8", newline="\n") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
