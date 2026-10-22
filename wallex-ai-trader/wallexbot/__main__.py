from __future__ import annotations
import argparse, csv, json, os, sys, unittest
from .config import Config
from .utils import setup_logging


def main():
    p = argparse.ArgumentParser(prog="wallexbot")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--mode", choices=["shadow", "live"])
    f = sub.add_parser("fetch"); f.add_argument("--days", type=int, default=30)
    b = sub.add_parser("backtest"); b.add_argument("--data", default="data")
    b.add_argument("--synthetic", action="store_true"); b.add_argument("--learn", action="store_true")
    s = sub.add_parser("selfcheck"); s.add_argument("--yes-place-test-order", action="store_true")
    for n in ("flatten", "safetytests", "params", "reset-kill", "report"):
        sub.add_parser(n)
    a = p.parse_args()
    setup_logging()
    cfg = Config.from_env()
    if getattr(a, "mode", None):
        cfg.mode = a.mode

    if a.cmd in ("run", "flatten"):
        from .bot import Bot
        from .safety import LiveBlocked
        try:
            bot = Bot(cfg)
        except LiveBlocked as e:
            print("⛔ حالت live مسدود است:\n - " + "\n - ".join(str(e).split(" | ")))
            sys.exit(2)
        if a.cmd == "run":
            bot.run()
        else:
            bot.exit_all(bot._books(), "manual_flatten"); bot.save()
    elif a.cmd == "fetch":
        import time
        from .exchange import Wallex
        from .backtest import save_csv, MANIFEST, REAL_SOURCE
        from .utils import iso
        w = Wallex(cfg.wallex_api_key)
        now = time.time()
        results = {}
        for sym in cfg.symbols:                      # ابتدا همه نمادها؛ فقط در صورت موفقیت همه، چیزی نوشته می‌شود
            try:
                results[sym] = w.fetch_history(sym, cfg.resolution, a.days, now=now)
            except Exception as e:
                sys.exit(f"❌ دریافت/اعتبارسنجی داده {sym} ناموفق: {e}\nداده واقعی ساخته نشد؛ هیچ CSV/manifest نوشته نمی‌شود.")
        man = {"source": REAL_SOURCE, "source_resolution_min": Wallex.SOURCE_RES, "resolution": cfg.resolution,
               "aggregated_client_side": True, "days": a.days, "fetched_at": now, "fetched_at_iso": iso(now),
               "validation": "ok: sorted, no duplicates, aligned, gap-free, only complete closed candles", "symbols": {}}
        for sym, (cs, meta) in results.items():
            save_csv(f"data/{sym}.csv", cs)
            man["symbols"][sym] = {k: meta[k] for k in ("candles", "minute_candles", "requests", "first_t", "last_t")}
            print(sym, len(cs), f"candles ({cfg.resolution}m from {meta['minute_candles']} x 1m)")
        json.dump(man, open(f"data/{MANIFEST}", "w"), indent=1)
    elif a.cmd == "backtest":
        from .backtest import run_backtest, synthetic, load_real_dataset, write_outputs, format_funnel
        if a.synthetic:
            data = {s: synthetic(seed=i + 1) for i, s in enumerate(cfg.symbols[:4])}
            rep, tr, cv, ev = run_backtest(data, cfg, learn=a.learn, label="SYNTHETIC_NOT_EVIDENCE")
            write_outputs("logs/synthetic", rep, tr, cv, ev)
            print("!! داده مصنوعی — فقط تست دود؛ هیچ ادعای سودآوری ندارد (خروجی: logs/synthetic/)")
        else:
            data, man = load_real_dataset(a.data, cfg.symbols)
            rep, tr, cv, ev = run_backtest(data, cfg, learn=a.learn, label=f"real:{man['source']}")
            write_outputs("logs/backtest_real" + ("_learn" if a.learn else ""), rep, tr, cv, ev)
        print(json.dumps({k: v for k, v in rep.items() if k not in ("funnel", "score_stats")}, indent=2, ensure_ascii=False))
        print(format_funnel(rep))
    elif a.cmd == "selfcheck":
        from .selfcheck import run_selfcheck
        rep = run_selfcheck(cfg, a.yes_place_test_order)
        for k, v in rep["checks"].items():
            print(("✅" if v["ok"] else "❌"), k, str(v["detail"])[:140])
        print("ALL PASSED" if rep["all_passed"] else "NOT PASSED — live همچنان مسدود است")
    elif a.cmd == "safetytests":
        from .safety import code_hash
        class R(unittest.TextTestResult):
            ok = []
            def addSuccess(self, t):
                super().addSuccess(t); R.ok.append(t._testMethodName)
        suite = unittest.defaultTestLoader.discover("tests", top_level_dir=".")
        res = unittest.TextTestRunner(verbosity=1, resultclass=R).run(suite)
        os.makedirs(cfg.log_dir, exist_ok=True)
        json.dump({"code_hash": code_hash(), "passed_tests": R.ok, "failures": len(res.failures),
                   "errors": len(res.errors), "skipped": len(res.skipped)},
                  open(os.path.join(cfg.log_dir, "safety_tests.json"), "w"), indent=1)
        sys.exit(0 if res.wasSuccessful() else 1)
    elif a.cmd == "params":
        d = {k: getattr(cfg, k) for k in Config.FINGERPRINT_FIELDS}
        print(json.dumps(d, indent=1, ensure_ascii=False))
        print("\nاعتبارسنجی:", cfg.validate() or "OK")
        print("WTB_MAX_DRAWDOWN صریح؟", "max_drawdown" in cfg.explicit, "(مقدار:", cfg.max_drawdown, ")")
        print("\nپس از تأیید این مقادیر با بریف رسمی، تنظیم کنید:  WTB_LIVE_ACK=" + cfg.fingerprint())
    elif a.cmd == "reset-kill":
        st = json.load(open(cfg.state_path)); st["risk"]["killed"] = False
        st["risk"]["peak"] = 0.0; json.dump(st, open(cfg.state_path, "w"), indent=1); print("kill-switch reset")
    elif a.cmd == "report":
        path = os.path.join(cfg.log_dir, "trades.csv")
        rows = list(csv.DictReader(open(path))) if os.path.exists(path) else []
        pnl = [float(x["pnl_tmn"]) for x in rows]
        print(f"trades={len(rows)} net_pnl={sum(pnl):,.0f} TMN wins={sum(1 for x in pnl if x > 0)}")


if __name__ == "__main__":
    main()
