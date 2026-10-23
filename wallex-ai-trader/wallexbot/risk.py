"""مدیریت سرمایه و ریسک — قوانین سخت، خارج از کنترل AI."""
from __future__ import annotations
from dataclasses import dataclass, asdict


@dataclass
class Position:
    symbol: str
    qty: float
    entry: float
    entry_ts: float
    stop: float
    tp: float
    high: float
    kind: str
    cost_tmn: float           # شامل کارمزد ورود
    reason: str = ""
    low: float = 0.0          # برای MAE
    sl_pct: float = 0.0
    tp_pct: float = 0.0
    feat: dict | None = None
    proceeds_acc: float = 0.0  # فروش‌های جزئی
    cost_sold: float = 0.0
    qty_closed: float = 0.0    # مجموع حجم فروخته‌شده (برای گزارش درست در trades.csv)
    last_px: float = 0.0

    def to_dict(self):
        return asdict(self)

    def unrealized_pct(self, price):
        return 100.0 * (price / self.entry - 1.0) if self.entry else 0.0

    def market_value(self, price):
        return self.qty * price


class RiskManager:
    def __init__(self, cfg, state: dict):
        self.cfg, self.s = cfg, state
        self.s.setdefault("peak", 0.0)
        self.s.setdefault("day", 0)
        self.s.setdefault("day_start", 0.0)
        self.s.setdefault("killed", False)
        self.s.setdefault("ai_mode", "normal")   # normal | defensive | pause
        self.s.setdefault("pause_since", 0.0)

    @property
    def killed(self):
        return self.s["killed"]

    def update(self, equity, ts):
        day = int(ts // 86400)
        if day != self.s["day"] or not self.s["day_start"]:
            self.s["day"], self.s["day_start"] = day, equity
        self.s["peak"] = max(self.s["peak"], equity)
        if self.s["peak"] and equity < self.s["peak"] * (1 - self.cfg.max_drawdown):
            self.s["killed"] = True

    def entry_block(self, equity, n_positions, trades_today=None, exposure=None):
        """دلیل مسدود بودن ورود، یا None.

        trades_today / exposure اختیاری‌اند تا فراخوانی‌های قدیمی (و تست‌ها) بدون تغییر کار کنند.
        """
        c = self.cfg
        if self.s["killed"]:
            return "kill_switch: max drawdown hit"
        if self.s["ai_mode"] == "pause":
            return "ai_risk_mode=pause"
        if n_positions >= c.max_positions:
            return "max_positions"
        if trades_today is not None and trades_today >= c.max_trades_per_day:
            return "max_trades_per_day"
        if exposure is not None and equity > 0 and exposure > c.max_exposure_frac * equity:
            return "max_exposure"
        if self.s["day_start"] and equity < self.s["day_start"] * (1 - c.daily_loss_limit):
            return "daily_loss_limit"
        return None

    def levels(self, price, atr_pct):
        c = self.cfg
        sl = min(max(c.sl_atr * atr_pct, c.sl_min), c.sl_max)
        tp = max(c.tp_atr * atr_pct, c.tp_min, 4 * c.fee_rate)
        return price * (1 - sl), price * (1 + tp), sl, tp

    def size(self, equity, cash, stop_pct):
        c = self.cfg
        by_risk = equity * c.risk_per_trade / stop_pct       # ضرر در حد استاپ = ۰٫۶٪ کل سرمایه
        cap = equity * c.max_pos_frac
        avail = max(0.0, cash - equity * c.reserve_frac)     # همیشه بخشی نقد می‌ماند
        avail = avail / (1.0 + c.fee_rate)                   # کارمزد هم باید از همین نقد پرداخت شود
        n = min(by_risk, cap, avail)
        if self.s["ai_mode"] == "defensive":
            n *= 0.5
        return n


def check_exit(pos: Position, price, ts, cfg, high=None, low=None):
    """برمی‌گرداند (reason, ref_price) یا None. برای live: high=low=price."""
    hi = price if high is None else high
    lo = price if low is None else low
    old_stop = pos.stop
    pos.high = max(pos.high, hi)
    pos.low = min(pos.low or lo, lo)
    pos.last_px = price
    # ترتیب درون‌کندلی محافظه‌کارانه: ابتدا low با استاپِ «قبل از این کندل» سنجیده می‌شود؛ تریلینگ جدید
    # از high همین کندل فقط برای ادامه اعمال می‌شود (وگرنه خروج به استاپ بالاتری ثبت می‌شد = خوش‌بینانه).
    if lo <= old_stop:
        return ("trail_stop" if old_stop >= pos.entry else "stop_loss", old_stop)
    if pos.high >= pos.entry * (1 + cfg.trail_activate):
        pos.stop = max(old_stop, pos.high * (1 - cfg.trail_dist))
    if lo <= pos.stop:
        return ("trail_stop" if pos.stop >= pos.entry else "stop_loss", pos.stop)
    if hi >= pos.tp:
        return ("take_profit", pos.tp)
    if ts - pos.entry_ts > cfg.max_hold_h * 3600:
        return ("time_stop", price)
    return None
