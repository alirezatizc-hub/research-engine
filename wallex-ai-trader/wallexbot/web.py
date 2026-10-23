"""پنل کنترل وب (بدون dependency اضافه) — ThreadingHTTPServer + JSON API + UI استاتیک.

اجرا:
    python -m wallexbot ui                      # http://127.0.0.1:8800
    python -m wallexbot ui --host 0.0.0.0       # برای دسترسی از شبکه/کانتینر
    python -m wallexbot ui --demo               # فقط replay روی دادهٔ ضبط‌شده، بدون کلید

امنیت:
- پیش‌فرض فقط روی loopback گوش می‌دهد.
- POSTها به هدر X-Requested-With نیاز دارند (ضد CSRF).
- اگر --token بدهید، همهٔ /apiها توکن می‌خواهند.
- کلیدهای API هرگز به مرورگر برنمی‌گردند (فقط ماسک).
"""
from __future__ import annotations

import csv
import dataclasses
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Config, save_env

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")
ALLOWED_MODES = ("shadow", "live", "replay")


def _now_iso():
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def tail_jsonl(path, limit=200, action=None):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            rows = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if action and obj.get("action") != action:
                    continue
                rows.append(obj)
            return rows[-limit:]
    except OSError:
        return []


def read_trades(path, limit=500):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    out = []
    for r in rows[-limit:]:
        r = {k: v for k, v in r.items() if k is not None}
        r.setdefault("open_ts", r.get("entry_t") or "")
        r.setdefault("close_ts", r.get("exit_t") or "")
        r.setdefault("exit_reason", r.get("reason") or "")
        r.setdefault("qty", "")
        for k in ("entry", "exit", "qty", "cost_tmn", "proceeds_tmn", "pnl_tmn", "pnl_pct"):
            if r.get(k) not in (None, ""):
                try:
                    r[k] = float(r[k])
                except (TypeError, ValueError):
                    pass
        out.append(r)
    return out


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_csv_curve(path):
    if not os.path.exists(path):
        return []
    pts = []
    try:
        with open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    pts.append([float(r["t"]), float(r["equity"])])
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        pass
    return pts


def status_from_state(cfg, state):
    st = state or {}
    positions = []
    prices = st.get("last_prices", {})
    for s, p in (st.get("positions") or {}).items():
        px = prices.get(s, p.get("last_px") or p.get("entry") or 0)
        positions.append({"symbol": s, "qty": p.get("qty"), "entry": p.get("entry"), "last": px,
                          "stop": p.get("stop"), "take_profit": p.get("tp"), "kind": p.get("kind"),
                          "pnl_pct": round(100 * (px / p["entry"] - 1), 3) if p.get("entry") else 0,
                          "value_tmn": round((p.get("qty") or 0) * px), "age_min": None})
    stats = st.get("stats", {})
    equity = st.get("last_equity") or cfg.initial_capital
    risk = st.get("risk", {})
    hb_age = round(time.time() - st["heartbeat"], 1) if st.get("heartbeat") else None
    return {
        "mode": st.get("mode") or cfg.mode,
        "running": bool(st.get("running")) and (hb_age is not None and hb_age < 120),
        "heartbeat_age_s": hb_age,
        "equity": round(equity), "cash": round(st["last_cash"]) if st.get("last_cash") is not None else None,
        "initial_capital": cfg.initial_capital,
        "return_pct": round(100 * (equity / cfg.initial_capital - 1), 3) if cfg.initial_capital else None,
        "realized_pnl": round(float(stats.get("realized_pnl", 0.0))),
        "unrealized_pnl": round(sum((p["value_tmn"] - ((p.get("qty") or 0) * (p.get("entry") or 0))) for p in positions)),
        "fees_paid": round(float(stats.get("fees_paid", 0.0))), "trades": int(stats.get("trades", 0)),
        "wins": int(stats.get("wins", 0)), "losses": int(stats.get("losses", 0)),
        "trades_today": int(stats.get("trades_today", 0)), "positions": positions,
        "risk": {"killed": bool(risk.get("killed")), "ai_mode": risk.get("ai_mode", "normal"),
                 "peak": round(risk.get("peak", 0.0)),
                 "drawdown_pct": round(100 * (1 - equity / risk["peak"]), 3) if risk.get("peak") else 0.0},
        "learner": {"overrides": st.get("learner", {}).get("overrides", {})},
        "challenge_end": cfg.challenge_end,
        "hours_left": round(cfg.hours_left(), 2) if cfg.challenge_end else None,
        "market_ok": None, "allowed": cfg.symbols, "halted": None, "last_scan": st.get("last_scan"),
        "ai": {"enabled": bool(cfg.ai_enabled and cfg.ai_key()), "provider": cfg.ai_provider, "model": cfg.ai_model},
        "started_at": st.get("started_at"), "updated_at": st.get("heartbeat") and
        datetime.fromtimestamp(st["heartbeat"], tz=timezone.utc).isoformat(timespec="seconds"),
        "curve": st.get("curve", []),
    }


class BotRunner:
    def __init__(self, cfg: Config):
        self.base = cfg
        self.bot = None
        self.thread = None
        self.stop_evt = threading.Event()
        self.error = None
        self.mode = None

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, mode):
        if self.running:
            raise RuntimeError("ربات همین حالا در حال اجراست")
        from .bot import Bot
        from .safety import LiveGate
        cfg = dataclasses.replace(self.base, mode=mode)
        if mode == "live":
            LiveGate(cfg).assert_open()
        self.bot = Bot(cfg)
        self.stop_evt = threading.Event()
        self.error = None
        self.mode = mode
        self.thread = threading.Thread(target=self._target, name="bot", daemon=True)
        self.thread.start()
        return True

    def _target(self):
        try:
            self.bot.run(should_stop=self.stop_evt.is_set)
        except Exception as e:                  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            traceback.print_exc()

    def stop(self, wait=20):
        self.stop_evt.set()
        if self.thread:
            self.thread.join(timeout=wait)
        return not self.running

    def flatten(self):
        if not self.bot:
            return {"ok": False, "error": "رباتی در حال اجرا نیست"}
        if self.running:
            self.bot.flatten_requested = True
            return {"ok": True, "queued": True, "note": "درخواست تسویه در گام بعدی ربات اجرا می‌شود"}
        return {"ok": True, "result": self.bot.exit_all(self.bot._books(), "manual_flatten")}

    def status(self):
        if self.bot is not None:
            st = self.bot.status()
            st["error"] = self.error
            st["thread_alive"] = self.running
            return st
        return None


class App:
    def __init__(self, cfg: Config, demo=False, read_only=False, token="", autostart_speed=900):
        self.cfg = cfg
        self.demo = demo
        self.read_only = read_only
        self.token = token or ""
        self.runner = BotRunner(cfg)
        self.replay = None
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        self.started_at = time.time()
        self.autostart_speed = autostart_speed
        self._gate_cache = (0.0, None)
        # چرخهٔ بهینه‌سازی (اجرای پس‌زمینه + پیشرفت زنده)
        self._opt_lock = threading.Lock()
        self._opt_stop = threading.Event()
        self.opt = {"running": False, "run_id": None, "stage": "", "done": 0, "total": 0,
                    "detail": "", "started": None, "ended": None, "error": None, "proposal": None}

    # ------------------------------------------------------------ helpers
    def fresh_cfg(self):
        return Config.from_env()

    def project_path(self, *p):
        return os.path.join(ROOT, *p)

    def body_status(self):
        st = self.runner.status()
        if st:
            st["source"] = "runner"
            return st
        if self.replay and self.replay.bot is not None:
            st = self.replay.bot.status()
            st["source"] = "replay"
            st["replay"] = self.replay.snapshot()
            return st
        state = read_json(self.cfg.state_path)
        st = status_from_state(self.cfg, state)
        st["source"] = "state_file" if state else "empty"
        st["state_exists"] = bool(state)
        trades = read_trades(self.project_path(self.cfg.log_dir, "trades.csv"), limit=2000)
        st["trades"] = len(trades)
        st["realized_pnl"] = round(sum(float(t.get("pnl_tmn") or 0) for t in trades))
        st["curve"] = (read_csv_curve(self.project_path("logs", "backtest_real", "backtest_equity.csv")) or [])[-1500:]
        return st

    def start_replay(self, speed=900, loop=True):
        if self.replay is not None:
            self.replay.stop()
        from .replay import ReplaySession, load_replay_data
        data, label = load_replay_data(self.project_path(self.cfg.data_dir), self.cfg.symbols)
        cfg = dataclasses.replace(self.cfg, mode="shadow")
        demo_profile = False
        if self.demo:
            # پروفایل دمو: فقط برای نمایش زندهٔ چرخهٔ کامل روی همین داده؛ در تنظیمات ذخیره نمی‌شود.
            cfg = dataclasses.replace(
                cfg, rules_min_score=min(cfg.rules_min_score, 55.0), min_score=min(cfg.min_score, 55.0),
                min_atr_pct=min(cfg.min_atr_pct, 0.002), rsi_buy=max(cfg.rsi_buy, 45.0),
                bb_max_pos=max(cfg.bb_max_pos, 0.3), max_trades_per_day=max(cfg.max_trades_per_day, 50))
            demo_profile = True
        self.replay = ReplaySession(cfg, data, label, speed=speed, loop=loop,
                                    log_dir=self.project_path("logs", "replay"))
        self.replay.demo_profile = demo_profile
        self.replay.start()
        return {"ok": True, "label": label, "speed": speed, "loop": loop, "demo_profile": demo_profile}

    def job(self, name, fn):
        jid = uuid.uuid4().hex[:10]
        with self.jobs_lock:
            self.jobs[jid] = {"id": jid, "name": name, "state": "running", "started": time.time()}
        def run():
            try:
                res = fn()
                with self.jobs_lock:
                    self.jobs[jid].update(state="done", result=res, ended=time.time())
            except Exception as e:              # noqa: BLE001
                with self.jobs_lock:
                    self.jobs[jid].update(state="error", error=f"{type(e).__name__}: {e}", ended=time.time())
        threading.Thread(target=run, daemon=True).start()
        return self.jobs[jid]

    # ------------------------------------------------------------ actions
    def gate_failures(self, max_age=20):
        now = time.time()
        ts, cached = self._gate_cache
        if cached is not None and now - ts < max_age:
            return cached
        from .safety import LiveGate
        try:
            fails = LiveGate(self.fresh_cfg()).failures()
        except Exception as e:                  # noqa: BLE001
            fails = [f"gate evaluation failed: {e}"]
        self._gate_cache = (now, fails)
        return fails

    def test_connection(self):
        cfg = self.fresh_cfg()
        if not cfg.wallex_api_key:
            return {"ok": False, "error": "WALLEX_API_KEY تنظیم نشده است"}
        from .exchange import Wallex
        w = Wallex(cfg.wallex_api_key, api_key_header=cfg.api_key_header, extra_headers=cfg.extra_headers, timeout=15)
        out = {"ok": True, "steps": {}}
        prof = None
        try:
            mk = w.markets(max_age=0)
            out["steps"]["markets"] = {"ok": True, "count": len(mk),
                                      "symbols_ok": [s for s in cfg.symbols if s in mk],
                                      "symbols_missing": [s for s in cfg.symbols if s not in mk]}
        except Exception as e:                  # noqa: BLE001
            out["ok"] = False
            out["steps"]["markets"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        try:
            prof = w.profile()
            meta = prof.get("meta") if isinstance(prof.get("meta"), dict) else {}
            out["steps"]["profile"] = {"ok": True, "tracking_id": prof.get("tracking_id"),
                                       "verification": prof.get("verification"),
                                       "meta_keys": sorted(meta)[:20],
                                       "identifiers": {k: meta[k] for k in meta
                                                       if any(x in k.lower() for x in ("sub", "client", "account", "team"))}}
        except Exception as e:                  # noqa: BLE001
            out["ok"] = False
            out["steps"]["profile"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        try:
            bal = w.balances()
            out["steps"]["balances"] = {"ok": True,
                                        "assets": {a: {"total": round(b.total, 8), "free": round(b.free, 8)}
                                                   for a, b in bal.items() if b.total > 0}}
        except Exception as e:                  # noqa: BLE001
            out["steps"]["balances"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        try:
            fee = w.fee()
            out["steps"]["fee"] = {"ok": True, "taker_max": max(v["taker"] for v in fee.values()),
                                   "maker_max": max(v["maker"] for v in fee.values()), "markets": len(fee)}
        except Exception as e:                  # noqa: BLE001
            out["steps"]["fee"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        return out

    def test_ai(self):
        cfg = self.fresh_cfg()
        from .ai_agent import AIAgent
        return AIAgent(cfg).healthcheck()

    def run_backtest(self, synthetic=False, learn=False, ai_gate=False):
        from .backtest import (format_funnel, load_real_dataset, run_backtest, synthetic as synth, write_outputs)
        cfg = self.fresh_cfg()
        if synthetic:
            data = {s: synth(seed=i + 1) for i, s in enumerate(cfg.symbols[:4])}
            label = "SYNTHETIC_NOT_EVIDENCE"
            out_dir = self.project_path("logs", "synthetic_web")
        else:
            data, man = load_real_dataset(self.project_path(cfg.data_dir), cfg.symbols)
            label = f"real:{man.get('source')}"
            out_dir = self.project_path("logs", "backtest_web")
        gate = None
        if ai_gate:
            from .ai_agent import AIAgent
            agent = AIAgent(cfg)
            if not agent.enabled:
                raise RuntimeError("AI gate درخواست شد ولی کلید/مدل AI تنظیم نشده است")
            gate = lambda sig, ctx: agent.decide(sig, ctx)  # noqa: E731
        rep, tr, cv, ev = run_backtest(data, cfg, learn=learn, label=label, ai_gate=gate)
        write_outputs(out_dir, rep, tr, cv, ev)
        return {"report": rep, "funnel": format_funnel(rep), "dir": out_dir, "trades": len(tr)}

    # ------------------------------------------------------------ optimizer
    def optimizer_state(self):
        from .optimizer import list_runs
        with self._opt_lock:
            st = dict(self.opt)
        st["runs"] = list_runs(self.project_path())
        st["ai_available"] = bool(self.cfg.ai_key() and self.cfg.ai_enabled)
        st["data_ready"] = os.path.exists(self.project_path(self.cfg.data_dir, "manifest.json"))
        st["demo"] = self.demo
        st["read_only"] = self.read_only
        return st

    def start_optimizer(self, *, synthetic=False, ai=False, rounds=2, min_trades=2, purge=8,
                        split=(0.6, 0.2, 0.2)):
        with self._opt_lock:
            if self.opt["running"]:
                raise RuntimeError("چرخهٔ بهینه‌سازی همین حالا در حال اجراست")
            self.opt = {"running": True, "run_id": None, "stage": "start", "done": 0, "total": 1,
                        "detail": "آماده‌سازی", "started": time.time(), "ended": None, "error": None,
                        "proposal": None}
        self._opt_stop = threading.Event()
        rounds = max(1, min(4, int(rounds or 2)))
        run_id = "opt-" + datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        with self._opt_lock:
            self.opt["run_id"] = run_id

        def progress(stage, done, total, detail=""):
            with self._opt_lock:
                self.opt.update(stage=stage, done=done, total=total, detail=detail)

        def work():
            from . import optimizer as O
            try:
                cfg = self.fresh_cfg()
                if synthetic:
                    from .backtest import synthetic as synth
                    data = {s: synth(1500, seed=i + 1) for i, s in enumerate(cfg.symbols[:4])}
                    label = "SYNTHETIC_NOT_EVIDENCE"
                else:
                    from .backtest import load_real_dataset
                    data, man = load_real_dataset(self.project_path(cfg.data_dir), cfg.symbols)
                    label = f"real:{man.get('source')}"
                ai_obj = None
                if ai:
                    from .ai_agent import AIAgent
                    agent = AIAgent(cfg)
                    if not agent.enabled:
                        raise RuntimeError("کلید/مدل AI تنظیم نشده است (یا AI خاموش است)")
                    ai_obj = agent
                rep = O.run_optimizer(data, cfg, data_label=label, split=split, rounds=rounds,
                                      min_trades=max(1, int(min_trades)), purge_bars=int(purge),
                                      ai=ai_obj, progress=progress, should_stop=self._opt_stop.is_set,
                                      run_id=run_id, out_dir=self.project_path(O.OUT_ROOT, run_id))
                with self._opt_lock:
                    self.opt.update(running=False, ended=time.time(), stage="done",
                                    proposal=rep.get("proposal"), status=rep.get("status"))
            except O.OptimizerStopped:
                with self._opt_lock:
                    self.opt.update(running=False, ended=time.time(), stage="stopped",
                                    detail="توسط کاربر متوقف شد", error=None)
            except Exception as e:              # noqa: BLE001 — خطا به UI برمی‌گردد، سرور نمی‌افتد
                with self._opt_lock:
                    self.opt.update(running=False, ended=time.time(), stage="error",
                                    error=f"{type(e).__name__}: {e}"[:400])

        threading.Thread(target=work, name="optimizer", daemon=True).start()
        return {"run_id": run_id, "rounds": rounds, "synthetic": bool(synthetic)}

    def stop_optimizer(self):
        self._opt_stop.set()
        with self._opt_lock:
            self.opt["detail"] = "درخواست توقف ثبت شد"
        return {"ok": True}

    def approve_optimizer(self, run_id, confirm=False, force=False):
        from . import optimizer as O
        return O.apply_proposal(run_id, root=self.project_path(), confirm=confirm, force=force,
                                env_path=os.path.join(os.getcwd(), ".env"),
                                log_dir=self.project_path("logs"), cfg=self.fresh_cfg())

    def run_tests(self, safety=False):
        cmd = ([sys.executable, "-m", "wallexbot", "safetytests"] if safety else
               [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."])
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300, env={**os.environ, "PYTHONPATH": ROOT})
        return {"returncode": p.returncode, "stdout": p.stdout[-8000:], "stderr": p.stderr[-4000:]}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WallexBotUI/1.0"
        protocol_version = "HTTP/1.1"

        # ---- low level
        def _json(self, code, obj, extra_headers=None):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path, ctype):
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                return self._json(404, {"ok": False, "error": "not found"})
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _auth_ok(self, parsed):
            if not app.token:
                return True
            q = parse_qs(parsed.query or "")
            tok = (q.get("token") or [""])[0] or self.headers.get("X-UI-Token", "")
            return hmac.compare_digest(str(tok), str(app.token))

        def _read_body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n > 1_000_000:
                raise ValueError("body too large")
            raw = self.rfile.read(n) if n else b"{}"
            try:
                return json.loads(raw or b"{}")
            except ValueError as e:
                raise ValueError(f"invalid JSON body: {e}")

        def log_message(self, fmt, *args):      # کم‌صدا
            if os.getenv("WTB_UI_VERBOSE"):
                super().log_message(fmt, *args)

        # ---- GET
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path in ("/", "/index.html"):
                    return self._file(os.path.join(UI_DIR, "index.html"), "text/html; charset=utf-8")
                if path == "/app.js":
                    return self._file(os.path.join(UI_DIR, "app.js"), "application/javascript; charset=utf-8")
                if path == "/style.css":
                    return self._file(os.path.join(UI_DIR, "style.css"), "text/css; charset=utf-8")
                if path == "/favicon.svg":
                    return self._file(os.path.join(UI_DIR, "favicon.svg"), "image/svg+xml")
                if not path.startswith("/api/"):
                    return self._json(404, {"ok": False, "error": "not found"})
                if not self._auth_ok(parsed):
                    return self._json(401, {"ok": False, "error": "unauthorized"})
                return self.api_get(path, parse_qs(parsed.query or ""))
            except Exception as e:              # noqa: BLE001
                traceback.print_exc()
                return self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

        def api_get(self, path, q):
            if path == "/api/health":
                return self._json(200, {"ok": True, "ts": _now_iso(), "demo": app.demo, "read_only": app.read_only,
                                        "uptime_s": round(time.time() - app.started_at), "version": "1.1"})
            if path == "/api/status":
                st = app.body_status()
                st["gate_failures"] = app.gate_failures()
                st["config"] = {k: v for k, v in app.cfg.public().items()
                                if k in ("mode", "symbols", "quote_asset", "resolution", "challenge_end",
                                         "initial_capital", "max_drawdown", "daily_loss_limit", "max_positions",
                                         "max_pos_frac", "risk_per_trade", "max_trades_per_day",
                                         "min_score", "rules_min_score", "min_atr_pct", "rsi_buy",
                                         "ai_provider", "ai_model", "mcp_enabled", "fingerprint")}
                st["config"]["ai_key_present"] = bool(app.cfg.ai_key())
                st["config"]["wallex_key_present"] = bool(app.cfg.wallex_api_key)
                return self._json(200, {"ok": True, "status": st})
            if path == "/api/trades":
                limit = int((q.get("limit") or ["300"])[0])
                log_dir = app.replay.log_dir if (app.replay and app.replay.bot is not None) else app.cfg.log_dir
                tr = read_trades(app.project_path(log_dir, "trades.csv"), limit=limit)
                return self._json(200, {"ok": True, "trades": tr[-limit:], "count": len(tr),
                                        "source": "replay" if log_dir != app.cfg.log_dir else "live"})
            if path == "/api/decisions":
                limit = min(int((q.get("limit") or ["200"])[0]), 2000)
                action = (q.get("action") or [None])[0]
                if app.replay and app.replay.bot is not None:
                    rows = tail_jsonl(os.path.join(app.replay.log_dir, "decisions.jsonl"), limit=limit, action=action)
                else:
                    rows = tail_jsonl(app.project_path(app.cfg.log_dir, "decisions.jsonl"), limit=limit, action=action)
                return self._json(200, {"ok": True, "decisions": rows, "count": len(rows)})
            if path == "/api/backtest":
                name = (q.get("name") or ["real"])[0]
                d = {"real": "backtest_real", "learn": "backtest_real_learn", "web": "backtest_web",
                     "synthetic": "synthetic_web"}.get(name, "backtest_real")
                base = app.project_path("logs", d)
                rep = read_json(os.path.join(base, "backtest_report.json"))
                if not rep:
                    return self._json(404, {"ok": False, "error": f"گزارش {name} موجود نیست؛ ابتدا بک‌تست را اجرا کنید"})
                return self._json(200, {"ok": True, "name": name, "report": rep,
                                        "curve": read_csv_curve(os.path.join(base, "backtest_equity.csv")),
                                        "trades": read_trades(os.path.join(base, "backtest_trades.csv"), limit=1000)})
            if path == "/api/gate":
                return self._json(200, {"ok": True, "failures": app.gate_failures(),
                                        "explicit": sorted(app.cfg.explicit)})
            if path == "/api/settings":
                cfg = app.cfg.public()
                return self._json(200, {"ok": True, "settings": cfg, "demo": app.demo,
                                        "dotenv": os.path.join(os.getcwd(), ".env"), "read_only": app.read_only})
            if path == "/api/jobs":
                with app.jobs_lock:
                    return self._json(200, {"ok": True, "jobs": list(app.jobs.values())[-20:]})
            if path.startswith("/api/jobs/"):
                jid = path.rsplit("/", 1)[-1]
                with app.jobs_lock:
                    j = app.jobs.get(jid)
                return self._json(200 if j else 404, {"ok": bool(j), "job": j})
            if path == "/api/mcp/tools":
                cfg = app.fresh_cfg()
                from .mcp import WallexMCP
                c = WallexMCP(cfg.mcp_url, timeout=cfg.mcp_timeout_s)
                info = c.probe_market_tools()
                return self._json(200, {"ok": True, **info})
            if path in ("/api/optimizer", "/api/optimizer/status"):
                return self._json(200, {"ok": True, **app.optimizer_state()})
            if path == "/api/optimizer/runs":
                from .optimizer import list_runs
                return self._json(200, {"ok": True, "runs": list_runs(app.project_path())})
            if path.startswith("/api/optimizer/") and path.count("/") == 3:
                from .optimizer import load_run, OptimizerError
                try:
                    run = load_run(path.rsplit("/", 1)[-1], root=app.project_path())
                except OptimizerError as e:
                    return self._json(404, {"ok": False, "error": str(e)})
                return self._json(200, {"ok": True, **run})
            if path == "/api/replay/status":
                r = app.replay.snapshot() if app.replay else None
                return self._json(200, {"ok": True, "replay": r})
            if path == "/api/profiles":
                from .profiles import PROFILES
                return self._json(200, {"ok": True, "profiles": PROFILES})
            if path == "/api/help":
                return self._json(200, {"ok": True, "commands": {
                    "shadow": "python -m wallexbot run --mode shadow",
                    "live": "python -m wallexbot run --mode live",
                    "backtest": "python -m wallexbot backtest --learn",
                    "fetch": "python -m wallexbot fetch --days 30",
                    "tests": "python -m unittest discover -s tests -t .",
                    "docs": ["README.md", "docs/ALGORITHM.md", "docs/RISK.md", "docs/RUNBOOK.md"]}})
            return self._json(404, {"ok": False, "error": "unknown endpoint"})

        # ---- POST
        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if not path.startswith("/api/"):
                    return self._json(404, {"ok": False, "error": "not found"})
                if not self._auth_ok(parsed):
                    return self._json(401, {"ok": False, "error": "unauthorized"})
                if self.headers.get("X-Requested-With") != "wallexbot":
                    return self._json(403, {"ok": False, "error": "missing X-Requested-With header"})
                body = self._read_body()
                return self.api_post(path, body)
            except Exception as e:              # noqa: BLE001
                traceback.print_exc()
                return self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

        def api_post(self, path, body):
            if path in ("/api/bot/start", "/api/bot/stop", "/api/flatten", "/api/reset-kill",
                        "/api/settings", "/api/replay/start", "/api/replay/stop",
                        "/api/backtest/run", "/api/tests/run", "/api/optimizer/start",
                        "/api/optimizer/stop", "/api/optimizer/approve"):
                blocked = app.read_only or (app.demo and path in ("/api/settings", "/api/optimizer/approve"))
                if blocked:
                    return self._json(403, {"ok": False, "error": "حالت فقط‌خواندنی/دمو اجازهٔ این عملیات را نمی‌دهد"})
            if path == "/api/bot/start":
                mode = str(body.get("mode") or "shadow")
                if app.demo:
                    mode = "replay"
                if mode not in ALLOWED_MODES:
                    return self._json(400, {"ok": False, "error": "mode must be shadow|live|replay"})
                if app.runner.running:
                    return self._json(409, {"ok": False, "error": "ربات در حال اجراست"})
                if mode == "replay":
                    r = app.start_replay(speed=float(body.get("speed") or 600), loop=bool(body.get("loop", True)))
                    return self._json(200, {"ok": True, **r})
                if mode == "live":
                    fails = app.gate_failures()
                    if fails:
                        return self._json(400, {"ok": False, "error": "دروازهٔ live باز نیست", "failures": fails})
                app.runner.base = app.fresh_cfg()
                app.runner.start(mode)
                return self._json(200, {"ok": True, "mode": mode})
            if path == "/api/bot/stop":
                ok = app.runner.stop()
                return self._json(200, {"ok": ok, "status": app.runner.status()})
            if path == "/api/flatten":
                if app.replay and app.replay.bot is not None and app.replay.thread and app.replay.thread.is_alive():
                    app.replay.bot.flatten_requested = True
                    return self._json(200, {"ok": True, "queued": True})
                r = app.runner.flatten()
                return self._json(200, r)
            if path == "/api/reset-kill":
                p = app.cfg.state_path
                st = read_json(p) or {}
                st.setdefault("risk", {})["killed"] = False
                st["risk"]["peak"] = float(st.get("last_equity") or 0.0)
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(st, f, ensure_ascii=False, indent=1)
                return self._json(200, {"ok": True, "note": "kill-switch ریست شد؛ peak روی سرمایهٔ فعلی تنظیم شد"})
            if path == "/api/replay/start":
                r = app.start_replay(speed=float(body.get("speed") or 900), loop=bool(body.get("loop", True)))
                return self._json(200, {"ok": True, **r})
            if path == "/api/replay/stop":
                if app.replay:
                    app.replay.stop()
                return self._json(200, {"ok": True})
            if path == "/api/settings":
                return self.save_settings(body)
            if path == "/api/test/connection":
                return self._json(200, app.test_connection())
            if path == "/api/test/ai":
                return self._json(200, app.test_ai())
            if path == "/api/mcp/call":
                name = str(body.get("name") or "")
                if not name:
                    return self._json(400, {"ok": False, "error": "name لازم است"})
                cfg = app.fresh_cfg()
                from .mcp import WallexMCP
                c = WallexMCP(cfg.mcp_url, timeout=cfg.mcp_timeout_s)
                res = c.call_tool(name, body.get("arguments") or {})
                return self._json(200, {"ok": True, "result": res, "text": WallexMCP.text_of(res)})
            if path == "/api/backtest/run":
                j = app.job("backtest", lambda: app.run_backtest(
                    synthetic=bool(body.get("synthetic")), learn=bool(body.get("learn")),
                    ai_gate=bool(body.get("ai_gate"))))
                return self._json(200, {"ok": True, "job": j})
            if path == "/api/optimizer/start":
                return self._json(200, {"ok": True, **app.start_optimizer(
                    synthetic=bool(body.get("synthetic")), ai=bool(body.get("ai")),
                    rounds=int(body.get("rounds") or 2), min_trades=int(body.get("min_trades") or 2),
                    purge=int(body.get("purge") or 8))})
            if path == "/api/optimizer/stop":
                return self._json(200, app.stop_optimizer())
            if path == "/api/optimizer/approve":
                run_id = str(body.get("run_id") or "").strip()
                if not run_id:
                    return self._json(400, {"ok": False, "error": "run_id لازم است"})
                if not body.get("confirm"):
                    return self._json(400, {"ok": False, "error": "اعمال نیازمند تأیید صریح است (confirm=true)"})
                try:
                    res = app.approve_optimizer(run_id, confirm=True, force=bool(body.get("force")))
                except Exception as e:          # noqa: BLE001
                    return self._json(400, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                return self._json(200, res)
            if path == "/api/tests/run":
                j = app.job("safetytests" if body.get("safety") else "unit", lambda: app.run_tests(bool(body.get("safety"))))
                return self._json(200, {"ok": True, "job": j})
            if path == "/api/auto-replay":
                r = app.start_replay(speed=float(body.get("speed") or 900), loop=bool(body.get("loop", True)))
                return self._json(200, {"ok": True, **r})
            return self._json(404, {"ok": False, "error": "unknown endpoint"})

        def save_settings(self, body):
            mapping = {
                "wallex_api_key": "WALLEX_API_KEY", "ai_api_key": "WTB_AI_API_KEY",
                "ai_provider": "WTB_AI_PROVIDER", "ai_model": "WTB_AI_MODEL", "ai_base_url": "WTB_AI_BASE_URL",
                "symbols": "WTB_SYMBOLS", "quote_asset": "WTB_QUOTE_ASSET", "resolution": "WTB_RESOLUTION",
                "challenge_end": "WTB_CHALLENGE_END", "initial_capital": "WTB_INITIAL_CAPITAL",
                "max_drawdown": "WTB_MAX_DRAWDOWN", "daily_loss_limit": "WTB_DAILY_LOSS_LIMIT",
                "max_positions": "WTB_MAX_POSITIONS", "max_pos_frac": "WTB_MAX_POS_FRAC",
                "reserve_frac": "WTB_RESERVE_FRAC", "max_trades_per_day": "WTB_MAX_TRADES_PER_DAY",
                "risk_per_trade": "WTB_RISK_PER_TRADE", "min_score": "WTB_MIN_SCORE",
                "rules_min_score": "WTB_RULES_MIN_SCORE", "fee_rate": "WTB_FEE_RATE",
                "min_atr_pct": "WTB_MIN_ATR_PCT", "rsi_buy": "WTB_RSI_BUY", "bb_max_pos": "WTB_BB_MAX_POS",
                "mcp_url": "WTB_MCP_URL", "mcp_enabled": "WTB_MCP_ENABLED",
                "api_key_header": "WTB_API_KEY_HEADER", "extra_headers": "WTB_EXTRA_HEADERS",
            }
            values = {}
            for ui_key, env_key in mapping.items():
                if ui_key not in body:
                    continue
                v = body[ui_key]
                if ui_key == "symbols" and isinstance(v, str):
                    v = ",".join(s.strip() for s in v.split(",") if s.strip())
                if ui_key == "extra_headers" and isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)
                values[env_key] = v
            if not values:
                return self._json(400, {"ok": False, "error": "هیچ تنظیمی ارسال نشد"})
            # اعتبارسنجی با یک کانفیگ موقت (بدون نوشتن فایل)
            old = {k: os.environ.get(k) for k in values}
            try:
                os.environ.update({k: str(v) for k, v in values.items()})
                test = Config.from_env()
                errs = test.validate()
            finally:
                for k, prev in old.items():
                    if prev is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = prev
            if errs:
                return self._json(400, {"ok": False, "error": "تنظیمات نامعتبر", "details": errs})
            path = save_env(os.path.join(os.getcwd(), ".env"), values)
            app.cfg = Config.from_env()
            app.runner.base = app.cfg
            return self._json(200, {"ok": True, "saved": path, "settings": app.cfg.public(),
                                    "note": "برای اعمال کامل، ربات را دوباره شروع کنید."})

    return Handler


def serve(host="127.0.0.1", port=8800, demo=False, read_only=False, token="", autostart=None, speed=600):
    cfg = Config.from_env()
    app = App(cfg, demo=demo, read_only=read_only, token=token)
    if autostart or (demo and autostart is None):
        try:
            app.start_replay(speed=speed, loop=True)
        except Exception as e:                  # noqa: BLE001
            print(f"replay autostart failed: {e}")
    handler = make_handler(app)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    print(f"Wallex control panel: {url}" + (f"?token={token}" if token else ""))
    print(f"data dir: {ROOT} | demo={demo} read_only={read_only}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.replay:
            app.replay.stop()
        if app.runner.running:
            app.runner.stop(wait=3)
        httpd.server_close()


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="wallexbot ui")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8800)
    p.add_argument("--demo", action="store_true", help="فقط replay محلی؛ تنظیمات/کلیدها غیرفعال")
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--token", default="", help="توکن دسترسی به API پنل")
    p.add_argument("--autostart", action="store_true", help="شروع خودکار replay")
    p.add_argument("--speed", type=float, default=600, help="سرعت ساعت مجازی replay (ثانیهٔ مجازی به ازای هر ثانیهٔ واقعی)")
    a = p.parse_args(argv)
    serve(a.host, a.port, a.demo, a.read_only, a.token, a.autostart, a.speed)
    return 0
