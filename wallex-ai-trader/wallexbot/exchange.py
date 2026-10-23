"""کلاینت REST والکس. همه پاسخ‌ها از schemas.py می‌گذرند. ثبت سفارش هرگز خودکار retry نمی‌شود."""
from __future__ import annotations
import time, logging
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import requests
from . import schemas as S
from . import aggregate as A

log = logging.getLogger("wtb")
BASE = "https://api.wallex.ir"
__all__ = ["Wallex", "HTTPFailure", "NotFound", "AmbiguousOrder", "TransientError", "NoDataError", "fmt_qty", "fmt_price",
           "ROUND_DOWN", "ROUND_UP"]


class HTTPFailure(Exception):
    def __init__(self, status, body=None):
        super().__init__(f"HTTP {status}: {str(body)[:200]}")
        self.status, self.body = status, body


class NotFound(HTTPFailure):
    pass


class AmbiguousOrder(Exception):
    """نتیجه ثبت سفارش نامعلوم است (timeout/5xx/409/پاسخ نامعتبر): ممکن است سفارش ثبت شده باشد."""


class TransientError(Exception):
    """خطای شبکه/سرور پس از چند تلاش."""


class NoDataError(Exception):
    """پاسخ معتبر UDF با s=no_data؛ علت خالی‌بودن تاریخچه نامشخص است."""
    def __init__(self, symbol, frm, to, params=None):
        self.symbol, self.frm, self.to, self.params = symbol, int(frm), int(to), params or {}
        super().__init__(
            f"Wallex returned s=no_data for {symbol} range "
            f"[{self.frm}, {self.to}); cause is unknown (not proof of an exchange outage)"
        )


def _q(decimals, value, rounding):
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=rounding)


def fmt_qty(mi, qty, rounding=ROUND_DOWN):
    return format(_q(mi.step_dec, qty, rounding), "f")


def fmt_price(mi, price, rounding=ROUND_DOWN):
    return format(_q(mi.tick_dec, price, rounding), "f")


class Wallex:
    def __init__(self, api_key="", base=BASE, session=None, timeout=12, sleep=time.sleep, now=time.time,
                 api_key_header="X-API-Key", extra_headers=None):
        self.key, self.base, self.timeout, self.sleep, self.now = api_key, base, timeout, sleep, now
        self.api_key_header = api_key_header or "X-API-Key"
        # هدرهای اضافه برای حساب‌های فرعی/هکاتون؛ هرگز در لاگ چاپ نمی‌شوند.
        self.extra_headers = {str(k): str(v) for k, v in (extra_headers or {}).items()}
        self.s = session or requests.Session()
        self._mk, self._mk_ts = None, 0.0

    # ---- low level ----
    def _send(self, method, path, params=None, body=None, auth=False):
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if auth:
            headers[self.api_key_header] = self.key
        r = self.s.request(method, self.base + path, params=params, json=body, headers=headers, timeout=self.timeout)
        try:
            data = r.json()
        except ValueError:
            data = None
        return r.status_code, data

    def _call(self, method, path, params=None, body=None, auth=False, retries=3):
        last = None
        for i in range(retries):
            try:
                status, data = self._send(method, path, params, body, auth)
            except requests.RequestException as e:
                last = e; self.sleep(1 + i); continue
            if status == 429 or status >= 500:
                last = HTTPFailure(status, data); self.sleep(1.5 * (i + 1)); continue
            if status == 404:
                raise NotFound(status, data)
            if status >= 400:
                raise HTTPFailure(status, data)
            if data is None:
                last = ValueError("non-JSON response"); self.sleep(1 + i); continue
            return data
        raise TransientError(f"{method} {path}: {last}")

    # ---- public ----
    def markets(self, max_age=600):
        if self._mk is not None and self.now() - self._mk_ts <= max_age:
            return self._mk
        ok, bad = S.parse_markets(self._call("GET", "/v1/markets"))
        if bad:
            log.warning("invalid market entries ignored: %s", bad[:3])
        self._mk, self._mk_ts = ok, self.now()
        return ok

    def market(self, symbol):
        m = self.markets().get(symbol)
        if m is None:
            raise KeyError(f"unknown market {symbol}")
        return m

    def book(self, symbol):
        return S.parse_book(self._call("GET", "/v1/depth", params={"symbol": symbol}))

    def profile(self):
        """پروفایل حساب؛ برای پیدا کردن شناسهٔ حساب/زیرحساب (tracking_id / meta) و بررسی کلید."""
        return S.parse_profile(self._call("GET", "/v1/account/profile", auth=True))

    def account_trades(self, symbol=None, side=None):
        """آخرین معاملات حساب (منبع مستقل برای راستی‌آزمایی معاملات و کارمزد واقعی)."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        if side:
            params["side"] = side
        r = S.envelope(self._call("GET", "/v1/account/trades", params=params or None, auth=True), "accountTrades")
        rows = r.get("AccountLatestTrades") if isinstance(r, dict) else None
        if not isinstance(rows, list):
            raise S.SchemaError("accountTrades: AccountLatestTrades not a list")
        return rows

    # ---- candles ----
    # رفتار واقعی (گزارش اجرا): resolution=15 کندل‌هایی با فاصله ۶۰ثانیه برمی‌گرداند؛ پس فقط دادهٔ ۱ دقیقه‌ای
    # از endpoint دریافت می‌شود و تجمیع به N دقیقه در سمت کلاینت انجام می‌شود (aggregate.py).
    SOURCE_RES = 1              # دقیقه
    CHUNK_MIN = 480             # حداکثر کندل ۱ دقیقه‌ای در هر درخواست (۸ ساعت) — کوچک و امن

    
    
    def minute_candles(self, symbol, frm, to):
        """کندل‌های ۱ دقیقه‌ای بازه‌ی [frm, to)."""
        params = {
            "symbol": symbol,
            "resolution": str(self.SOURCE_RES),
            "from": int(frm),
            "to": int(to) - 1,
        }
        d = self._call("GET", "/v1/udf/history", params=params)

        if isinstance(d, dict) and d.get("s") == "no_data":
            # Keep an empty historical response distinct from a malformed payload.
            # no_data alone does not prove whether the source data is absent or unavailable.
            log.warning(
                "Wallex history empty (s=no_data); cause unknown; symbol=%s range=[%s,%s) params=%r",
                symbol, int(frm), int(to), params,
            )
            raise NoDataError(symbol, frm, to, params)

        rows = S.parse_candles(d, self.SOURCE_RES)
        return [c for c in rows if frm <= c["t"] < to]



    def aggregated_candles(self, symbol, res_min, start, end):
        """کندل‌های کامل res_min دقیقه‌ای برای [start, end) از روی دادهٔ ۱ دقیقه‌ای. -> (bars, meta)"""
        step = res_min * 60
        if res_min % self.SOURCE_RES or start % step or end % step or end <= start:
            raise A.DataIntegrityError(f"بازه/رزولوشن نامعتبر: res={res_min} start={start} end={end}")
        mins, reqs, t = [], 0, start
        while t < end:
            nxt = min(t + self.CHUNK_MIN * 60, end)
            try:
                mins += self.minute_candles(symbol, t, nxt)
            except NoDataError as e:
                raise A.DataIntegrityError(
                    f"{symbol}: Wallex returned no_data for {A._fmt(t)}..{A._fmt(nxt)}; "
                    "علت نامشخص است و این پاسخ به‌تنهایی قطعی صرافی یا نبود واقعی داده را ثابت نمی‌کند"
                ) from e
            except S.SchemaError as e:
                raise A.DataIntegrityError(f"{symbol}: پاسخ نامعتبر برای بازه {A._fmt(t)}..{A._fmt(nxt)}: {e}") from e
            reqs += 1
            t = nxt
            if t < end:
                self.sleep(0.3)
        bars = A.aggregate(mins, res_min, start, end)
        return bars, {"source_resolution_min": self.SOURCE_RES, "resolution_min": res_min,
                      "minute_candles": len(mins), "candles": len(bars), "requests": reqs,
                      "first_t": bars[0]["t"], "last_t": bars[-1]["t"], "range_start": start, "range_end": end}

    def candles(self, symbol, resolution=15, count=220, to_ts=None):
        """آخرین `count` کندل *بسته‌شده*؛ کندل در حال تشکیل هرگز برنمی‌گردد."""
        end = A.last_closed_boundary(to_ts or self.now(), resolution)
        return self.aggregated_candles(symbol, resolution, end - count * resolution * 60, end)[0]

    def fetch_history(self, symbol, resolution, days, now=None):
        """-> (bars, meta). بازه: days روز تمام‌شده تا آخرین مرز بسته‌شده؛ کندل ناقص انتهایی حذف می‌شود."""
        end = A.last_closed_boundary(now or self.now(), resolution)
        start = end - int(days * 86400) // (resolution * 60) * (resolution * 60)
        bars, meta = self.aggregated_candles(symbol, resolution, start, end)
        meta["fetched_at"] = self.now() if now is None else now
        return bars, meta

    def history(self, symbol, resolution, days):
        return self.fetch_history(symbol, resolution, days)[0]

    # ---- private ----
    def balances(self):
        return S.parse_balances(self._call("GET", "/v1/account/balances", auth=True))

    def fee(self):
        return S.parse_fee(self._call("GET", "/v1/account/fee", auth=True))

    def open_orders(self, symbol=None):
        return S.parse_open_orders(self._call("GET", "/v1/account/openOrders",
                                              params={"symbol": symbol} if symbol else None, auth=True))

    def order(self, client_id):
        return S.parse_order_envelope(self._call("GET", f"/v1/account/orders/{client_id}", auth=True))

    def cancel(self, client_id):
        """نتیجه لغو فقط با order() تأیید می‌شود، نه با پاسخ این تابع."""
        return self._call("DELETE", "/v1/account/orders", params={"clientOrderId": client_id}, auth=True, retries=2)

    def place(self, mi, side, price_str, qty_str, client_id):
        body = {"symbol": mi.symbol, "type": "LIMIT", "side": side, "price": price_str,
                "quantity": qty_str, "client_id": client_id}
        try:
            status, data = self._send("POST", "/v1/account/orders", body=body, auth=True)
        except requests.RequestException as e:
            raise AmbiguousOrder(f"network: {e}")
        if status in (400, 401, 402, 403, 404, 405, 422):
            raise HTTPFailure(status, data)             # رد قطعی: سفارش ثبت نشده
        # 409 (client_id تکراری), 429 (محدودیت نرخ پیش از پردازش یا بعد از آن — نامعلوم), 5xx,
        # پاسخ غیر-JSON و هر کد نامنتظره: ممکن است سفارش ثبت شده باشد => باید با client_id بررسی شود.
        if status == 409 or status >= 400 or data is None:
            raise AmbiguousOrder(f"HTTP {status}: {str(data)[:120]}")
        try:
            return S.parse_order_envelope(data)
        except S.SchemaError as e:
            raise AmbiguousOrder(f"unparseable 2xx response: {e}")
