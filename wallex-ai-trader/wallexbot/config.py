from __future__ import annotations
import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _parse_env_file(path: Path) -> dict:
    """خوانندهٔ سادهٔ .env (بدون dependency). کلیدهای موجود در محیط واقعی همیشه مقدم‌اند."""
    out = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        else:
            v = v.split(" #", 1)[0].rstrip()
        if k:
            out[k] = v
    return out


def load_dotenv(paths=None):
    """مقادیر .env را (فقط برای کلیدهای تعریف‌نشده) داخل os.environ می‌گذارد."""
    if paths is None:
        paths = [Path.cwd() / ".env", ROOT / ".env"]
    loaded = {}
    for p in paths:
        for k, v in _parse_env_file(Path(p)).items():
            if k not in os.environ:
                os.environ[k] = v
                loaded[k] = v
    return loaded


@dataclass
class Config:
    # ---- اجرا ----
    mode: str = "shadow"                 # shadow | live
    wallex_api_key: str = ""
    anthropic_api_key: str = ""          # سازگاری با نسخه‌های قبلی
    ai_api_key: str = ""                 # کلید AI (Anthropic یا OpenAI-compatible)
    ai_provider: str = "anthropic"       # anthropic | openai
    ai_base_url: str = ""                # خالی = پیش‌فرض provider
    ai_model: str = "claude-sonnet-5-5"
    ai_enabled: bool = True
    ai_required_live: bool = True        # live بدون AI مجاز نیست مگر صریحاً false شود
    ai_timeout_s: int = 25
    ai_max_tokens: int = 400
    ai_retries: int = 1
    ai_version: str = "2023-06-01"        # هدر anthropic-version؛ در صورت تغییر API قابل تنظیم
    ai_cache_s: int = 90
    ai_log_path: str = "logs/ai_calls.jsonl"
    min_ai_conf: float = 0.60
    rules_min_score: float = 55.0        # آستانه وقتی AI نیست (shadow / backtest)
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

    # ---- تنظیمات اتصال ----
    api_key_header: str = "X-API-Key"    # طبق مستندات رسمی والکس
    extra_headers: dict = field(default_factory=dict)   # هدرهای اضافه (مثلاً حساب فرعی)، JSON در WTB_EXTRA_HEADERS
    mcp_url: str = "https://api.wallex.ir/mcp"
    mcp_enabled: bool = False            # استفاده از MCP برای زمینهٔ بازار در پرامپت AI
    mcp_timeout_s: int = 12
    ws_url: str = "wss://api.wallex.ir/connection/websocket"   # فقط مستند/آینده؛ منبع اصلی داده REST است

    # ---- سیگنال ----
    # آستانهٔ نوسان به‌صورت «هزینه‌محور» انتخاب شده: با کارمزد ۰٫۳۵٪ در هر سمت، حرکت مورد انتظار باید
    # چند برابر هزینهٔ رفت‌وبرگشت باشد، وگرنه معامله از پیش بازنده است. ATR واقعی ۱۵ دقیقه‌ای در دادهٔ
    # نمونه ۰٫۲۰–۰٫۳۷٪ بود؛ ۰٫۲۵٪ یعنی فقط نمادهایی که نوسانشان به‌اندازهٔ کافی بالاست بررسی می‌شوند.
    rsi_buy: float = 45.0
    min_atr_pct: float = 0.0025
    max_trend_dev: float = 0.04
    bb_max_pos: float = 0.30

    # ---- ریسک ----
    max_positions: int = 3
    max_pos_frac: float = 0.25
    reserve_frac: float = 0.10
    max_exposure_frac: float = 0.75
    max_trades_per_day: int = 20
    risk_per_trade: float = 0.008
    sl_atr: float = 2.5
    tp_atr: float = 5.0
    sl_min: float = 0.030
    sl_max: float = 0.060
    tp_min: float = 0.060
    trail_activate: float = 0.040
    trail_dist: float = 0.025
    max_hold_h: float = 48.0
    cooldown_min: float = 30.0
    daily_loss_limit: float = 0.025
    # !! مقدار پیش‌فرض ۱۵٪ انتخاب خودِ پروژه است و در بریف رسمی چنین عددی نیامده. برای live باید
    # !! صریحاً با WTB_MAX_DRAWDOWN تعیین شود (docs/DECISIONS.md).
    max_drawdown: float = 0.05

    # ---- یادگیری ----
    learn_enabled: bool = True
    learn_every: int = 4
    learn_min_trades: int = 8
    learn_window: int = 20

    # ---- بک‌تست ----
    bt_size_mult: float = 0.85           # فرض: میانگین ضریب حجم AI

    # ---- مسیرها / تأییدیه ----
    state_path: str = "state.json"
    save_interval_s: float = 0.0          # 0 = هر گام؛ در replay برای کاهش I/O بزرگ‌تر می‌شود
    log_dir: str = "logs"
    data_dir: str = "data"
    live_ack: str = ""
    validation_max_age_h: float = 96.0

    explicit: set = field(default_factory=set, repr=False)

    FINGERPRINT_FIELDS = ("max_positions", "max_pos_frac", "reserve_frac", "max_exposure_frac",
                          "max_trades_per_day", "risk_per_trade", "sl_atr", "tp_atr",
                          "sl_min", "sl_max", "tp_min", "trail_activate", "trail_dist", "max_hold_h",
                          "daily_loss_limit", "max_drawdown", "fee_rate", "min_notional", "order_slip",
                          "stop_entries_h", "flatten_min", "challenge_end", "symbols", "quote_asset",
                          "min_score", "rules_min_score", "rsi_buy", "min_atr_pct", "max_trend_dev",
                          "bb_max_pos")

    @classmethod
    def from_env(cls, dotenv_paths=None) -> "Config":
        load_dotenv(dotenv_paths)
        cfg = cls()
        cfg.wallex_api_key = os.getenv("WALLEX_API_KEY", cfg.wallex_api_key)
        cfg.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", cfg.anthropic_api_key)
        # کلید AI: اولویت با WTB_AI_API_KEY / AI_API_KEY / OPENAI_API_KEY و در آخر ANTHROPIC_API_KEY
        cfg.ai_api_key = (os.getenv("WTB_AI_API_KEY") or os.getenv("AI_API_KEY")
                          or os.getenv("OPENAI_API_KEY") or cfg.anthropic_api_key)
        for f in dataclasses.fields(cls):
            if f.name == "explicit":
                continue
            v = os.getenv("WTB_" + f.name.upper())
            if v is None or v == "":
                continue
            cur = getattr(cfg, f.name)
            if isinstance(cur, bool):
                val = v.lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, int) and not isinstance(cur, bool):
                val = int(v)
            elif isinstance(cur, float):
                val = float(v)
            elif isinstance(cur, list):
                val = [s.strip() for s in v.split(",") if s.strip()]
            elif isinstance(cur, dict):
                try:
                    parsed = json.loads(v)
                except ValueError as e:
                    raise SystemExit(f"WTB_{f.name.upper()} باید JSON معتبر باشد: {e}")
                if not isinstance(parsed, dict):
                    raise SystemExit(f"WTB_{f.name.upper()} باید آبجکت JSON باشد")
                val = {str(k): str(vv) for k, vv in parsed.items()}
            else:
                val = v
            setattr(cfg, f.name, val)
            cfg.explicit.add(f.name)
        if not cfg.ai_api_key:
            cfg.ai_api_key = cfg.anthropic_api_key
        return cfg

    # ---- کمکی‌ها ----
    def end_ts(self):
        return datetime.fromisoformat(self.challenge_end).timestamp() if self.challenge_end else None

    def hours_left(self, now=None):
        end = self.end_ts()
        if not end:
            return None
        return (end - (now if now is not None else datetime.now().timestamp())) / 3600.0

    def ai_key(self):
        return self.ai_api_key or self.anthropic_api_key

    def validate(self):
        e = []
        if self.mode not in ("shadow", "live"):
            e.append("mode must be shadow|live")
        if self.ai_provider not in ("anthropic", "openai"):
            e.append("ai_provider must be anthropic|openai")
        if not self.symbols:
            e.append("symbols is empty")
        elif len(set(self.symbols)) != len(self.symbols):
            e.append("symbols contains duplicates")
        if any(not s.endswith(self.quote_asset) for s in self.symbols):
            e.append(f"all symbols must end with {self.quote_asset}")
        for n in ("max_pos_frac", "reserve_frac", "max_exposure_frac", "risk_per_trade",
                  "daily_loss_limit", "max_drawdown"):
            if not 0 < getattr(self, n) < 1:
                e.append(f"{n} must be in (0,1)")
        if self.max_positions < 1:
            e.append("max_positions must be >= 1")
        if self.max_positions * self.max_pos_frac > self.max_exposure_frac + 1e-9:
            e.append("max_positions*max_pos_frac exceeds max_exposure_frac")
        if self.max_exposure_frac > 1 - self.reserve_frac + 1e-9:
            e.append("max_exposure_frac exceeds 1-reserve_frac")
        if self.max_trades_per_day < 1:
            e.append("max_trades_per_day must be >= 1")
        if self.daily_loss_limit >= self.max_drawdown:
            e.append("daily_loss_limit must be < max_drawdown (otherwise the daily limit can never trigger first)")
        if not self.sl_min < self.sl_max:
            e.append("sl_min must be < sl_max")
        if self.tp_min <= 2 * self.fee_rate:
            e.append("tp_min must exceed round-trip fees")
        if self.resolution not in (1, 3, 5, 15, 30, 60, 120, 240):
            e.append("unsupported resolution")
        if self.min_score > self.rules_min_score:
            e.append("min_score must be <= rules_min_score (rules-only is the stricter path)")
        if self.challenge_end:
            try:
                self.end_ts()
            except ValueError:
                e.append("challenge_end is not ISO-8601")
        if self.save_interval_s < 0:
            e.append("save_interval_s must be >= 0")
        if not isinstance(self.extra_headers, dict):
            e.append("extra_headers must be a dict")
        return e

    def fingerprint(self):
        d = {k: getattr(self, k) for k in self.FINGERPRINT_FIELDS}
        d["symbols"] = sorted(d["symbols"])
        return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:12]

    def public(self):
        """نمای قابل‌نمایش/ذخیره (کلیدها ماسک می‌شوند)."""
        secret = {"wallex_api_key", "anthropic_api_key", "ai_api_key"}
        out = {}
        for f in dataclasses.fields(self):
            if f.name == "explicit":
                continue
            v = getattr(self, f.name)
            if f.name in secret:
                out[f.name] = mask(v)
            else:
                out[f.name] = v
        out["fingerprint"] = self.fingerprint()
        out["ai_key_present"] = bool(self.ai_key())
        out["wallex_key_present"] = bool(self.wallex_api_key)
        return out


def mask(v):
    v = v or ""
    if not v:
        return ""
    if len(v) <= 8:
        return "•" * len(v)
    return v[:3] + "…" + v[-3:]


def save_env(path, values: dict):
    """ذخیرهٔ امن تنظیمات در .env (فقط کلیدهای مجاز، بدون چاپ مقدارها)."""
    allowed = {f.name.upper() for f in dataclasses.fields(Config)} | {
        "WALLEX_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY"}
    path = Path(path)
    existing = {}
    if path.exists():
        existing = _parse_env_file(path)
    for k, v in values.items():
        k = k.strip().upper()
        if k not in allowed and not k.startswith("WTB_"):
            continue
        if v is None:
            continue
        v = str(v)
        if "\n" in v or "\r" in v:
            raise ValueError(f"مقدار {k} شامل خط جدید است")
        existing[k] = v
    tmp = path.with_suffix(path.suffix + ".tmp")
    lines = ["# ساخته‌شده توسط پنل والکس‌بات — این فایل را در گیت ذخیره نکنید.", ""]
    for k in sorted(existing):
        val = existing[k]
        if any(ch in val for ch in " #"):
            val = '"' + val.replace('"', '') + '"'
        lines.append(f"{k}={val}")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path
