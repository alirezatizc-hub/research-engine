"""تست‌های استراتژی با داده مصنوعی کنترل‌شده: سیگنال/ورود/خروج مورد انتظار از قوانین مستند معلوم است."""
import random, unittest
from wallexbot import backtest as bt
from wallexbot.config import Config
from wallexbot.strategy import evaluate, features, diagnose
from .scenarios import *

CFG = Config()                       # پیش‌فرض‌ها؛ هیچ آستانه‌ای دستکاری نمی‌شود
SIG = lambda bars, cfg=CFG: evaluate("X", bars, cfg)[0]
failing = lambda bars, pat, cfg=CFG: [k for k, ok in diagnose(features(bars), cfg)[pat].items() if not ok]


def spec_levels(atr_pct, px0, cfg=CFG):
    """حد ضرر/سود طبق قانون مستند (مستقل از risk.py)."""
    sl = min(max(cfg.sl_atr * atr_pct, cfg.sl_min), cfg.sl_max)
    tp = max(cfg.tp_atr * atr_pct, cfg.tp_min, 4 * cfg.fee_rate)
    return px0 * (1 - sl), px0 * (1 + tp), sl, tp


class SignalRules(unittest.TestCase):
    def test_dip_signal_matches_documented_rules(self):
        b = dip_scenario()
        f, sig = features(b), SIG(b)
        # شرایط مستند ورود dip (README): RSI≤35، نزدیک باند پایین، کندل سبز، بدون ریزش شدید، ATR≥۰٫۶٪
        self.assertLessEqual(f["rsi"], CFG.rsi_buy); self.assertLessEqual(f["bb_pos"], CFG.bb_max_pos)
        self.assertTrue(f["green"]); self.assertGreater(f["trend_gap"], -CFG.max_trend_dev)
        self.assertGreater(f["ret_4h"], -0.06); self.assertGreaterEqual(f["atr_pct"], CFG.min_atr_pct)
        self.assertIsNotNone(sig); self.assertEqual(sig.kind, "dip")
        self.assertGreaterEqual(sig.score, max(CFG.rules_min_score, CFG.min_score))
        self.assertEqual(failing(b, "dip"), [])

    def test_dip_requires_each_condition(self):
        cases = {                                                     # دقیقاً یک شرط نقض می‌شود
            "green_candle": dip_scenario(bounce=-0.002),              # بدون کندل برگشتی
        }
        for cond, b in cases.items():
            self.assertIsNone(SIG(b), cond); self.assertEqual(failing(b, "dip"), [cond])
        b = dip_scenario(drops=1, drop=-0.002)                        # اصلاً اشباع فروش نیست
        self.assertIsNone(SIG(b)); self.assertIn("rsi<=rsi_buy", failing(b, "dip"))
        b = dip_scenario(drops=9, drop=-0.012)                        # چاقوی در حال سقوط
        self.assertIsNone(SIG(b)); self.assertTrue({"trend_gap>-max_trend_dev", "ret_4h>-6%"} & set(failing(b, "dip")))

    def test_breakout_signal_matches_documented_rules(self):
        b = breakout_scenario(); f, sig = features(b), SIG(b)
        self.assertTrue(f["breakout"]); self.assertGreaterEqual(f["vol_ratio"], 1.8); self.assertGreater(f["trend_gap"], 0)
        self.assertTrue(55 <= f["rsi"] <= 72); self.assertGreaterEqual(f["atr_pct"], CFG.min_atr_pct)
        self.assertIsNotNone(sig); self.assertEqual(sig.kind, "breakout")
        self.assertGreaterEqual(sig.score, max(CFG.rules_min_score, CFG.min_score))
        # کنترل‌های منفی: هر بار دقیقاً یک شرط نقض
        b = breakout_scenario(vol=100.0); self.assertIsNone(SIG(b)); self.assertEqual(failing(b, "breakout"), ["vol_ratio>=1.8"])
        b = breakout_scenario(jump=0.03); self.assertIsNone(SIG(b)); self.assertEqual(failing(b, "breakout"), ["55<=rsi<=72"])  # بیش‌خرید
        b = breakout_scenario(jump=0.002); self.assertIsNone(SIG(b)); self.assertIn("close>20bar_high", failing(b, "breakout"))

    def test_quiet_market_produces_zero_signals_because_of_atr_filter(self):
        b, n_atr_blocked = quiet_market(), 0
        for i in range(120, len(b)):
            sig, f = evaluate("X", b[: i + 1], CFG)
            self.assertIsNone(sig)
            n_atr_blocked += not diagnose(f, CFG)["atr_ok"]
        self.assertEqual(n_atr_blocked, len(b) - 120)               # علت: همه کندل‌ها زیر کف نوسان

    def test_diagnose_is_consistent_with_evaluate(self):
        """diagnose فقط توضیح می‌دهد؛ باید دقیقاً همان تصمیم evaluate را بازتاب دهد."""
        data = bt.synthetic(1500, seed=3) + []
        for i in range(120, len(data), 3):
            cs = data[: i + 1]
            sig, f = evaluate("X", cs, CFG); d = diagnose(f, CFG)
            dip, brk = all(d["dip"].values()), all(d["breakout"].values())
            expect = None if not d["atr_ok"] else ("dip" if dip else ("breakout" if brk else None))
            self.assertEqual(sig.kind if sig else None, expect, i)


class BacktestBehaviour(unittest.TestCase):
    def setUp(self):
        self.sig_bars = dip_scenario()
        self.K = len(self.sig_bars) - 1                                # کندل سیگنال
        sig = SIG(self.sig_bars)
        self.entry_px = self.sig_bars[-1]["c"] * (1 + CFG.slippage)    # open کندل بعد = close کندل سیگنال
        self.stop, self.tp, self.sl_pct, self.tp_pct = spec_levels(sig.atr_pct, self.entry_px)

    def run_path(self, kind):
        bars = path_after(self.sig_bars, kind, self.entry_px, self.stop, self.tp, self.tp_pct)
        return bars, bt.run_backtest({"X": bars}, CFG)

    def test_backtest_enters_next_bar_open_and_exits_at_take_profit(self):
        bars, (rep, trades, _, _) = self.run_path("tp")
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["entry_t"], bars[self.K + 1]["t"])                       # بعد از کندل سیگنال، نه خودِ آن
        self.assertAlmostEqual(t["entry"], self.entry_px, places=9)
        self.assertEqual(t["reason"], "take_profit"); self.assertEqual(t["exit_t"], bars[-1]["t"])
        self.assertAlmostEqual(t["exit"], self.tp * (1 - CFG.slippage), places=9)
        self.assertGreater(t["pnl_tmn"], 0)
        f = rep["funnel"]
        self.assertEqual((f["signal:dip"], f["passed_score:dip"], f["pending_entries_queued"], f["entries_executed"]), (1, 1, 1, 1))
        self.assertNotIn("signal:breakout", f)

    def test_final_liquidation_is_reflected_in_last_equity_point(self):
        """Final report and final equity-curve point must use the same post-liquidation cash."""
        bars = path_after(self.sig_bars, "flat", self.entry_px, self.stop, self.tp, self.tp_pct)
        rep, trades, curve, _ = bt.run_backtest({"X": bars}, CFG)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "final_liquidation")
        self.assertEqual(curve[-1][0], bars[-1]["t"])
        self.assertAlmostEqual(curve[-1][1], rep["final_tmn"], delta=0.5)
        self.assertAlmostEqual(curve[-1][1], 5_000_000 * (1 + rep["return_pct"] / 100), delta=30_000)

    def test_backtest_exits_at_stop_loss(self):
        bars, (rep, trades, _, _) = self.run_path("sl")
        self.assertEqual(len(trades), 1); t = trades[0]
        self.assertEqual(t["reason"], "stop_loss"); self.assertEqual(t["exit_t"], bars[-1]["t"])
        self.assertAlmostEqual(t["exit"], self.stop * (1 - CFG.slippage), places=9)   # open بالاتر از استاپ => خروج در استاپ
        self.assertLess(t["pnl_tmn"], 0)

    def test_intrabar_stop_wins_over_target_and_over_new_trailing(self):
        """کندلی که هم به تارگت می‌رسد هم زیر استاپ اولیه می‌رود: ترتیب نامعلوم => محافظه‌کارانه، خروج در استاپ اولیه."""
        o = self.sig_bars[-1]["c"]
        wild = self.sig_bars + [{"t": T0 + (self.K + 1) * RES, "o": o, "h": self.tp * 1.01,
                                 "l": self.stop * 0.99, "c": o, "v": 100.0}]
        _, trades, _, _ = bt.run_backtest({"X": wild}, CFG)
        self.assertEqual(len(trades), 1); t = trades[0]
        self.assertEqual(t["reason"], "stop_loss")
        self.assertAlmostEqual(t["exit"], self.stop * (1 - CFG.slippage), places=9)
        self.assertLess(t["pnl_tmn"], 0)

    def test_backtest_has_no_lookahead(self):
        """دو آینده‌ی کاملاً متفاوت (تارگت/سقوط) با پیشوند یکسان: ورود باید عیناً یکسان باشد."""
        b1, (_, t1, _, _) = self.run_path("tp")
        b2, (_, t2, _, _) = self.run_path("sl")
        self.assertNotEqual(b1[-1], b2[-1])
        self.assertEqual((t1[0]["entry_t"], t1[0]["entry"], t1[0]["kind"]), (t2[0]["entry_t"], t2[0]["entry"], t2[0]["kind"]))
        self.assertNotEqual(t1[0]["reason"], t2[0]["reason"])
        # آینده‌هایی که از «خودِ کندل بعد از سیگنال» متفاوت‌اند: موتور اگر یک کندل جلو را ببیند، ورود تغییر می‌کند
        o = self.sig_bars[-1]["c"]
        crash = self.sig_bars + [{"t": T0 + (self.K + 1) * RES, "o": o, "h": o, "l": o * 0.9, "c": o * 0.9, "v": 100.0}]
        calm = self.sig_bars + [bar(self.K + 1, o, o * 1.0003, wick=0.0005)]
        _, ta, _, _ = bt.run_backtest({"X": crash}, CFG); _, tb, _, _ = bt.run_backtest({"X": calm}, CFG)
        self.assertEqual(len(ta), 1); self.assertEqual(len(tb), 1)
        self.assertEqual((ta[0]["entry_t"], ta[0]["entry"]), (tb[0]["entry_t"], tb[0]["entry"]))
        self.assertNotEqual(ta[0]["pnl_tmn"], tb[0]["pnl_tmn"])
        # سیگنال هر کندل فقط از prefix تا همان کندل ساخته می‌شود: آینده‌ی تصادفی نباید تصمیم را تغییر دهد
        rnd = random.Random(7)
        for i in range(120, self.K + 1):
            a = SIG(self.sig_bars[: i + 1])
            noisy = self.sig_bars[: i + 1] + [{"t": 0, "o": 1, "h": 1e9, "l": 1e-9, "c": rnd.random() * 1e6, "v": 1e9}]
            b = SIG(noisy[:-1])
            self.assertEqual((a.kind, a.score) if a else None, (b.kind, b.score) if b else None)

    def test_quiet_market_backtest_has_zero_trades_with_exact_reason(self):
        b = quiet_market()
        rep, trades, _, _ = bt.run_backtest({"X": b}, CFG)
        f = rep["funnel"]
        self.assertEqual(trades, [])
        self.assertEqual(f["rejected:atr_below_min_atr_pct"], f["bars_evaluated"])
        self.assertNotIn("signal:dip", f); self.assertNotIn("signal:breakout", f)

    def test_signal_below_score_threshold_is_counted_not_traded(self):
        """سیگنال معتبر ولی با امتیاز زیر آستانه: رد با دلیل ثبت می‌شود (مکانیزم علت صفر معامله در داده مصنوعی تصادفی)."""
        cfg = Config(rules_min_score=95.0)
        rep, trades, _, _ = bt.run_backtest({"X": dip_scenario()}, cfg)
        f = rep["funnel"]
        self.assertEqual(trades, []); self.assertEqual(f["signal:dip"], 1)
        self.assertEqual(f["rejected:score<95 (dip)"], 1); self.assertNotIn("passed_score:dip", f)

    def test_breakout_scenario_enters_next_bar(self):
        b = breakout_scenario(); K = len(b) - 1
        sig = SIG(b); px0 = b[-1]["c"] * (1 + CFG.slippage)
        stop, tp, _, tpp = spec_levels(sig.atr_pct, px0)
        bars = path_after(b, "tp", px0, stop, tp, tpp)
        _, trades, _, _ = bt.run_backtest({"X": bars}, CFG)
        self.assertEqual(len(trades), 1)
        self.assertEqual((trades[0]["kind"], trades[0]["entry_t"], trades[0]["reason"]), ("breakout", bars[K + 1]["t"], "take_profit"))


if __name__ == "__main__":
    unittest.main()
