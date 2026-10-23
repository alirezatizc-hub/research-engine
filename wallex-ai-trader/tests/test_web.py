"""تست پنل کنترل: health/status، ضد CSRF، توکن، و خواندن معاملات بدون ستون qty."""
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import requests

from wallexbot.config import Config
from wallexbot.web import App, make_handler, read_trades


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = tempfile.mkdtemp()
        cfg = Config(mode="shadow", state_path=os.path.join(d, "state.json"), log_dir=d,
                     ai_enabled=False, wallex_api_key="")
        cfg.explicit = {"max_drawdown", "symbols", "challenge_end"}
        cls.app = App(cfg, demo=True)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_health_and_static(self):
        r = requests.get(self.base + "/api/health", timeout=5)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertTrue(r.json()["demo"])
        self.assertEqual(requests.get(self.base + "/", timeout=5).status_code, 200)
        self.assertEqual(requests.get(self.base + "/style.css", timeout=5).status_code, 200)
        self.assertEqual(requests.get(self.base + "/app.js", timeout=5).status_code, 200)

    def test_status_endpoint(self):
        r = requests.get(self.base + "/api/status", timeout=10).json()
        self.assertTrue(r["ok"])
        st = r["status"]
        for k in ("mode", "running", "equity", "positions", "risk", "gate_failures"):
            self.assertIn(k, st)
        if st["curve"]:
            self.assertIsInstance(st["curve"][0], list)
            self.assertEqual(len(st["curve"][0]), 2)

    def test_csrf_header_required(self):
        r = requests.post(self.base + "/api/test/ai", json={}, timeout=10)
        self.assertEqual(r.status_code, 403)

    def test_token_required_when_configured(self):
        self.app.token = "s3cret"
        try:
            self.assertEqual(requests.get(self.base + "/api/health", timeout=5).status_code, 401)
            r = requests.get(self.base + "/api/health", headers={"X-UI-Token": "s3cret"}, timeout=5)
            self.assertEqual(r.status_code, 200)
        finally:
            self.app.token = ""

    def test_read_only_mode_blocks_actions(self):
        self.app.read_only = True
        try:
            r = requests.post(self.base + "/api/bot/start", json={"mode": "shadow"},
                              headers={"X-Requested-With": "wallexbot"}, timeout=10)
            self.assertEqual(r.status_code, 403)
        finally:
            self.app.read_only = False

    def test_settings_does_not_leak_keys(self):
        s = requests.get(self.base + "/api/settings", timeout=5).json()["settings"]
        self.assertNotIn("sk-", json.dumps(s))
        self.assertIn("wallex_key_present", s)


class TradeReader(unittest.TestCase):
    def test_backtest_csv_without_qty_does_not_crash(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "trades.csv")
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write("symbol,kind,entry_t,exit_t,entry,exit,pnl_tmn,pnl_pct,reason\n")
            f.write("BTCTMN,dip,1000,2000,10,9,-100,-1.0,stop_loss\n")
        rows = read_trades(p)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exit_reason"], "stop_loss")
        self.assertEqual(rows[0]["qty"], "")


if __name__ == "__main__":
    unittest.main()
