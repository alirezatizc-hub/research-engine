"""تیونر walk-forward: انتخاب پارامتر فقط روی بازهٔ آموزش و گزارش عملکرد روی بازهٔ آزمون.

هدف: جلوگیری از overfit به یک بازه. خروجی JSON + جدول متنی. هیچ پارامتری خودکار اعمال نمی‌شود.
"""
from __future__ import annotations
import dataclasses
import itertools
import json
import os

from .backtest import run_backtest

QUICK = {"min_atr_pct": [0.0015, 0.0025, 0.0045], "rsi_buy": [35.0, 42.0, 50.0],
         "bb_max_pos": [0.08, 0.25], "rules_min_score": [50.0, 60.0, 70.0]}
FULL = {"min_atr_pct": [0.0015, 0.0025, 0.0045, 0.006], "rsi_buy": [32.0, 35.0, 40.0, 45.0, 50.0],
        "bb_max_pos": [0.05, 0.08, 0.15, 0.25, 0.40], "rules_min_score": [50.0, 55.0, 60.0, 70.0]}


def _slice(data, lo=None, hi=None):
    out = {}
    for s, cs in data.items():
        bars = [c for c in cs if (lo is None or c["t"] >= lo) and (hi is None or c["t"] < hi)]
        if len(bars) >= 135:                     # 120 گرم‌شدن + حداقل ۱۵ کندل معامله
            out[s] = bars
    return out


def run_tuning(data, base_cfg, split=0.7, grid=None, min_trades=2, max_combos=200, label="real"):
    grid = grid or QUICK
    all_t = sorted({c["t"] for cs in data.values() for c in cs})
    if len(all_t) < 300:
        raise SystemExit(f"داده کافی نیست: {len(all_t)} کندل. برای walk-forward حداقل ~۳۰۰ کندل (ترجیحاً ۳۰ روز) لازم است.")
    cut = all_t[int(len(all_t) * split)]
    train, test = _slice(data, hi=cut), _slice(data, lo=cut)
    if not train or not test:
        raise SystemExit("یکی از بازه‌های آموزش/آزمون کندل کافی ندارد؛ split یا داده را تغییر دهید.")
    keys = list(grid)
    combos = list(itertools.product(*[grid[k] for k in keys]))[:max_combos]
    rows = []
    for i, vals in enumerate(combos, 1):
        p = dict(zip(keys, vals))
        if p.get("rules_min_score", 70) < 55:
            p["min_score"] = min(55.0, p["rules_min_score"])
        else:
            p["min_score"] = min(55.0, p["rules_min_score"])
        cfg = dataclasses.replace(base_cfg, **p)
        try:
            tr_rep, tr_trades, _, _ = run_backtest(train, cfg, learn=False, label=label + ":train")
            te_rep, te_trades, _, _ = run_backtest(test, cfg, learn=False, label=label + ":test")
        except Exception as e:                  # noqa: BLE001
            rows.append({**p, "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        rows.append({
            **p, "train_return_pct": tr_rep["return_pct"], "test_return_pct": te_rep["return_pct"],
            "train_trades": tr_rep["trades"], "test_trades": te_rep["trades"],
            "test_win_rate_pct": te_rep["win_rate_pct"], "test_max_drawdown_pct": te_rep["max_drawdown_pct"],
            "test_profit_factor": te_rep["profit_factor"],
            "ok": te_rep["trades"] >= min_trades,
        })
        print(f"[{i}/{len(combos)}] test={te_rep['return_pct']:+.2f}% trades={te_rep['trades']:<3} {p}", flush=True)
    good = [r for r in rows if r.get("ok")]
    ranked = sorted(good, key=lambda r: (-r["test_return_pct"], -r["test_trades"])) if good else []
    best = ranked[0] if ranked else None
    out = {"split": split, "grid": grid, "n_combos": len(combos), "min_trades": min_trades,
           "label": label, "cut_ts": cut, "train_bars": {s: len(cs) for s, cs in train.items()},
           "test_bars": {s: len(cs) for s, cs in test.items()},
           "best": best, "top": ranked[:10], "all": rows,
           "note": "پارامترها فقط با بازهٔ آموزش انتخاب می‌شوند؛ آزمون دست‌نخورده می‌ماند."}
    os.makedirs("logs/tuning", exist_ok=True)
    with open("logs/tuning/tuning_report.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    return out


def format_tuning(rep):
    lines = [f"walk-forward: train/test = {rep['split']:.0%}/{1-rep['split']:.0%} · "
             f"{rep['n_combos']} ترکیب · حداقل {rep['min_trades']} معامله در آزمون"]
    if not rep["top"]:
        lines.append("هیچ ترکیبی شرط حداقل معامله در آزمون را برآورده نکرد؛ نتیجه: «معامله نکردن» گزینهٔ معتبری است.")
        return "\n".join(lines)
    lines.append(f"{'test%':>8} {'train%':>8} {'trades':>7} {'win%':>6} {'DD%':>6}  params")
    for r in rep["top"]:
        p = {k: r[k] for k in ("min_atr_pct", "rsi_buy", "bb_max_pos", "rules_min_score")}
        lines.append(f"{r['test_return_pct']:>8.2f} {r['train_return_pct']:>8.2f} {r['test_trades']:>7} "
                     f"{r['test_win_rate_pct']:>6.1f} {r['test_max_drawdown_pct']:>6.2f}  {p}")
    b = rep["best"]
    lines.append("\nپیشنهاد (فقط پس از بررسی دستی):")
    for k in ("min_atr_pct", "rsi_buy", "bb_max_pos", "min_score", "rules_min_score"):
        lines.append(f"  WTB_{k.upper()}={b[k]}")
    lines.append("هشدار: انتخاب پارامتر روی نمونهٔ کوچک = overfit؛ قبل از live حتماً shadow بگیرید.")
    return "\n".join(lines)
