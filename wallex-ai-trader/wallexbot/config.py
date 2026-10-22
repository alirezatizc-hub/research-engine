from __future__ import annotations
import os, dataclasses, hashlib, json
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Config:
    # ---- اجرا ----
    mode: str = "shadow"                 # shadow | live
    wallex_api_key: str = ""
    anthropic_api_key: str = ""
    ai_model: str = "claude-sonnet-5-5"
    ai_enabled: bool = True
    ai_required_live: bool = True        # live بدون AI مجاز نیست مگر صریحاً false شود
    ai_timeout_s: int = 25
    min_ai_conf: float = 0.60
    rules_min_score: float = 70.0        # آستانه وقتی AI نیست (shadow / backtest)
    min_score: float = 55.0              # حداقل امتیاز سیگنال (پایه؛ learner می‌تواند بالا ببرد)
    review_interval_min: int = 180
    pause_min_minutes: int = 60          # AI نمی‌تواند pause را زودتر از این برگرداند

    symbols: list = field(default_factory=lambda: [
        "BTCTMN", "ETHTMN", "SOLTMN", "XRPTMN", "DOGETMN", "TRXTMN", "ADATMN", "BNBTMN"])
    quote_asset: str = "TMN"
    resolution: int = 15
    candle_count: int = 220
    poll_s: int = 15
    initial_capital: float = 5_000_000   # فقط shadow/backtest

    # ---- زمان‌بندی چالش ----
    challenge_end: str = ""
    stop_entries_h: float = 4.0
    flatten_min: float = 45.0

    # ---- هزینه‌ها و اجرا ----
    fee_rate: float = 0.0035             # محافظه‌کارانه؛ در live max(این، takerFeeRate واقعی) استفاده می‌شود
    slippage: float = 0.0010             # shadow/backtest
    kill_slip_mult: float = 3.0          # لغزش تسویه اضطراری = slippage × این
    order_slip: float = 0.0020           # تلورانس limit قابل‌اجرا در live
    fill_timeout_s: int = 20
    order_poll_s: float = 1.5
    cancel_confirm_tries: int = 8
    min_notional: float = 120_000        # کف داخلی؛ در live max(این، minNotional بازار)
    dust_tmn: float = 30_000
    cancel_unknown_orders: bool = True
    max_spread: float = 0.004

    # ---- سیگنال ----
    rsi_buy: float = 35.0
    min_atr_pct: float = 0.006
    max_trend_dev: float = 0.04
    bb_max_pos: float = 0.08

    # ---- ریسک ----
    max_positions: int = 3
    max_pos_frac: float = 0.30
    reserve_frac: float = 0.10
    risk_per_trade: float = 0.006
    sl_atr: float = 1.6
    tp_atr: float = 2.6
    sl_min: float = 0.012
    sl_max: float = 0.030
    tp_min: float = 0.016
    trail_activate: float = 0.012
    trail_dist: float = 0.008
    max_hold_h: float = 10.0
    cooldown_min: float = 90.0
    daily_loss_limit: float = 0.08
    # !! مقدار پیش‌فرض ۱۵٪ انتخاب خودِ پروژه است و در بریف رسمی چنین عددی نیامده. برای live باید
    # !! صریحاً با WTB_MAX_DRAWDOWN تعیین شود (docs/DECISIONS.md).
    max_drawdown: float = 0.15

    # ---- یادگیری ----
    learn_enabled: bool = True
    learn_every: int = 4
    learn_min_trades: int = 8
    learn_window: int = 20

    # ---- بک‌تست ----
    bt_size_mult: float = 0.85           # فرض: میانگین ضریب حجم AI

    # ---- مسیرها / تأییدیه ----
    state_path: str = "state.json"
    log_dir: str = "logs"
    live_ack: str = ""
    validation_max_age_h: float = 96.0

    explicit: set = field(default_factory=set, repr=False)

    FINGERPRINT_FIELDS = ("max_positions", "max_pos_frac", "reserve_frac", "risk_per_trade", "sl_atr", "tp_atr",
                          "sl_min", "sl_max", "tp_min", "trail_activate", "trail_dist", "max_hold_h",
                          "daily_loss_limit", "max_drawdown", "fee_rate", "min_notional", "order_slip",
                          "stop_entries_h", "flatten_min", "challenge_end", "symbols", "quote_asset")

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls()
        cfg.wallex_api_key = os.getenv("WALLEX_API_KEY", "")
        cfg.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
        for f in dataclasses.fields(cls):
            if f.name == "explicit":
                continue
            v = os.getenv("WTB_" + f.name.upper())
            if v is None or v == "":
                continue
            cur = getattr(cfg, f.name)
            if isinstance(cur, bool):
                val = v.lower() in ("1", "true", "yes")
            elif isinstance(cur, int):
                val = int(v)
            elif isinstance(cur, float):
                val = float(v)
            elif isinstance(cur, list):
                val = [s.strip() for s in v.split(",") if s.strip()]
            else:
                val = v
            setattr(cfg, f.name, val)
            cfg.explicit.add(f.name)
        return cfg

    def end_ts(self):
        return datetime.fromisoformat(self.challenge_end).timestamp() if self.challenge_end else None

    def validate(self):
        e = []
        if self.mode not in ("shadow", "live"):
            e.append("mode must be shadow|live")
        if not self.symbols:
            e.append("symbols is empty")
        if any(not s.endswith(self.quote_asset) for s in self.symbols):
            e.append(f"all symbols must end with {self.quote_asset}")
        for n in ("max_pos_frac", "reserve_frac", "risk_per_trade", "daily_loss_limit", "max_drawdown"):
            if not 0 < getattr(self, n) < 1:
                e.append(f"{n} must be in (0,1)")
        if self.max_positions * self.max_pos_frac > 1 - self.reserve_frac + 1e-9:
            e.append("max_positions*max_pos_frac exceeds 1-reserve_frac")
        if self.daily_loss_limit >= self.max_drawdown:
            e.append("daily_loss_limit must be < max_drawdown (otherwise the daily limit can never trigger first)")
        if not self.sl_min < self.sl_max:
            e.append("sl_min must be < sl_max")
        if self.tp_min <= 2 * self.fee_rate:
            e.append("tp_min must exceed round-trip fees")
        if self.resolution not in (1, 3, 5, 15, 30, 60, 120, 240):
            e.append("unsupported resolution")
        if self.challenge_end:
            try:
                self.end_ts()
            except ValueError:
                e.append("challenge_end is not ISO-8601")
        return e

    def fingerprint(self):
        d = {k: getattr(self, k) for k in self.FINGERPRINT_FIELDS}
        d["symbols"] = sorted(d["symbols"])
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:12]
