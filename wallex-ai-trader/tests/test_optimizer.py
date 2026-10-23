"""تست چرخهٔ بهینه‌سازی: تقسیم بی‌نشت، کران پارامترها، ادامهٔ کار بدون AI، و اعمال فقط با تأیید صریح."""
import dataclasses
import json
import os
import tempfile
import unittest

from wallexbot import optimizer as O
from wallexbot.backtest import synthetic
from wallexbot.config import Config
from wallexbot.strategy import evaluate


def small_cfg():
    cfg = Config(ai_enabled=False)
    cfg.explicit = {"max_drawdown", "symbols", "challenge_end"}
    return cfg


def tiny_data(n=900, symbols=("BTCTMN", "ETHTMN", "DOGTMN")):
    return {s: synthetic(n, seed=i + 1) for i, s in enumerate(symbols)}


class SplitWindows(unittest.TestCase):
    def test_three_contiguous_purged_windows(self):
        data = tiny_data()
        w, meta = O.split_windows(data, split=(0.6, 0.2, 0.2), purge_bars=8, min_trade_bars=40)
        tr, va, te = w["train"], w["validation"], w["test"]
        self.assertLessEqual(tr["t1"], va["t0"])          # بدون هم‌پوشانی
        self.assertLessEqual(va["t1"], te["t0"])
        gap = (va["t0"] - tr["t1"]) // 900
        self.assertGreaterEqual(gap, 7)                   # شکاف purge
        self.assertGreater(te["trade_bars"], 100)
        self.assertEqual(meta["bars"], 900)
        # هیچ کندلی در دو پنجره معامله نمی‌شود
        for s in data:
            a = {c["t"] for c in tr["trade"][s]}
            b = {c["t"] for c in va["trade"][s]}
            c = {c["t"] for c in te["trade"][s]}
            self.assertFalse(a & b)
            self.assertFalse(b & c)

    def test_context_only_contains_past(self):
        data = tiny_data()
        w, _ = O.split_windows(data)
        te = w["test"]
        self.assertTrue(all(c["t"] < te["t1"] for cs in te["context"].values() for c in cs))
        self.assertTrue(all(c["t"] >= te["trade_from"] for cs in te["trade"].values() for c in cs))

    def test_too_short_data_is_reported(self):
        with self.assertRaises(O.OptimizerError) as cm:
            O.split_windows(tiny_data(n=200))
        self.assertIn("fetch --days 30", str(cm.exception))


class Scoring(unittest.TestCase):
    cfg = small_cfg()

    def test_low_activity_is_rejected(self):
        rep = {"return_pct": 3.0, "max_drawdown_pct": 1.0, "trades": 1, "profit_factor": 3.0}
        m = O.metrics_of(rep, [], self.cfg, min_trades=2)
        self.assertIsNone(m["score"])
        self.assertIn("insufficient_trades", m["reject"])

    def test_drawdown_over_limit_is_rejected(self):
        rep = {"return_pct": 9.0, "max_drawdown_pct": 12.0, "trades": 10, "profit_factor": 4.0}
        m = O.metrics_of(rep, [], self.cfg, min_trades=2)
        self.assertIsNone(m["score"])
        self.assertIn("drawdown_over_limit", m["reject"])

    def test_better_risk_adjusted_result_scores_higher(self):
        a = O.metrics_of({"return_pct": 2.0, "max_drawdown_pct": 0.5, "trades": 6, "profit_factor": 1.6},
                         [], self.cfg, min_trades=2)
        b = O.metrics_of({"return_pct": 2.0, "max_drawdown_pct": 3.0, "trades": 6, "profit_factor": 1.6},
                         [], self.cfg, min_trades=2)
        self.assertGreater(a["score"], b["score"])


class ParamGuardrails(unittest.TestCase):
    cfg = small_cfg()

    def test_rejects_unknown_and_out_of_bounds(self):
        params, obj, issues = O.sanitize_params(self.cfg, {"leverage": 10, "rsi_buy": 500})
        self.assertIsNone(obj)
        self.assertTrue(any("unknown parameter" in i for i in issues))
        self.assertTrue(any("رsi" in i or "rsi_buy" in i for i in issues))

    def test_accepts_inside_bounds_and_fixes_min_score(self):
        params, obj, issues = O.sanitize_params(self.cfg, {"rsi_buy": 40, "rules_min_score": 50})
        self.assertEqual(issues, [])
        self.assertLessEqual(params["min_score"], params["rules_min_score"])
        self.assertEqual(obj.rsi_buy, 40)

    def test_ai_proposals_are_filtered(self):
        answer = {"diagnosis": "fees", "proposals": [
            {"name": "ok", "params": {"min_atr_pct": 0.003}},
            {"name": "evil", "params": {"rsi_buy": 999, "martingale": True}},
            {"name": "empty", "params": {}},
        ]}
        accepted, rejected = O.proposals_from_ai(answer, self.cfg, self.cfg)
        self.assertEqual([a["name"] for a in accepted], ["ai:ok"])
        self.assertTrue(rejected)


class FakeAI:
    last_error = None

    def __init__(self, answer=None, boom=False):
        self.answer, self.boom, self.payloads = answer, boom, []

    def optimize(self, payload):
        self.payloads.append(payload)
        if self.boom:
            raise RuntimeError("provider down")
        return self.answer


class Cycle(unittest.TestCase):
    def setUp(self):
        self.cfg = small_cfg()
        self.data = tiny_data()
        self.tmp = tempfile.mkdtemp()

    def scenarios(self):
        return [{"name": "baseline", "source": "seed", "params": {}},
                {"name": "tighter", "source": "seed",
                 "params": {"min_atr_pct": 0.003, "rules_min_score": 60.0}}]

    def test_cycle_produces_pending_proposal_and_hides_test_from_ai(self):
        ai = FakeAI({"diagnosis": "fees", "failure_modes": ["stop-outs"],
                     "proposals": [{"name": "ai1", "params": {"rsi_buy": 42.0}}]})
        rep = O.run_optimizer(self.data, self.cfg, data_label="TEST", rounds=2, min_trades=1,
                              ai=ai, scenarios=self.scenarios(), run_id="t1",
                              out_dir=os.path.join(self.tmp, "t1"))
        self.assertIn(rep["status"], ("pending_approval", "no_candidate"))
        self.assertEqual(rep["guardrails"]["test_used_for_selection"], False)
        self.assertEqual(rep["guardrails"]["auto_apply"], False)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "t1", "summary.md")))
        for payload in ai.payloads:                      # پنجرهٔ آزمون هرگز به AI داده نمی‌شود
            self.assertEqual(set(payload["windows"]), {"train", "validation"})
        names = [r["name"] for r in rep["scenarios"]]
        self.assertIn("ai:ai1", names)                   # پیشنهاد AI در دور دوم ارزیابی شد
        self.assertTrue(any(r["round"] == 2 for r in rep["scenarios"]))

    def test_ai_failure_does_not_stop_the_cycle(self):
        rep = O.run_optimizer(self.data, self.cfg, data_label="TEST", rounds=2, min_trades=1,
                              ai=FakeAI(boom=True), scenarios=self.scenarios(), run_id="t2",
                              out_dir=os.path.join(self.tmp, "t2"))
        self.assertEqual(rep["ai"][0]["ok"], False)
        self.assertIn("provider down", rep["ai"][0]["error"])
        self.assertTrue(any(r["source"] == "local_search" for r in rep["scenarios"]))

    def test_stop_request_aborts_before_writing(self):
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 2

        out = os.path.join(self.tmp, "t4")
        with self.assertRaises(O.OptimizerStopped):
            O.run_optimizer(self.data, self.cfg, rounds=1, min_trades=1, scenarios=self.scenarios(),
                            should_stop=stop, run_id="t4", out_dir=out)
        self.assertFalse(os.path.exists(os.path.join(out, "report.json")))

    def test_test_window_never_used_for_ranking(self):
        seen = []
        real = O.run_backtest

        def spy(data, cfg, **kw):
            seen.append(kw.get("label"))
            return real(data, cfg, **kw)

        O.run_backtest = spy
        try:
            rep = O.run_optimizer(self.data, self.cfg, data_label="TEST", rounds=1, min_trades=1,
                                  scenarios=self.scenarios(), run_id="t3",
                                  out_dir=os.path.join(self.tmp, "t3"))
        finally:
            O.run_backtest = real
        self.assertTrue(seen and seen[-1] == "optimizer:test")   # آزمون آخرین مرحله است
        self.assertLessEqual(seen.count("optimizer:test"), 2)    # فقط بهترین و مرجع
        self.assertIsNotNone(rep["final"]["best"])
        self.assertNotIn("test", json.dumps(rep["scenarios"][0]["params"]))


class Apply(unittest.TestCase):
    def setUp(self):
        self.cfg = small_cfg()
        self.root = tempfile.mkdtemp()
        self.logs = os.path.join(self.root, "logs")
        os.makedirs(self.logs, exist_ok=True)
        self.run_id = "opt-manual"
        d = os.path.join(self.root, O.OUT_ROOT, self.run_id)
        os.makedirs(d, exist_ok=True)
        self.prop = {"run_id": self.run_id, "status": "pending_approval", "selected": "tighter",
                     "source": "seed", "params": {"rsi_buy": 44.0, "kinds": ["dip"]},
                     "recommend_apply": True, "validation": {"return_pct": 1.0},
                     "held_out_test": {"return_pct": 0.5}}
        self.write_prop()
        self.env = os.path.join(self.root, ".env")
        with open(self.env, "w", encoding="utf-8") as f:
            f.write("WALLEX_API_KEY=secret\nWTB_LIVE_ACK=deadbeef\nWTB_RSI_BUY=45\n")

    def write_prop(self):
        with open(os.path.join(self.root, O.OUT_ROOT, self.run_id, "proposal.json"), "w", encoding="utf-8") as f:
            json.dump(self.prop, f)

    def test_requires_explicit_confirmation(self):
        with self.assertRaises(O.OptimizerError):
            O.apply_proposal(self.run_id, root=self.root, env_path=self.env, log_dir=self.logs, cfg=self.cfg)

    def test_apply_writes_env_clears_ack_and_logs(self):
        res = O.apply_proposal(self.run_id, root=self.root, env_path=self.env, log_dir=self.logs,
                               confirm=True, cfg=self.cfg)
        self.assertTrue(res["ok"])
        with open(self.env, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("WTB_RSI_BUY=44", body)
        self.assertIn("WTB_KINDS=dip", body)
        self.assertNotIn("WTB_LIVE_ACK", body)          # اثرانگشت عوض شده => ACK باطل
        self.assertIn("WALLEX_API_KEY=secret", body)    # بقیهٔ کلیدها دست‌نخورده
        self.assertTrue(os.path.exists(os.path.join(self.logs, "decisions.jsonl")))
        with open(os.path.join(self.root, O.OUT_ROOT, self.run_id, "proposal.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["status"], "applied")
        with self.assertRaises(O.OptimizerError):       # دوباره اعمال نمی‌شود
            O.apply_proposal(self.run_id, root=self.root, env_path=self.env, log_dir=self.logs,
                             confirm=True, cfg=self.cfg)

    def test_negative_validation_needs_force(self):
        self.prop["recommend_apply"] = False
        self.prop["warning"] = "validation negative"
        self.write_prop()
        with self.assertRaises(O.OptimizerError):
            O.apply_proposal(self.run_id, root=self.root, env_path=self.env, log_dir=self.logs,
                             confirm=True, cfg=self.cfg)
        res = O.apply_proposal(self.run_id, root=self.root, env_path=self.env, log_dir=self.logs,
                               confirm=True, force=True, cfg=self.cfg)
        self.assertTrue(res["ok"])


class KindsSwitch(unittest.TestCase):
    def test_kinds_filter_blocks_disabled_pattern(self):
        from tests.scenarios import dip_scenario, breakout_scenario
        cfg = small_cfg()
        bars = dip_scenario()
        self.assertIsNotNone(evaluate("X", bars, cfg)[0])
        cfg_dip_only = dataclasses.replace(cfg, kinds=["breakout"])
        self.assertIsNone(evaluate("X", bars, cfg_dip_only)[0])
        brk = breakout_scenario()
        cfg_dip_only = dataclasses.replace(cfg, kinds=["dip"])
        self.assertIsNone(evaluate("X", brk, cfg_dip_only)[0])
        cfg_dip_only = dataclasses.replace(cfg, kinds=["breakout"])
        self.assertIsNotNone(evaluate("X", brk, cfg_dip_only)[0])


if __name__ == "__main__":
    unittest.main()
