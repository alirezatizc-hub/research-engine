"""تست لایهٔ REST با نشست جعلی: retry فقط برای GET، و طبقه‌بندی قطعی/نامعلوم ثبت سفارش."""
import json
import os
import unittest

import requests

from wallexbot.exchange import (AmbiguousOrder, HTTPFailure, TransientError, Wallex)
from wallexbot.schemas import MarketInfo

HERE = os.path.dirname(__file__)
MARKETS = json.load(open(os.path.join(HERE, "fixtures", "docs", "markets.json"), encoding="utf-8"))
MI = MarketInfo("BTCTMN", "BTC", "TMN", 6, 0, 1e-6, 10_000.0)
ORDER = {"success": True, "message": "ok", "result": {
    "symbol": "BTCTMN", "side": "BUY", "price": "100", "origQty": "10", "executedQty": "0",
    "executedSum": "0", "status": "NEW", "active": True, "clientOrderId": "wtb-1"}}


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params, json, headers))
        if not self.responses:
            raise AssertionError("unexpected HTTP call")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(*responses):
    s = Session(*responses)
    return Wallex("key", session=s, sleep=lambda _s: None), s


class GetRetry(unittest.TestCase):
    def test_5xx_and_429_are_retried(self):
        w, s = client(Resp(500, {}), Resp(429, {}), Resp(200, MARKETS))
        mk = w.markets()
        self.assertIn("BTCUSDT", mk)
        self.assertEqual(len(s.calls), 3)

    def test_non_json_2xx_is_retried(self):
        w, s = client(Resp(200, ValueError("html")), Resp(200, MARKETS))
        self.assertTrue(w.markets())

    def test_4xx_other_than_429_is_not_retried(self):
        w, s = client(Resp(401, {"success": False}))
        with self.assertRaises(HTTPFailure):
            w.markets()
        self.assertEqual(len(s.calls), 1)

    def test_network_error_retried_then_transient(self):
        w, s = client(requests.ConnectionError("down"), requests.ConnectionError("down"),
                      requests.ConnectionError("down"))
        with self.assertRaises(TransientError):
            w.markets()
        self.assertEqual(len(s.calls), 3)


class PlaceClassification(unittest.TestCase):
    def test_402_is_definite_rejection(self):
        w, s = client(Resp(402, {"success": False, "message": "insufficient"}))
        with self.assertRaises(HTTPFailure):
            w.place(MI, "BUY", "100", "10", "wtb-1")
        self.assertEqual(s.calls[0][0], "POST")

    def test_429_is_ambiguous_never_duplicated(self):
        w, s = client(Resp(429, {"success": False}))
        with self.assertRaises(AmbiguousOrder):
            w.place(MI, "BUY", "100", "10", "wtb-1")
        self.assertEqual(len(s.calls), 1)          # هیچ retry خودکاری

    def test_409_is_ambiguous(self):
        w, _ = client(Resp(409, {"success": False}))
        with self.assertRaises(AmbiguousOrder):
            w.place(MI, "BUY", "100", "10", "wtb-1")

    def test_network_is_ambiguous(self):
        w, _ = client(requests.Timeout("slow"))
        with self.assertRaises(AmbiguousOrder):
            w.place(MI, "BUY", "100", "10", "wtb-1")

    def test_success_parses_order_and_sends_client_id(self):
        w, s = client(Resp(200, ORDER))
        info = w.place(MI, "BUY", "100", "10", "wtb-1")
        self.assertEqual((info.client_id, info.active, info.orig_qty), ("wtb-1", True, 10.0))
        self.assertEqual(s.calls[0][3]["client_id"], "wtb-1")
        self.assertEqual(s.calls[0][4]["X-API-Key"], "key")


if __name__ == "__main__":
    unittest.main()
