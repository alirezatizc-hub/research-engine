"""دریافت ۱ دقیقه‌ای و تجمیع به ۱۵ دقیقه‌ای: فقط با پاسخ‌های ساختگی و کنترل‌شده (بدون شبکه، بدون سفارش)."""
import json, os, tempfile, unittest
from wallexbot import aggregate as A, schemas as S
from wallexbot.exchange import Wallex, NoDataError, TransientError
from wallexbot.backtest import save_csv, load_real_dataset, MANIFEST, REAL_SOURCE

T0 = 1_700_000_100            # 1_700_000_100 % 900 == 0  (مرز ۱۵ دقیقه)


def m(t, o, h, l, c, v):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def minute_series(start, n, skip=(), dup=()):
    """n کندل ۱ دقیقه‌ای قطعی؛ open هر دقیقه = i+100، close = i+100.5، high = +2، low = -1، volume = 1+i%3."""
    out = []
    for i in range(n):
        if i in skip:
            continue
        o = 100.0 + i
        out.append(m(start + 60 * i, o, o + 2, o - 1, o + 0.5, 1.0 + i % 3))
        if i in dup:
            out.append(dict(out[-1]))
    return out


def payload(rows):
    return {"s": "ok", "t": [r["t"] for r in rows], **{k: [str(r[k]) for r in rows] for k in "ohlcv"}}


class FakeResp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


class FakeSession:
    """endpoint ساختگی: بر اساس from/to از یک سری ۱ دقیقه‌ای کامل پاسخ می‌دهد و درخواست‌ها را ثبت می‌کند."""
    def __init__(self, rows, mutate=None, ignore_resolution=True):
        self.rows, self.mutate, self.calls = rows, mutate, []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append(dict(params))
        sel = [r for r in self.rows if params["from"] <= r["t"] <= params["to"]]
        d = payload(sel) if sel else {"s": "no_data"}
        return FakeResp(self.mutate(d, len(self.calls)) if self.mutate else d)


def client(session, now):
    return Wallex(session=session, sleep=lambda s: None, now=lambda: now)


class Aggregation(unittest.TestCase):
    def test_ohlcv_aggregation_exact(self):
        bars = A.aggregate(minute_series(T0, 30), 15, T0, T0 + 1800)
        self.assertEqual(len(bars), 2)
        b0, b1 = bars
        self.assertEqual(b0["t"], T0)
        self.assertEqual(b0["o"], 100.0)                       # open دقیقه اول
        self.assertEqual(b0["c"], 114.5)                       # close دقیقه آخر (i=14)
        self.assertEqual(b0["h"], 116.0)                       # max(high) = 114+2
        self.assertEqual(b0["l"], 99.0)                        # min(low)  = 100-1
        self.assertEqual(b0["v"], sum(1.0 + i % 3 for i in range(15)))
        self.assertEqual((b1["t"], b1["o"], b1["c"]), (T0 + 900, 115.0, 129.5))

    def test_high_low_not_taken_from_open_close_only(self):
        rows = minute_series(T0, 15)
        rows[7] = m(rows[7]["t"], 107.0, 500.0, 1.0, 107.5, 5.0)    # قله و دره وسط بازه
        b = A.aggregate(rows, 15, T0, T0 + 900)[0]
        self.assertEqual((b["h"], b["l"]), (500.0, 1.0))
        self.assertEqual((b["o"], b["c"]), (100.0, 114.5))

    def test_bucket_boundaries_are_epoch_aligned(self):
        self.assertEqual(T0 % 900, 0)
        self.assertEqual(A.last_closed_boundary(T0 + 899, 15), T0)
        self.assertEqual(A.last_closed_boundary(T0 + 900, 15), T0 + 900)
        self.assertEqual(A.last_closed_boundary(T0 + 1799, 15), T0 + 900)
        with self.assertRaises(A.DataIntegrityError):              # بازه هم‌تراز نیست
            A.aggregate(minute_series(T0 + 60, 15), 15, T0 + 60, T0 + 960)
        # دقیقه‌ای که به باکت بعدی تعلق دارد به باکت قبلی نشت نمی‌کند
        bars = A.aggregate(minute_series(T0, 30), 15, T0, T0 + 1800)
        self.assertEqual(bars[1]["o"], 115.0)

    def test_missing_minute_is_rejected_not_filled(self):
        with self.assertRaises(A.DataIntegrityError) as cm:
            A.aggregate(minute_series(T0, 30, skip={20}), 15, T0, T0 + 1800)
        self.assertIn("ناقص", str(cm.exception))
        with self.assertRaises(A.DataIntegrityError):              # کل یک باکت گم‌شده
            A.aggregate(minute_series(T0, 15), 15, T0, T0 + 1800)

    def test_incomplete_trailing_candle_never_emitted(self):
        # 22 دقیقه داده: باکت دوم ناقص است؛ بازه‌ی [T0, T0+1800) نباید «موفق» شود
        with self.assertRaises(A.DataIntegrityError):
            A.aggregate(minute_series(T0, 22), 15, T0, T0 + 1800)
        # و از طریق کلاینت: now وسط باکت => فقط باکت‌های بسته‌شده درخواست/ساخته می‌شوند
        rows = minute_series(T0 - 3600, 400)
        c = client(FakeSession(rows), now=T0 + 600)               # 10 دقیقه از باکت جاری گذشته
        bars = c.candles("BTCTMN", 15, 4)
        self.assertEqual([b["t"] for b in bars], [T0 - 3600 + 900 * i for i in range(4)])
        self.assertLess(bars[-1]["t"] + 900, T0 + 1)               # باکت در حال تشکیل (T0) وجود ندارد

    def test_duplicate_and_unordered_minutes_rejected(self):
        with self.assertRaises(A.DataIntegrityError):
            A.aggregate(minute_series(T0, 15, dup={3}), 15, T0, T0 + 900)
        rows = minute_series(T0, 15); rows[4], rows[5] = rows[5], rows[4]
        with self.assertRaises(A.DataIntegrityError):
            A.aggregate(rows, 15, T0, T0 + 900)

    def test_bad_timestamps_rejected(self):
        ms = minute_series(T0, 15); ms[0]["t"] = T0 * 1000
        with self.assertRaises(A.DataIntegrityError): A.validate_minutes(ms)
        off = minute_series(T0, 15); off[0]["t"] += 7
        with self.assertRaises(A.DataIntegrityError): A.validate_minutes(off)
        out = minute_series(T0, 16)
        with self.assertRaises(A.DataIntegrityError): A.aggregate(out, 15, T0, T0 + 900)   # خارج از بازه

    def test_validate_series_detects_gap_duplicate_order(self):
        good = A.aggregate(minute_series(T0, 45), 15, T0, T0 + 2700)
        A.validate_series(good, 15)
        for bad in ([good[0], good[2]], [good[0], good[0]], [good[1], good[0]], []):
            with self.assertRaises(A.DataIntegrityError): A.validate_series(bad, 15)


class Fetching(unittest.TestCase):
    def test_requests_are_always_one_minute_and_small(self):
        days, now = 2, T0 + 5 * 3600 * 24 + 450                    # now وسط یک باکت
        end = A.last_closed_boundary(now, 15)
        rows = minute_series(end - days * 86400, days * 1440)
        sess = FakeSession(rows)
        bars, meta = client(sess, now).fetch_history("BTCTMN", 15, days, now=now)
        self.assertTrue(all(c["resolution"] == "1" for c in sess.calls))      # هرگز resolution=15 نیست
        self.assertTrue(all(c["to"] - c["from"] + 1 <= Wallex.CHUNK_MIN * 60 for c in sess.calls))
        self.assertEqual(len(sess.calls), days * 1440 // Wallex.CHUNK_MIN)
        self.assertEqual(len(bars), days * 96)
        self.assertEqual(meta["minute_candles"], days * 1440)
        self.assertEqual(meta["source_resolution_min"], 1)
        self.assertEqual(bars[-1]["t"] + 900, end)                 # آخرین کندل = آخرین باکت بسته‌شده
        A.validate_series(bars, 15)
        self.assertEqual(bars[0]["o"], 100.0)

    def test_thirty_days_pagination_count(self):
        days, now = 30, T0 + 40 * 86400
        end = A.last_closed_boundary(now, 15)
        sess = FakeSession(minute_series(end - days * 86400, days * 1440))
        bars, meta = client(sess, now).fetch_history("ETHTMN", 15, days, now=now)
        self.assertEqual((len(bars), meta["requests"]), (2880, 90))

    def test_gap_in_api_data_fails_loudly(self):
        days, now = 1, T0 + 86400
        end = A.last_closed_boundary(now, 15)
        rows = minute_series(end - 86400, 1440, skip={700, 701})
        with self.assertRaises(A.DataIntegrityError) as cm:
            client(FakeSession(rows), now).fetch_history("BTCTMN", 15, 1, now=now)
        self.assertIn("ناقص", str(cm.exception))

    def test_invalid_responses_rejected(self):
        now = T0 + 3600
        rows = minute_series(T0 - 3600, 400)
        cases = {
            "no_data": lambda d, n: {"s": "no_data"},
            "error": lambda d, n: {"s": "error", "errmsg": "x"},
            "ragged": lambda d, n: {**d, "v": d["v"][:-1]},
            "bad_ohlc": lambda d, n: {**d, "h": ["0.5"] * len(d["t"])},
            "nan_text": lambda d, n: {**d, "o": ["abc"] * len(d["t"])},
            "dup_in_response": lambda d, n: {**d, "t": [d["t"][0]] + d["t"][:-1]},
            "wrong_spacing_900s": lambda d, n: {**d, "t": [d["t"][0] + 900 * i for i in range(len(d["t"]))]},
            "millis": lambda d, n: {**d, "t": [t * 1000 for t in d["t"]]},
        }
        for name, mut in cases.items():
            with self.subTest(name):
                with self.assertRaises(A.DataIntegrityError):
                    client(FakeSession(rows, mutate=mut), now).candles("BTCTMN", 15, 4)

    def test_no_data_is_distinguished_from_malformed_schema(self):
        empty = FakeSession([])
        with self.assertRaises(NoDataError) as cm:
            client(empty, T0).minute_candles("BTCTMN", T0, T0 + 600)
        self.assertEqual(cm.exception.symbol, "BTCTMN")
        self.assertEqual((cm.exception.frm, cm.exception.to), (T0, T0 + 600))
        self.assertIn("no_data", str(cm.exception))

    def test_http_504_is_transient_not_no_data(self):
        class TimeoutSession:
            def __init__(self):
                self.calls = 0
            def request(self, *args, **kwargs):
                self.calls += 1
                return FakeResp({"message": "upstream request timeout"}, 504)

        sess = TimeoutSession()
        with self.assertRaises(TransientError) as cm:
            client(sess, T0).minute_candles("BTCTMN", T0, T0 + 600)
        self.assertIn("HTTP 504", str(cm.exception))
        self.assertEqual(sess.calls, 3)  # retries transient server failures

    def test_non_json_and_http_error_not_treated_as_data(self):
        class Bad(FakeSession):
            def request(self, *a, **k):
                self.calls.append(1); return FakeResp(None, 200)
        from wallexbot.exchange import TransientError
        with self.assertRaises(TransientError):
            client(Bad([]), T0).candles("BTCTMN", 15, 4)


class DatasetFiles(unittest.TestCase):
    def _write(self, d, bars, **man_over):
        save_csv(os.path.join(d, "BTCTMN.csv"), bars)
        man = {"source": REAL_SOURCE, "source_resolution_min": 1, "resolution": 15, "aggregated_client_side": True}
        man.update(man_over)
        json.dump(man, open(os.path.join(d, MANIFEST), "w"))

    def test_roundtrip_and_gap_rejection(self):
        bars = A.aggregate(minute_series(T0, 150), 15, T0, T0 + 9000)
        with tempfile.TemporaryDirectory() as d:
            self._write(d, bars)
            data, man = load_real_dataset(d, ["BTCTMN"])
            self.assertEqual(len(data["BTCTMN"]), 10)
        with tempfile.TemporaryDirectory() as d:
            self._write(d, bars[:4] + bars[5:])                     # شکاف
            with self.assertRaises(SystemExit): load_real_dataset(d, ["BTCTMN"])
        with tempfile.TemporaryDirectory() as d:
            self._write(d, bars, source_resolution_min=15)          # منبع ۱۵ دقیقه‌ای ادعایی
            with self.assertRaises(SystemExit): load_real_dataset(d, ["BTCTMN"])


if __name__ == "__main__":
    unittest.main()
