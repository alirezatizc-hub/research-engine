import glob, json, os, time, unittest
from wallexbot import schemas as S
from wallexbot.config import Config
from .fakes import *

DOCS = os.path.join(os.path.dirname(__file__), "fixtures", "docs")
load = lambda n: json.load(open(os.path.join(DOCS, n + ".json")))


class DocSamples(unittest.TestCase):
    def test_official_doc_samples_parse(self):
        mk, bad = S.parse_markets(load("markets")); self.assertEqual(mk["BTCUSDT"].tick_dec, 2); self.assertEqual(mk["BTCUSDT"].min_notional, 10)
        b = S.parse_book(load("depth")); self.assertEqual((b.bid, b.ask), (21100.0, 21300.0))
        self.assertEqual(len(S.parse_candles(load("candles"), 60)), 3)
        self.assertEqual(S.parse_balances(load("balances"))["TMN"].free, 10_000_000)
        self.assertTrue(S.parse_order_envelope(load("order")).active)
        self.assertEqual(len(S.parse_open_orders(load("open_orders"))), 1)
        self.assertEqual(S.parse_fee(load("fee"))["BTCTMN"]["taker"], 0.002)

    def test_real_captured_fixtures_if_present(self):
        files = glob.glob(os.path.join(os.path.dirname(__file__), "fixtures", "real", "*.json"))
        if not files:
            self.skipTest("هنوز پاسخ واقعی ضبط نشده (selfcheck اجرا نشده)")
        for f in files:
            for item in json.load(open(f)):
                if item["status"] != 200 or "udf" in f:
                    continue
                r, name = item["response"], os.path.basename(f)
                if "markets" in name: S.parse_markets(r)
                elif "depth" in name: S.parse_book(r)
                elif "balances" in name: S.parse_balances(r)
                elif "openOrders" in name: S.parse_open_orders(r)
                elif "orders" in name and item["request_body"]: S.parse_order_envelope(r)


class ConfigTests(unittest.TestCase):
    def test_validation_and_fingerprint(self):
        self.assertEqual(Config().validate(), [])
        self.assertTrue(Config(daily_loss_limit=0.2).validate())
        self.assertTrue(Config(max_pos_frac=0.5).validate())
        self.assertTrue(Config(symbols=["BTCUSDT"]).validate())
        a, b = Config(), Config(max_drawdown=0.03, daily_loss_limit=0.02)
        self.assertEqual(b.validate(), [])
        self.assertNotEqual(a.fingerprint(), b.fingerprint())


class ShadowE2E(unittest.TestCase):
    def test_shadow_entry_take_profit_and_end_flatten(self):
        cfg = tcfg(mode="shadow")
        bot, ex, clock = make_bot(cfg)
        bot.step(); self.assertIn("BTCTMN", bot.positions)
        clock.sleep(20); ex.price *= 1.05; bot.step()
        self.assertNotIn("BTCTMN", bot.positions); self.assertGreater(bot.broker.cash(), 5_000_000)
        bot.state["last_bucket"] = None; clock.sleep(900); ex.cs = dip_candles(clock); ex.price = ex.cs[-1]["c"]
        bot.cooldown.clear(); bot.step(); self.assertIn("BTCTMN", bot.positions)
        cfg.challenge_end = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(clock.now() + 600))
        clock.sleep(30); self.assertTrue(bot.step()); self.assertEqual(bot.positions, {})
        self.assertFalse(bot.step())
        rows = open(os.path.join(cfg.log_dir, "trades.csv")).read()
        self.assertIn("end_of_challenge", rows); self.assertIn("take_profit", rows)
        self.assertTrue(os.path.exists(os.path.join(cfg.log_dir, "decisions.jsonl")))


if __name__ == "__main__":
    unittest.main()
