"""تست‌های Replay: ساعت مجازی، عدم دیدن آینده، و دفتر سفارش مشتق‌شده از کندل بسته."""
import os
import tempfile
import unittest

from wallexbot.backtest import synthetic
from wallexbot.replay import ReplayExchange, VirtualClock, load_replay_data


class VirtualClockTests(unittest.TestCase):
    def test_time_advances_with_speed_and_sleep_is_short(self):
        c = VirtualClock(1_000_000.0, speed=600)
        t0 = c.now()
        c.sleep(15)
        self.assertGreaterEqual(c.now(), t0)


class ReplayExchangeTests(unittest.TestCase):
    def setUp(self):
        self.data = {"BTCTMN": synthetic(140, seed=3), "ETHTMN": synthetic(140, seed=4)}
        self.clock = VirtualClock(self.data["BTCTMN"][120]["t"], speed=600)
        self.ex = ReplayExchange(self.data, self.clock)

    def test_markets_and_book(self):
        mk = self.ex.markets()
        self.assertIn("BTCTMN", mk)
        self.assertEqual(mk["BTCTMN"].quote, "TMN")
        b = self.ex.book("BTCTMN")
        self.assertLess(b.bid, b.ask)
        self.assertLess(b.spread, 0.004)

    def test_candles_never_include_unclosed_bar(self):
        now = self.clock.now()
        cs = self.ex.candles("BTCTMN", 15, 140)
        self.assertTrue(cs)
        self.assertTrue(all(c["t"] + 900 <= now + 1e-6 for c in cs), "future candle leaked")
        self.assertEqual(cs, sorted(cs, key=lambda c: c["t"]))

    def test_only_15m_supported(self):
        with self.assertRaises(ValueError):
            self.ex.candles("BTCTMN", 5, 140)

    def test_load_falls_back_to_labeled_synthetic(self):
        data, label = load_replay_data(os.path.join(tempfile.mkdtemp(), "missing"), ["BTCTMN", "ETHTMN"])
        self.assertEqual(label, "SYNTHETIC_NOT_EVIDENCE")
        self.assertEqual(set(data), {"BTCTMN", "ETHTMN"})
        self.assertGreater(len(data["BTCTMN"]), 100)


if __name__ == "__main__":
    unittest.main()
