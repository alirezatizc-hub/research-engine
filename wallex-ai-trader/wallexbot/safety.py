"""دروازه live: تا همه شرایط زیر برقرار نباشد، هیچ سفارش واقعی ارسال نمی‌شود.
(محافظ در برابر اشتباه است، نه در برابر دست‌کاری عمدی فایل‌ها.)"""
from __future__ import annotations
import hashlib, json, os, time
from . import schemas

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REQUIRED_CHECKS = ["markets", "depth", "candles", "balances", "fee", "order_place", "order_status",
                   "order_cancel", "open_orders"]
REQUIRED_TESTS = [
    "test_kill_switch_live_flattens_blocks_entries_and_persists",
    "test_kill_switch_backtest_stops_entries_and_liquidates_conservatively",
    "test_unconfirmed_cancel_halts_bot",
    "test_cancel_is_confirmed_before_result",
    "test_partial_fill_buy_creates_partial_position",
    "test_partial_fill_sell_keeps_remainder_and_retries",
    "test_ambiguous_place_resolved_by_lookup_no_duplicate",
    "test_ambiguous_place_unresolved_halts",
    "test_restart_recovers_filled_pending_buy",
    "test_restart_cancels_unknown_open_orders",
    "test_restart_unknown_holdings_halts",
    "test_disallowed_symbol_buy_rejected_without_http",
    "test_markets_failure_blocks_entries",
    "test_live_gate_blocks_orders_without_validation",
    "test_live_gate_requires_all_conditions",
    "test_llm_error_does_not_reset_pause",
    "test_llm_invalid_types_rejected",
    "test_ai_gate_fails_closed",
    "test_schema_rejects_malformed_responses",
    "test_learner_stays_within_bounds_and_rolls_back",
    "test_learner_disables_losing_symbol",
    "test_dip_signal_matches_documented_rules",
    "test_breakout_signal_matches_documented_rules",
    "test_diagnose_is_consistent_with_evaluate",
    "test_backtest_enters_next_bar_open_and_exits_at_take_profit",
    "test_backtest_exits_at_stop_loss",
    "test_intrabar_stop_wins_over_target_and_over_new_trailing",
    "test_backtest_has_no_lookahead",
]


def _sha(paths):
    h = hashlib.sha256()
    for p in sorted(paths):
        with open(p, "rb") as f:
            h.update(os.path.relpath(p, ROOT).encode()); h.update(f.read())
    return h.hexdigest()[:16]


def schemas_hash():
    d = os.path.join(ROOT, "wallexbot")
    return _sha([os.path.join(d, "schemas.py"), os.path.join(d, "exchange.py")])


def code_hash():
    paths = []
    for sub in ("wallexbot", "tests"):
        d = os.path.join(ROOT, sub)
        for dp, _, fs in os.walk(d):
            if "__pycache__" in dp or "fixtures" in dp:
                continue
            paths += [os.path.join(dp, f) for f in fs if f.endswith(".py")]
    return _sha(paths)


class LiveBlocked(Exception):
    pass


class LiveGate:
    def __init__(self, cfg, now=time.time):
        self.cfg, self.now = cfg, now

    def _load(self, name):
        p = os.path.join(self.cfg.log_dir, name)
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            return None

    def failures(self, check_age=True):
        c, f = self.cfg, []
        for n in ("max_drawdown", "symbols", "challenge_end"):
            if n not in c.explicit:
                f.append(f"{n} باید صریحاً با WTB_{n.upper()} تعیین شود (در live پیش‌فرض ندارد)")
        f += [f"config: {e}" for e in c.validate()]
        if c.live_ack != c.fingerprint():
            f.append(f"WTB_LIVE_ACK نادرست/خالی؛ پس از مرور پارامترها (python -m wallexbot params) مقدار {c.fingerprint()} را تنظیم کنید")
        if c.ai_required_live and not (c.ai_enabled and c.anthropic_api_key):
            f.append("کلید/فعال‌سازی AI برای live الزامی است (یا WTB_AI_REQUIRED_LIVE=false)")
        if not c.wallex_api_key:
            f.append("WALLEX_API_KEY خالی است")
        v = self._load("api_validation.json")
        if not v:
            f.append("logs/api_validation.json نیست: `python -m wallexbot selfcheck --yes-place-test-order` را اجرا کنید")
        else:
            if v.get("source") != "live_api":
                f.append("api_validation: منبع live_api نیست")
            if v.get("validator_version") != schemas.VERSION or v.get("schemas_hash") != schemas_hash():
                f.append("api_validation: با نسخه فعلی schemas/exchange همخوان نیست؛ selfcheck را دوباره اجرا کنید")
            if check_age and now_age(v, self.now) > c.validation_max_age_h * 3600:
                f.append("api_validation منقضی شده")
            for k in REQUIRED_CHECKS:
                if (v.get("checks", {}).get(k) or {}).get("ok") is not True:
                    f.append(f"api_validation: بررسی {k} نگذشته")
        t = self._load("safety_tests.json")
        if not t:
            f.append("logs/safety_tests.json نیست: `python -m wallexbot safetytests` را اجرا کنید")
        else:
            if t.get("code_hash") != code_hash():
                f.append("safety_tests: کد پس از اجرای تست‌ها تغییر کرده؛ تست‌ها را دوباره اجرا کنید")
            ok = set(t.get("passed_tests", []))
            miss = [n for n in REQUIRED_TESTS if n not in ok]
            if t.get("failures") or t.get("errors") or miss:
                f.append(f"safety_tests ناموفق/ناقص: {miss[:3]}")
        return f

    def assert_open(self, check_age=True):
        """check_age=False برای هر سفارش: انقضای زمانی نباید تسویه پایان چالش را مسدود کند."""
        f = self.failures(check_age)
        if f:
            raise LiveBlocked(" | ".join(f))


def now_age(rep, now):
    return now() - float(rep.get("ts", 0))
