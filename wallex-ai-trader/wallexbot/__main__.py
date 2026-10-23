from __future__ import annotations
import argparse
import csv
import json
import os
import sys
import unittest

from .config import Config
from .utils import setup_logging


def _wallex(cfg, timeout=15):
    from .exchange import Wallex
    return Wallex(cfg.wallex_api_key, api_key_header=cfg.api_key_header,
                  extra_headers=cfg.extra_headers, timeout=timeout)


def cmd_fetch(cfg, a):
    import time
    from .backtest import save_csv, MANIFEST, REAL_SOURCE
    from .exchange import Wallex
    from .utils import iso
    w = Wallex(cfg.wallex_api_key, api_key_header=cfg.api_key_header, extra_headers=cfg.extra_headers)
    now = time.time()
    results, failed = {}, {}
    for sym in a.symbols or cfg.symbols:
        try:
            results[sym] = w.fetch_history(sym, cfg.resolution, a.days, now=now)
        except Exception as e:                       # noqa: BLE001
            failed[sym] = str(e)[:300]
            if not a.allow_partial:
                sys.exit(f"❌ دریافت/اعتبارسنجی داده {sym} ناموفق: {e}\n"
                         "داده واقعی ساخته نشد؛ هیچ CSV/manifest نوشته نمی‌شود. "
                         "برای ادامه با نمادهای موفق: --allow-partial")
    if not results:
        sys.exit("❌ هیچ نمادی دریافت نشد؛ چیزی نوشته نشد.")
    man = {"source": REAL_SOURCE, "source_resolution_min": Wallex.SOURCE_RES, "resolution": cfg.resolution,
           "aggregated_client_side": True, "days": a.days, "fetched_at": now, "fetched_at_iso": iso(now),
           "validation": "ok: sorted, no duplicates, aligned, gap-free, only complete closed candles",
           "failed_symbols": failed, "symbols": {}}
    for sym, (cs, meta) in results.items():
        save_csv(os.path.join(cfg.data_dir, f"{sym}.csv"), cs)
        man["symbols"][sym] = {k: meta[k] for k in ("candles", "minute_candles", "requests", "first_t", "last_t")}
        print(sym, len(cs), f"candles ({cfg.resolution}m from {meta['minute_candles']} x 1m)")
    with open(os.path.join(cfg.data_dir, MANIFEST), "w", encoding="utf-8") as f:
        json.dump(man, f, indent=1)
    if failed:
        print("⚠️ نمادهای ناموفق (در manifest ثبت شد):", ", ".join(failed))


def cmd_backtest(cfg, a):
    from .backtest import run_backtest, synthetic, load_real_dataset, write_outputs, format_funnel
    gate = None
    if a.ai_gate:
        from .ai_agent import AIAgent
        agent = AIAgent(cfg)
        if not agent.enabled:
            sys.exit("❌ --ai-gate نیازمند کلید AI معتبر است (WTB_AI_API_KEY یا ANTHROPIC_API_KEY/OPENAI_API_KEY).")
        gate = lambda sig, ctx: agent.decide(sig, ctx)  # noqa: E731
    if a.synthetic:
        data = {s: synthetic(seed=i + 1) for i, s in enumerate(cfg.symbols[:4])}
        rep, tr, cv, ev = run_backtest(data, cfg, learn=a.learn, label="SYNTHETIC_NOT_EVIDENCE", ai_gate=gate)
        out = "logs/synthetic"
        print("!! داده مصنوعی — فقط تست دود؛ هیچ ادعای سودآوری ندارد")
    else:
        data, man = load_real_dataset(cfg.data_dir, cfg.symbols)
        rep, tr, cv, ev = run_backtest(data, cfg, learn=a.learn, label=f"real:{man['source']}", ai_gate=gate)
        out = "logs/backtest_real" + ("_learn" if a.learn else "") + ("_ai" if gate else "")
    write_outputs(out, rep, tr, cv, ev)
    print(json.dumps({k: v for k, v in rep.items() if k not in ("funnel", "score_stats")}, indent=2, ensure_ascii=False))
    print(format_funnel(rep))
    print(f"\nخروجی: {out}/")


def cmd_report(cfg):
    path = os.path.join(cfg.log_dir, "trades.csv")
    rows = list(csv.DictReader(open(path, encoding="utf-8"))) if os.path.exists(path) else []
    pnl = [float(x["pnl_tmn"]) for x in rows]
    fees = [float(x.get("cost_tmn") or 0) for x in rows]
    only_pnl = sum(pnl)
    print(f"trades={len(rows)} realized_pnl={only_pnl:,.0f} TMN wins={sum(1 for x in pnl if x > 0)} "
          f"losses={sum(1 for x in pnl if x <= 0)}")
    if rows:
        print(f"return_vs_{cfg.initial_capital:,.0f}={100 * only_pnl / cfg.initial_capital:.2f}%")
    st = {}
    if os.path.exists(cfg.state_path):
        with open(cfg.state_path, encoding="utf-8") as f:
            st = json.load(f)
    pos = st.get("positions") or {}
    print(f"open_positions={list(pos)} last_equity={st.get('last_equity')} stats={st.get('stats')}")
    flat = st.get("flat_check")
    if flat:
        print("flat_check:", flat)


def cmd_account(cfg):
    from .schemas import SchemaError
    if not cfg.wallex_api_key:
        sys.exit("❌ WALLEX_API_KEY تنظیم نشده است (در .env یا محیط).")
    w = _wallex(cfg)
    out = {}
    try:
        prof = w.profile()
        meta = prof.get("meta") if isinstance(prof.get("meta"), dict) else {}
        out["profile"] = {
            "tracking_id": prof.get("tracking_id"), "verification": prof.get("verification"),
            "commission": prof.get("commission"), "meta_keys": sorted(meta)[:30],
            "identifiers": {k: meta[k] for k in meta if any(x in k.lower() for x in ("sub", "client", "account", "team", "wallet"))},
        }
    except Exception as e:                          # noqa: BLE001
        out["profile"] = f"{type(e).__name__}: {e}"
    try:
        out["balances"] = {a: {"total": round(b.total, 8), "free": round(b.free, 8)} for a, b in w.balances().items() if b.total > 0}
    except Exception as e:                          # noqa: BLE001
        out["balances"] = f"{type(e).__name__}: {e}"
    try:
        fees = w.fee()
        out["fee"] = {"markets": len(fees), "taker_max": max(v["taker"] for v in fees.values())}
    except Exception as e:                          # noqa: BLE001
        out["fee"] = f"{type(e).__name__}: {e}"
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print("\nاگر هدر مخصوص حساب فرعی لازم است: WTB_EXTRA_HEADERS='{\"X-Sub-Account\":\"...\"}'")


def cmd_doctor(cfg):
    from .safety import LiveGate, code_hash, schemas_hash
    checks = []

    def add(name, ok, detail=""):
        checks.append((name, bool(ok), str(detail)[:200]))

    errs = cfg.validate()
    add("config", not errs, "; ".join(errs) or "OK")
    add("wallex key", bool(cfg.wallex_api_key), "set" if cfg.wallex_api_key else "missing")
    add("AI key", bool(cfg.ai_key()), f"{cfg.ai_provider}/{cfg.ai_model}" if cfg.ai_key() else "missing (rules-only)")
    add("challenge end", bool(cfg.challenge_end), cfg.challenge_end or "not set")
    add("state file", os.path.exists(cfg.state_path), cfg.state_path)
    add("trades log", os.path.exists(os.path.join(cfg.log_dir, "trades.csv")), os.path.join(cfg.log_dir, "trades.csv"))
    man = os.path.join(cfg.data_dir, "manifest.json")
    add("real data manifest", os.path.exists(man), man)
    add("schemas hash", True, schemas_hash())
    add("code hash", True, code_hash())
    try:
        w = _wallex(cfg, timeout=12)
        mk = w.markets(max_age=0)
        add("public API", True, f"{len(mk)} markets")
        if cfg.wallex_api_key:
            prof = w.profile()
            add("private API", True, f"tracking_id={prof.get('tracking_id')}")
    except Exception as e:                          # noqa: BLE001
        add("public API", False, f"{type(e).__name__}: {e}")
    fails = LiveGate(cfg).failures()
    add("live gate", not fails, "; ".join(fails[:4]) or "open")
    for name, ok, detail in checks:
        print(("✅" if ok else "❌"), f"{name:22s}", detail)


def main():
    p = argparse.ArgumentParser(prog="wallexbot", description="Wallex AI Trader — agentic spot trading engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="اجرای ربات shadow/live")
    r.add_argument("--mode", choices=["shadow", "live"])
    f = sub.add_parser("fetch", help="دریافت کندل ۱ دقیقه‌ای واقعی و تجمیع")
    f.add_argument("--days", type=int, default=30)
    f.add_argument("--symbols", default="", help="فهرست جدا با کاما (پیش‌فرض: WTB_SYMBOLS)")
    f.add_argument("--allow-partial", action="store_true")
    b = sub.add_parser("backtest", help="بک‌تست روی دادهٔ واقعی/مصنوعی")
    b.add_argument("--data", default="data")
    b.add_argument("--synthetic", action="store_true")
    b.add_argument("--learn", action="store_true")
    b.add_argument("--ai-gate", action="store_true", help="AI در حلقه (همان مسیر live؛ مصرف API)")
    s = sub.add_parser("selfcheck", help="اعتبارسنجی API روی حساب واقعی")
    s.add_argument("--yes-place-test-order", action="store_true")
    helps = {"flatten": "تسویهٔ کامل پوزیشن‌های باز", "safetytests": "اجرای تست‌های ایمنی + هش کد",
             "params": "نمایش پارامترها و اثرانگشت ACK", "reset-kill": "برداشتن Kill-Switch از state",
             "report": "خلاصهٔ معاملات ثبت‌شده", "account": "پروفایل/موجودی/کارمزد حساب واقعی",
             "doctor": "بررسی سلامت پیکربندی و دروازهٔ live"}
    for n, h in helps.items():
        sub.add_parser(n, help=h)
    rp = sub.add_parser("replay", help="Replay سریع روی دادهٔ ضبط‌شده (بدون UI)")
    rp.add_argument("--speed", type=float, default=5000)
    rp.add_argument("--loop", action="store_true")
    rp.add_argument("--log-dir", default="logs/replay_cli")
    m = sub.add_parser("mcp", help="کلاینت MCP والکس")
    m.add_argument("--url", default="")
    m.add_argument("--list", action="store_true")
    m.add_argument("--call", default="")
    m.add_argument("--args", default="{}")
    t = sub.add_parser("tune", help="تیون walk-forward پارامترها (بدون اعمال خودکار)")
    t.add_argument("--split", type=float, default=0.7)
    t.add_argument("--grid", choices=["quick", "full"], default="quick")
    t.add_argument("--min-trades", type=int, default=2)
    t.add_argument("--max-combos", type=int, default=60)
    t.add_argument("--synthetic", action="store_true")
    pr = sub.add_parser("profiles", help="پروفایل‌های آمادهٔ ریسک")
    pr.add_argument("--apply", default="", help="نام پروفایل برای نوشتن در .env")
    pr.add_argument("--write", action="store_true", help="نوشتن در .env (بدون این فلگ فقط نشان می‌دهد)")
    u = sub.add_parser("ui", help="پنل کنترل وب")
    u.add_argument("--host", default="127.0.0.1")
    u.add_argument("--port", type=int, default=8800)
    u.add_argument("--demo", action="store_true")
    u.add_argument("--read-only", action="store_true")
    u.add_argument("--token", default="")
    u.add_argument("--autostart", action="store_true")
    u.add_argument("--speed", type=float, default=600)
    a = p.parse_args()
    setup_logging()
    cfg = Config.from_env()
    if getattr(a, "mode", None):
        cfg.mode = a.mode
    if getattr(a, "symbols", ""):
        a.symbols = [x.strip() for x in a.symbols.split(",") if x.strip()]
    else:
        a.symbols = []
    if getattr(a, "data", None):
        cfg.data_dir = a.data

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
            bot.exit_all(bot._books(), "manual_flatten")
            bot.save()
    elif a.cmd == "fetch":
        cmd_fetch(cfg, a)
    elif a.cmd == "backtest":
        cmd_backtest(cfg, a)
    elif a.cmd == "replay":
        import time
        from .replay import ReplaySession, load_replay_data
        data, label = load_replay_data(cfg.data_dir, cfg.symbols)
        sess = ReplaySession(cfg, data, label, speed=a.speed, loop=a.loop, log_dir=a.log_dir)
        sess.start()
        print(f"replay: {label} · سرعت {a.speed}x · {len(data)} نماد · Ctrl+C برای توقف")
        try:
            while True:
                snap = sess.snapshot()
                print(json.dumps(snap, ensure_ascii=False), flush=True)
                if snap["status"] in ("finished", "stopped", "error") and not a.loop:
                    break
                time.sleep(max(1.0, 5.0))
        except KeyboardInterrupt:
            pass
        finally:
            sess.stop()
        if sess.bot is not None:
            st = sess.bot.status()
            print(json.dumps({k: st[k] for k in ("equity", "return_pct", "trades", "wins", "losses", "realized_pnl")},
                             ensure_ascii=False))
            flat = sess.bot.verify_flat()
            print("flat_check:", json.dumps(flat, ensure_ascii=False))
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
                super().addSuccess(t)
                R.ok.append(t._testMethodName)
        from .safety import ROOT as PROJECT_ROOT
        suite = unittest.defaultTestLoader.discover(os.path.join(PROJECT_ROOT, "tests"), top_level_dir=PROJECT_ROOT)
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
        if not os.path.exists(cfg.state_path):
            sys.exit(f"state file not found: {cfg.state_path}")
        with open(cfg.state_path, encoding="utf-8") as f:
            st = json.load(f)
        st.setdefault("risk", {})["killed"] = False
        st["risk"]["peak"] = float(st.get("last_equity") or cfg.initial_capital)
        with open(cfg.state_path, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1)
        print("kill-switch reset (peak = current equity)")
    elif a.cmd == "report":
        cmd_report(cfg)
    elif a.cmd == "account":
        cmd_account(cfg)
    elif a.cmd == "doctor":
        cmd_doctor(cfg)
    elif a.cmd == "mcp":
        from .mcp import main_list, main_call
        url = a.url or cfg.mcp_url
        if a.call:
            try:
                args = json.loads(a.args)
            except ValueError as e:
                sys.exit(f"--args JSON نامعتبر: {e}")
            sys.exit(main_call(url, a.call, args))
        sys.exit(main_list(url))
    elif a.cmd == "tune":
        from .backtest import load_real_dataset, synthetic
        from .tune import run_tuning, format_tuning, QUICK, FULL
        if a.synthetic:
            data = {s: synthetic(1500, seed=i + 1) for i, s in enumerate(cfg.symbols[:4])}
            label = "SYNTHETIC_NOT_EVIDENCE"
        else:
            data, man = load_real_dataset(cfg.data_dir, cfg.symbols)
            label = f"real:{man.get('source')}"
        rep = run_tuning(data, cfg, split=a.split, grid=(FULL if a.grid == "full" else QUICK),
                         min_trades=a.min_trades, max_combos=a.max_combos, label=label)
        print("\n" + format_tuning(rep))
        print("\nخروجی کامل: logs/tuning/tuning_report.json")
    elif a.cmd == "profiles":
        import dataclasses
        from .profiles import PROFILES
        for name, prof in PROFILES.items():
            print(f"\n[{name}] {prof['title']}\n  {prof['note']}")
            print("  " + "  ".join(f"{k}={v}" for k, v in prof["params"].items()))
        if a.apply:
            if a.apply not in PROFILES:
                sys.exit(f"پروفایل ناشناخته: {a.apply}")
            p = PROFILES[a.apply]["params"]
            test = dataclasses.replace(cfg, **p)
            errs = test.validate()
            if errs:
                sys.exit("پروفایل با تنظیمات فعلی سازگار نیست: " + "; ".join(errs))
            values = {"WTB_" + k.upper(): v for k, v in p.items()}
            if not a.write:
                print("\nبرای نوشتن در .env:  python -m wallexbot profiles --apply "
                      f"{a.apply} --write")
                for k, v in values.items():
                    print(f"  {k}={v}")
            else:
                from .config import save_env
                path = save_env(os.path.join(os.getcwd(), ".env"), values)
                print(f"\nنوشته شد: {path} — سپس params/safetytests/ACK را دوباره اجرا کنید.")
    elif a.cmd == "ui":
        from .web import serve
        serve(a.host, a.port, a.demo, a.read_only, a.token, True if a.autostart else None, a.speed)


if __name__ == "__main__":
    main()
