"""Shadow Replay: اجرای همان حلقهٔ زندهٔ ربات روی کندل‌های ضبط‌شدهٔ والکس با ساعت مجازی.

این حالت برای دمو، تست دود سرتاسری و «Shadow Testing» بدون اینترنت/سفارش واقعی است.
هیچ سفارشی ارسال نمی‌شود (ShadowExecutor) و نتایج با برچسب داده (real/synthetic) ذخیره می‌شوند.
"""
from __future__ import annotations
import csv
import dataclasses
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone

from .aggregate import validate_series
from .backtest import load_csv, MANIFEST, REAL_SOURCE, synthetic
from .config import Config
from .schemas import Book, MarketInfo, Balance

log = logging.getLogger("wtb")


class VirtualClock:
    def __init__(self, start_ts, speed=600.0):
        self.start_ts, self.speed = float(start_ts), max(1.0, float(speed))
        self.wall0 = time.time()

    def now(self):
        return self.start_ts + (time.time() - self.wall0) * self.speed

    def sleep(self, virtual_s):
        # هیچ‌وقت واقعاً به اندازهٔ زمان مجازی نمی‌خوابیم؛ فقط کمی برای پرهیز از مصرف CPU
        time.sleep(max(0.001, min(0.03, float(virtual_s) / self.speed)))


class ReplayExchange:
    """پیاده‌سازی حداقلی رابط Wallex برای shadow (بازار/کندل/دفتر) از دادهٔ تاریخی."""

    def __init__(self, data, clock, quote="TMN", spread=0.0008):
        self.data = {s: sorted(cs, key=lambda c: c["t"]) for s, cs in data.items()}
        self.clock, self.quote, self.spread = clock, quote, spread
        self.mk = {}
        for s in self.data:
            base = s[:-len(quote)] if s.endswith(quote) else s[:-3]
            self.mk[s] = MarketInfo(s, base, quote, 6, 0, 1e-6, 10_000.0)

    def markets(self, max_age=600):
        return self.mk

    def market(self, symbol):
        return self.mk[symbol]

    def _closed(self, symbol):
        now = self.clock.now()
        cs = self.data[symbol]
        lo, hi = 0, len(cs)
        while lo < hi:                          # اولین کندلی که هنوز بسته نشده
            mid = (lo + hi) // 2
            if cs[mid]["t"] + 900 <= now:
                lo = mid + 1
            else:
                hi = mid
        return cs[:lo]

    def candles(self, symbol, resolution=15, count=220, to_ts=None):
        if resolution != 15:
            raise ValueError("replay فقط رزولوشن ۱۵ دقیقه را پشتیبانی می‌کند")
        return self._closed(symbol)[-count:]

    def book(self, symbol):
        cs = self._closed(symbol)
        px = cs[-1]["c"] if cs else self.data[symbol][0]["c"]
        half = px * self.spread / 2
        return Book(bid=px - half, ask=px + half, bid_qty=1e6, ask_qty=1e6)

    def fee(self):
        return {s: {"maker": 0.0035, "taker": 0.0035} for s in self.data}

    def balances(self):
        return {self.quote: Balance(5_000_000.0, 5_000_000.0)}


def load_replay_data(data_dir="data", symbols=None, candles_limit=None):
    """-> (data, label). ابتدا دادهٔ واقعی و در نبود آن دادهٔ مصنوعی با برچسب SYNTHETIC."""
    symbols = symbols or []
    mp = os.path.join(data_dir, MANIFEST)
    if os.path.exists(mp):
        try:
            man = json.load(open(mp, encoding="utf-8"))
            if man.get("source") == REAL_SOURCE and man.get("source_resolution_min") == 1:
                data = {}
                for s in symbols:
                    p = os.path.join(data_dir, f"{s}.csv")
                    if os.path.exists(p):
                        cs = load_csv(p)
                        validate_series(cs, int(man.get("resolution", 15)))
                        data[s] = cs if not candles_limit else cs[-candles_limit:]
                if data:
                    return data, f"real:{REAL_SOURCE}"
        except Exception as e:                  # noqa: BLE001
            log.warning("replay real data rejected: %s", e)
    syms = symbols[:4] or ["BTCTMN", "ETHTMN", "SOLTMN", "XRPTMN"]
    return {s: synthetic(seed=i + 1) for i, s in enumerate(syms)}, "SYNTHETIC_NOT_EVIDENCE"


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


class ReplaySession:
    """یک اجرای کامل shadow روی دادهٔ ضبط‌شده؛ قابل restart/loop برای پنل کنترل."""

    def __init__(self, cfg: Config, data, label, speed=600.0, loop=True, log_dir="logs/replay", initial_capital=None):
        self.base_cfg, self.data, self.label = cfg, data, label
        self.speed, self.loop, self.log_dir = speed, loop, log_dir
        self.initial_capital = initial_capital or cfg.initial_capital
        self.first_t = min(c["t"] for cs in data.values() for c in cs)
        self.last_t = max(c["t"] for cs in data.values() for c in cs)
        # features به ۱۱۰ کندل نیاز دارد (EMA96 + حاشیه)؛ بیشتر از آن فقط پنجرهٔ معاملاتی را کوتاه می‌کند.
        n_bars = min(len(cs) for cs in data.values())
        self.warmup = max(0, min(110, n_bars - 10))
        self.start_ts = self.first_t + self.warmup * self.base_cfg.resolution * 60
        self.state_path = os.path.join(log_dir, "state.json")
        self._stop = threading.Event()
        self.thread = None
        self.bot = None
        self.clock = None
        self.runs = 0
        self.status_text = "ready"
        self.error = None

    def snapshot(self):
        now = self.clock.now() if self.clock else self.start_ts
        return {"label": self.label, "speed": self.speed, "loop": self.loop, "runs": self.runs,
                "demo_profile": bool(getattr(self, "demo_profile", False)),
                "virtual_now": iso(now), "progress_pct": min(100.0, round(100 * (now - self.start_ts) /
                                                               max(1.0, self.last_t - self.start_ts), 1)),
                "finished": self.status_text in ("finished", "stopped") or now >= self.last_t,
                "status": self.status_text, "error": self.error}

    def _archive_run(self, keep=5):
        """قبل از پاک‌کردن لاگ‌های اجرای قبلی، شواهد آن را نگه می‌دارد (برای loop دمو).

        شمارهٔ اجرا از روی آرشیو موجود ادامه می‌یابد (نه از صفر) تا در ری‌استارت‌ها بازنویسی نشود؛
        فقط `keep` اجرای آخر نگه داشته می‌شود تا حلقهٔ دمو دیسک را پر نکند.
        """
        if self.runs <= 0:
            return
        base = os.path.join(self.log_dir, "archive")
        try:
            os.makedirs(base, exist_ok=True)
            nums = [int(m.group(1)) for n in os.listdir(base)
                    if (m := re.fullmatch(r"run-(\d+)", n)) and os.path.isdir(os.path.join(base, n))]
            idx = (max(nums) + 1) if nums else 1
            dst = os.path.join(base, f"run-{idx:03d}")
            os.makedirs(dst, exist_ok=True)
            for name in ("trades.csv", "decisions.jsonl", "state.json"):
                src = os.path.join(self.log_dir, name)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(dst, name))
            dirs = sorted(((int(m.group(1)), n) for n in os.listdir(base)
                           if (m := re.fullmatch(r"run-(\d+)", n)) and os.path.isdir(os.path.join(base, n))))
            for _, name in dirs[:-keep]:
                shutil.rmtree(os.path.join(base, name), ignore_errors=True)
        except OSError as e:                    # noqa: BLE001
            log.warning("archive replay run failed: %s", e)

    def _prepare(self):
        os.makedirs(self.log_dir, exist_ok=True)
        self._archive_run()
        for name in os.listdir(self.log_dir):
            if name == "archive":
                continue
            p = os.path.join(self.log_dir, name)
            try:
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            except OSError:
                pass
        self.clock = VirtualClock(self.start_ts, self.speed)
        ex = ReplayExchange(self.data, self.clock, quote=self.base_cfg.quote_asset)
        cfg = dataclasses.replace(self.base_cfg, mode="shadow", state_path=self.state_path,
                                  log_dir=self.log_dir, challenge_end=iso(self.last_t),
                                  save_interval_s=max(1.0, float(self.base_cfg.save_interval_s)),
                                  # در replay پایانِ داده یعنی پایان چالش؛ پنجرهٔ ۴ساعته/۴۵دقیقه‌ایِ live اینجا
                                  # معنا ندارد و فقط بخش بزرگی از داده را بی‌استفاده می‌گذارد.
                                  stop_entries_h=min(float(self.base_cfg.stop_entries_h), 2.0),
                                  flatten_min=min(float(self.base_cfg.flatten_min), 30.0),
                                  initial_capital=self.initial_capital)
        from .bot import Bot
        self.bot = Bot(cfg, client=ex, now=self.clock.now, sleep=self.clock.sleep)
        return cfg

    def start(self):
        if self.thread and self.thread.is_alive():
            return False
        self._stop.clear()
        self.thread = threading.Thread(target=self._run, name="replay", daemon=True)
        self.thread.start()
        return True

    def _run(self):
        try:
            while not self._stop.is_set():
                self._prepare()
                self.status_text = "running"
                self.runs += 1
                self.bot.run(should_stop=lambda: self._stop.is_set() or self.clock.now() >= self.last_t)
                self.status_text = "finished"
                if not self.loop or self._stop.is_set():
                    break
                self._stop.wait(2.0)
        except Exception as e:                  # noqa: BLE001
            log.exception("replay failed")
            self.status_text, self.error = "error", f"{type(e).__name__}: {e}"[:300]

    def stop(self):
        self._stop.set()
        if self.bot is not None:
            self.bot.state["running"] = False
        if self.thread:
            self.thread.join(timeout=5)
        self.status_text = "stopped"
