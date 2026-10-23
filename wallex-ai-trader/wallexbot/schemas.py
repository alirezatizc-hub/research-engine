"""اعتبارسنجی سخت‌گیرانه پاسخ‌های API والکس (fail-closed).
مرجع: https://api-docs.wallex.ir (مستندات رسمی). هر فیلد گم‌شده/نوع نادرست => SchemaError، بدون مقدار پیش‌فرض."""
from __future__ import annotations
import math
from collections import Counter
from dataclasses import dataclass

VERSION = "1"


class SchemaError(ValueError):
    pass


def num(x, name, minimum=None, strict_pos=False):
    if isinstance(x, bool) or x is None:
        raise SchemaError(f"{name}: not a number: {x!r}")
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise SchemaError(f"{name}: not a number: {x!r}")
    if not math.isfinite(v):
        raise SchemaError(f"{name}: not finite")
    if strict_pos and v <= 0:
        raise SchemaError(f"{name}: must be > 0, got {v}")
    if minimum is not None and v < minimum:
        raise SchemaError(f"{name}: must be >= {minimum}, got {v}")
    return v


def req(d, key, ctx):
    if not isinstance(d, dict) or key not in d:
        raise SchemaError(f"{ctx}: missing '{key}'")
    return d[key]


def text(x, name):
    if not isinstance(x, str) or not x:
        raise SchemaError(f"{name}: expected non-empty string, got {x!r}")
    return x


def envelope(d, ctx):
    if not isinstance(d, dict) or d.get("success") is not True or "result" not in d:
        raise SchemaError(f"{ctx}: bad envelope (success!=true or no result)")
    return d["result"]


def _dec(x, name):
    v = num(x, name, 0.0)
    if v != int(v) or v > 12:
        raise SchemaError(f"{name}: expected integer decimals 0..12, got {x!r}")
    return int(v)


# ---------------- markets ----------------
@dataclass
class MarketInfo:
    symbol: str
    base: str
    quote: str
    step_dec: int        # stepSize: تعداد اعشار حجم
    tick_dec: int        # tickSize: تعداد اعشار قیمت
    min_qty: float
    min_notional: float  # به واحد ارز تجاری (برای جفت TMN: تومان)


def parse_markets(d):
    syms = req(envelope(d, "markets"), "symbols", "markets")
    if not isinstance(syms, dict) or not syms:
        raise SchemaError("markets: empty symbols")
    ok, bad = {}, []
    for k, m in syms.items():
        try:
            if req(m, "symbol", k) != k:
                raise SchemaError("symbol mismatch")
            ok[k] = MarketInfo(
                k, text(req(m, "baseAsset", k), "baseAsset"), text(req(m, "quoteAsset", k), "quoteAsset"),
                _dec(req(m, "stepSize", k), "stepSize"), _dec(req(m, "tickSize", k), "tickSize"),
                num(req(m, "minQty", k), "minQty", 0.0), num(req(m, "minNotional", k), "minNotional", 0.0))
        except SchemaError as e:
            bad.append((k, str(e)))
    if not ok:
        raise SchemaError("markets: no valid market")
    return ok, bad


# ---------------- order book ----------------
@dataclass
class Book:
    bid: float
    ask: float
    bid_qty: float = 0.0
    ask_qty: float = 0.0

    @property
    def spread(self):
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid if mid else 1.0


def _levels(lst, name):
    if not isinstance(lst, list) or not lst:
        raise SchemaError(f"depth: empty {name}")
    return [(num(req(i, "price", name), f"{name}.price", strict_pos=True),
             num(req(i, "quantity", name), f"{name}.quantity", 0.0)) for i in lst]


def parse_book(d):
    r = envelope(d, "depth")
    asks, bids = _levels(req(r, "ask", "depth"), "ask"), _levels(req(r, "bid", "depth"), "bid")
    a, b = min(asks), max(bids)
    if b[0] >= a[0]:
        raise SchemaError(f"depth: crossed book bid={b[0]} ask={a[0]}")
    return Book(bid=b[0], ask=a[0], bid_qty=b[1], ask_qty=a[1])


# ---------------- candles (UDF) ----------------
def parse_candles(d, res_min):
    if isinstance(d, dict) and "s" not in d and isinstance(d.get("result"), dict):
        d = d["result"]
    if not isinstance(d, dict) or d.get("s") != "ok":
        raise SchemaError(f"candles: s!='ok' ({str(d)[:80]})")
    cols = {k: req(d, k, "candles") for k in "tohlcv"}
    n = len(cols["t"]) if isinstance(cols["t"], list) else 0
    if n == 0 or any(not isinstance(v, list) or len(v) != n for v in cols.values()):
        raise SchemaError("candles: empty or ragged arrays")
    out, prev = [], None
    for i in range(n):
        t = num(cols["t"][i], "t", strict_pos=True)
        if t != int(t) or (prev is not None and t <= prev):
            raise SchemaError("candles: t not strictly increasing ints")
        prev = t
        o, h, l, c = (num(cols[k][i], k, strict_pos=True) for k in "ohlc")
        v = num(cols["v"][i], "v", 0.0)
        if h < max(o, c) * (1 - 1e-9) or l > min(o, c) * (1 + 1e-9):
            raise SchemaError(f"candles: inconsistent OHLC at t={int(t)}")
        out.append({"t": int(t), "o": o, "h": h, "l": l, "c": c, "v": v})
    if n >= 3:
        common = Counter(out[i + 1]["t"] - out[i]["t"] for i in range(n - 1)).most_common(1)[0][0]
        if common != res_min * 60:
            raise SchemaError(f"candles: resolution mismatch, spacing={common}s expected={res_min * 60}s")
    return out


# ---------------- balances / fees ----------------
@dataclass
class Balance:
    free: float
    total: float


def parse_balances(d):
    bals = req(envelope(d, "balances"), "balances", "balances")
    if not isinstance(bals, dict):
        raise SchemaError("balances: not a mapping")
    out = {}
    for k, v in bals.items():
        val, locked = num(req(v, "value", k), f"{k}.value", 0.0), num(req(v, "locked", k), f"{k}.locked", 0.0)
        if locked > val + 1e-9:
            raise SchemaError(f"balances: locked>value for {k}")
        out[k] = Balance(free=val - locked, total=val)
    return out


def parse_fee(d):
    """مستندات نمونه را به‌صورت map نشان می‌دهد؛ برای سازگاری، list از آبجکت‌ها هم پذیرفته می‌شود."""
    r = envelope(d, "fee")
    if isinstance(r, list):
        rows = {}
        for i, v in enumerate(r):
            if not isinstance(v, dict):
                raise SchemaError(f"fee[{i}]: not an object")
            k = text(req(v, "symbol", f"fee[{i}]"), "fee.symbol")
            rows[k] = v
        r = rows
    if not isinstance(r, dict) or not r:
        raise SchemaError("fee: expected non-empty mapping")
    out = {}
    for k, v in r.items():
        if not isinstance(v, dict):
            raise SchemaError(f"fee.{k}: not an object")
        out[k] = {"maker": num(req(v, "makerFeeRate", k), "maker", 0.0),
                  "taker": num(req(v, "takerFeeRate", k), "taker", 0.0)}
    return out


def parse_profile(d):
    """پروفایل فقط خوانده می‌شود؛ برای شناسایی حساب (tracking_id / meta) بدون فرض ساختار سخت."""
    r = envelope(d, "profile")
    if not isinstance(r, dict):
        raise SchemaError("profile: not an object")
    return r


# ---------------- orders ----------------
TERMINAL_WORDS = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}


@dataclass
class OrderInfo:
    client_id: str
    symbol: str
    side: str
    status: str
    active: bool
    price: float
    orig_qty: float
    executed_qty: float
    executed_sum: float

    @property
    def avg_price(self):
        return self.executed_sum / self.executed_qty if self.executed_qty > 0 else 0.0


def parse_order(o, ctx="order"):
    side = req(o, "side", ctx)
    if side not in ("BUY", "SELL"):
        raise SchemaError(f"{ctx}: bad side {side!r}")
    active = req(o, "active", ctx)
    if not isinstance(active, bool):
        raise SchemaError(f"{ctx}: 'active' must be boolean")
    status = text(req(o, "status", ctx), "status")
    if active and status.upper() in TERMINAL_WORDS:
        raise SchemaError(f"{ctx}: active=true but status={status}")
    price = num(req(o, "price", ctx), "price", strict_pos=True)
    orig = num(req(o, "origQty", ctx), "origQty", strict_pos=True)
    exq, exs = num(req(o, "executedQty", ctx), "executedQty", 0.0), num(req(o, "executedSum", ctx), "executedSum", 0.0)
    if exq > orig * (1 + 1e-9):
        raise SchemaError(f"{ctx}: executedQty>origQty")
    if exq > 0:
        avg = exs / exq
        if (side == "BUY" and avg > price * 1.01) or (side == "SELL" and avg < price * 0.99):
            raise SchemaError(f"{ctx}: executed avg {avg} violates limit {price} ({side})")
    return OrderInfo(text(req(o, "clientOrderId", ctx), "clientOrderId"), text(req(o, "symbol", ctx), "symbol"),
                     side, status, active, price, orig, exq, exs)


def parse_order_envelope(d):
    return parse_order(envelope(d, "order"))


def parse_open_orders(d):
    lst = req(envelope(d, "openOrders"), "orders", "openOrders")
    if not isinstance(lst, list):
        raise SchemaError("openOrders: orders not a list")
    return [parse_order(o, "openOrders[]") for o in lst]
