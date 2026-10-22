"""فیلدهای تشخیصی لاگ ورود (sl_pct, est_loss_tmn, rr). فقط ثبت‌اند؛ منطق معامله را تغییر نمی‌دهند."""
import json, math, os, unittest
from wallexbot.bot import entry_risk_fields
from .fakes import *


class EntryRiskFields(unittest.TestCase):
    def test_normal_values_and_units(self):
        r = entry_risk_fields(1_000_000, 0.012, 0.0312)          # ۱ میلیون تومان، استاپ ۱٫۲٪، تارگت ۳٫۱۲٪
        self.assertEqual(r["sl_pct"], 0.012)
        self.assertEqual(r["est_loss_tmn"], 12_000)                # 1,000,000 × 0.012
        self.assertEqual(r["rr"], 2.6); self.assertEqual(r["rr_status"], "ok")

    def test_invalid_sl_never_divides_by_zero_or_invents_values(self):
        for bad in (0, 0.0, -0.01, float("nan"), float("inf"), None, "0.01", True):
            r = entry_risk_fields(1_000_000, bad, 0.03)
            self.assertIsNone(r["rr"], bad); self.assertIsNone(r["est_loss_tmn"], bad)
            self.assertEqual(r["rr_status"], "invalid_sl_pct", bad)

    def test_invalid_tp_or_value_is_explicit(self):
        for bad in (-0.01, float("nan"), None):
            r = entry_risk_fields(1_000_000, 0.012, bad)
            self.assertIsNone(r["rr"]); self.assertEqual(r["rr_status"], "invalid_tp_pct")
            self.assertEqual(r["est_loss_tmn"], 12_000)            # زیان به tp وابسته نیست
        for bad in (float("nan"), -5, None):
            self.assertIsNone(entry_risk_fields(bad, 0.012, 0.03)["est_loss_tmn"])


class BuyLogIntegration(unittest.TestCase):
    def test_buy_row_has_consistent_risk_fields(self):
        cfg = tcfg(mode="shadow")
        bot, ex, clock = make_bot(cfg)
        bot.step(); self.assertIn("BTCTMN", bot.positions)
        with open(os.path.join(cfg.log_dir, "decisions.jsonl"), encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh]
        buy = [r for r in rows if r["action"] == "BUY"][0]
        for k in ("sl_pct", "est_loss_tmn", "rr", "rr_status"):
            self.assertIn(k, buy)
        self.assertEqual(buy["rr_status"], "ok")
        self.assertGreaterEqual(buy["sl_pct"], cfg.sl_min - 1e-9); self.assertLessEqual(buy["sl_pct"], cfg.sl_max + 1e-9)
        self.assertAlmostEqual(buy["est_loss_tmn"], buy["notional"] * buy["sl_pct"], delta=buy["notional"] * 5e-6 + 1)
        # سازگاری با استاپ ثبت‌شده: stop ≈ fill × (1 − sl_pct) و tp ≈ fill × (1 + sl_pct × rr)
        self.assertAlmostEqual(buy["stop"], buy["fill_price"] * (1 - buy["sl_pct"]), delta=buy["fill_price"] * 1e-4)
        self.assertAlmostEqual(buy["take_profit"], buy["fill_price"] * (1 + buy["sl_pct"] * buy["rr"]),
                               delta=buy["fill_price"] * 2e-4)
        pos = bot.positions["BTCTMN"]
        self.assertAlmostEqual(pos.sl_pct, buy["sl_pct"], places=4)   # همان مقدار ذخیره‌شده در Position


if __name__ == "__main__":
    unittest.main()
