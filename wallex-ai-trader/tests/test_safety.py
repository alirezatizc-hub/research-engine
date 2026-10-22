import json, os, unittest
from unittest import mock
from wallexbot import backtest as bt, schemas as S
from wallexbot.config import Config
from wallexbot.orders import OrderManager, Halted, NotAllowed
from wallexbot.safety import LiveGate, LiveBlocked, REQUIRED_CHECKS, REQUIRED_TESTS, code_hash, schemas_hash
from wallexbot.ai_agent import AIAgent, validate_gate, validate_review, _json_obj
from wallexbot.learner import Learner
from wallexbot.strategy import Signal
from .fakes import *


def enter(bot, ex):
    """یک ورود از مسیر scan واقعی."""
    bot.scan(bot.equity({}), bot.now())
    return bot.positions.get("BTCTMN")


class KillSwitch(unittest.TestCase):
    def test_kill_switch_live_flattens_blocks_entries_and_persists(self):
        cfg = tcfg()
        bot, ex, clock = make_bot(cfg)
        self.assertTrue(bot.step())                      # اسکن => ورود
        self.assertIn("BTCTMN", bot.positions)
        clock.sleep(30); ex.price *= 0.2                 # افت شدید => DD > 15٪
        bot.step()
        self.assertTrue(bot.risk.killed)
        self.assertEqual(bot.positions, {})
        with open(os.path.join(cfg.log_dir, "trades.csv")) as f:
            rows = f.read()
        self.assertIn("kill_switch", rows)
        placed = ex.calls["place"]
        self.assertIn("kill", bot.entry_blocked(5_000_000, clock.now()))
        clock.sleep(1000)
        bot.step()
        self.assertEqual(ex.calls["place"], placed)      # هیچ سفارش جدیدی
        bot2, _, _ = make_bot(cfg, ex, clock)            # ری‌استارت: وضعیت kill حفظ می‌شود
        self.assertTrue(bot2.risk.killed)
        self.assertFalse(bot2.step())                    # flat + killed => توقف

    def test_kill_switch_backtest_stops_entries_and_liquidates_conservatively(self):
        n, bars = 400, []
        px = 100.0
        for i in range(n):
            o = px
            px = 100.0 if i <= 202 else px * 0.99
            bars.append({"t": 1_700_000_000 + i * 900, "o": o, "h": max(o, px), "l": min(o, px), "c": px, "v": 100.0})
        cfg = Config(sl_min=0.5, sl_max=0.6, tp_min=2.0, max_hold_h=1e6, trail_activate=10.0, risk_per_trade=0.5,
                     max_drawdown=0.02, daily_loss_limit=0.01, min_notional=1000.0)
        sig = lambda s, cs, c: (Signal(s, "dip", 90.0, cs[-1]["c"], 0.01, {}, ["t"]), {}) if len(cs) > 120 else (None, None)
        with mock.patch.object(bt, "evaluate", sig):
            rep, trades, curve, _ = bt.run_backtest({"BTCTMN": bars}, cfg)
        self.assertIsNotNone(rep["kill_switch_at"])
        kills = [t for t in trades if t["reason"] == "kill_switch"]
        self.assertEqual(len(kills), 1)
        self.assertTrue(all(t["entry_t"] <= rep["kill_switch_at"] for t in trades), "ورود پس از kill")
        k = kills[0]
        self.assertGreater(k["exit_t"], rep["kill_switch_at"])          # حداقل یک کندل تأخیر
        i = next(j for j, b in enumerate(bars) if b["t"] == k["exit_t"])
        expect = min(bars[i]["o"], bars[i - 1]["c"]) * (1 - cfg.slippage * cfg.kill_slip_mult)
        self.assertAlmostEqual(k["exit"], expect, places=6)
        self.assertEqual(len(trades), 1)                                # بعد از kill معامله دیگری نیست


class Orders(unittest.TestCase):
    def test_cancel_is_confirmed_before_result(self):
        bot, ex, _ = make_bot()
        ex.fill_mode_none = True
        self.assertIsNone(enter(bot, ex))
        self.assertGreaterEqual(ex.calls["cancel"], 1)
        self.assertTrue(all(not o["active"] for o in ex.orders.values()))
        self.assertIsNone(bot.om.halted)

    def test_unconfirmed_cancel_halts_bot(self):
        bot, ex, clock = make_bot()
        ex.fill_mode_none, ex.cancel_ignored = True, True
        self.assertIsNone(enter(bot, ex))
        self.assertIn("cancel not confirmed", bot.om.halted)
        self.assertIn("UNRESOLVED", [e["state"] for e in bot.journal.values()])
        placed = ex.calls["place"]
        self.assertIn("halted", bot.entry_blocked(5e6, clock.now()))
        clock.sleep(1000); self.assertTrue(bot.step())
        self.assertEqual(ex.calls["place"], placed)
        with self.assertRaises(Halted):
            bot.broker.buy("BTCTMN", 1e6, ex.book("BTCTMN"), ex.market("BTCTMN"), {})

    def test_partial_fill_buy_creates_partial_position(self):
        bot, ex, _ = make_bot()
        ex.fill_fraction = 0.4
        pos = enter(bot, ex)
        o = next(iter(ex.orders.values()))
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos.qty, o["qty"] * 0.4, places=9)
        self.assertFalse(o["active"])                                    # باقی‌مانده لغو و تأیید شد
        self.assertTrue(all(e["applied"] for e in bot.journal.values()))
        self.assertAlmostEqual(ex.bal["BTC"], pos.qty, places=9)

    def test_partial_fill_sell_keeps_remainder_and_retries(self):
        bot, ex, clock = make_bot()
        pos = enter(bot, ex)
        q0 = pos.qty
        ex.fill_fraction = 0.5
        self.assertTrue(bot.close("BTCTMN", ex.book("BTCTMN"), "take_profit"))
        self.assertIn("BTCTMN", bot.positions)
        self.assertAlmostEqual(bot.positions["BTCTMN"].qty, q0 * 0.5, places=6)
        self.assertFalse(os.path.exists(os.path.join(bot.cfg.log_dir, "trades.csv")))   # هنوز بسته نشده
        ex.fill_fraction = 1.0
        self.assertTrue(bot.close("BTCTMN", ex.book("BTCTMN"), "take_profit"))
        self.assertNotIn("BTCTMN", bot.positions)
        with open(os.path.join(bot.cfg.log_dir, "trades.csv")) as f:
            rows = f.read().strip().splitlines()
        self.assertEqual(len(rows), 2)                                    # هدر + یک معامله تجمیعی
        self.assertLess(ex.bal.get("BTC", 0), 1e-6)       # فقط خاکِ گردکردن گام حجم

    def test_ambiguous_place_resolved_by_lookup_no_duplicate(self):
        bot, ex, _ = make_bot()
        ex.raise_after_accept = 1
        pos = enter(bot, ex)
        self.assertIsNotNone(pos)
        self.assertEqual(ex.calls["place"], 1)
        self.assertEqual(len(ex.orders), 1)
        self.assertIsNone(bot.om.halted)

    def test_ambiguous_place_unresolved_halts(self):
        bot, ex, clock = make_bot()
        ex.raise_before_accept = 1
        self.assertIsNone(enter(bot, ex))
        self.assertIn("unresolved", bot.om.halted)
        self.assertEqual(ex.calls["place"], 1)
        with self.assertRaises(Halted):
            bot.broker.buy("BTCTMN", 1e6, ex.book("BTCTMN"), ex.market("BTCTMN"), {})


class Restart(unittest.TestCase):
    def test_restart_recovers_filled_pending_buy(self):
        cfg = tcfg()
        bot, ex, clock = make_bot(cfg)
        bot.apply_buy = mock.Mock(side_effect=RuntimeError("crash after exchange fill"))
        with self.assertRaises(RuntimeError):
            bot.scan(bot.equity({}), clock.now())
        self.assertGreater(ex.bal["BTC"], 0)
        bot2, _, _ = make_bot(cfg, ex, clock)
        self.assertIn("BTCTMN", bot2.positions)
        self.assertAlmostEqual(bot2.positions["BTCTMN"].qty, ex.bal["BTC"], places=9)
        self.assertIsNone(bot2.om.halted)
        self.assertTrue(all(e["applied"] for e in bot2.journal.values()))

    def test_restart_cancels_unknown_open_orders(self):
        ex = FakeExchange(Clock())
        ex.orders["ghost"] = {"id": "ghost", "symbol": "BTCTMN", "side": "BUY", "price": 1.0, "qty": 1.0, "exq": 0.0, "active": True}
        bot, _, _ = make_bot(ex=ex)
        self.assertFalse(ex.orders["ghost"]["active"])
        self.assertIsNone(bot.om.halted)

    def test_restart_unknown_holdings_halts(self):
        ex = FakeExchange(Clock())
        ex.bal["BTC"] = 0.5
        bot, _, clock = make_bot(ex=ex)
        self.assertIn("unknown holding", bot.om.halted)
        self.assertIn("halted", bot.entry_blocked(5e6, clock.now()))
        bot.scan(5e6, clock.now()) if not bot.entry_blocked(5e6, clock.now()) else None
        self.assertEqual(ex.calls["place"], 0)
        ex2 = FakeExchange(Clock()); ex2.bal["BTC"] = 1e-9                # خاک مجاز است
        bot2, _, _ = make_bot(ex=ex2)
        self.assertIsNone(bot2.om.halted)


class Markets(unittest.TestCase):
    def test_disallowed_symbol_buy_rejected_without_http(self):
        bot, ex, _ = make_bot()
        self.assertEqual(bot.allowed, {"BTCTMN"})
        with self.assertRaises(NotAllowed):
            bot.broker.buy("DOGETMN", 1e6, ex.book("DOGETMN"), ex.mk["DOGETMN"], {})
        self.assertEqual(ex.calls["place"], 0)

    def test_markets_failure_blocks_entries(self):
        clock = Clock(); ex = FakeExchange(clock); ex.fail_markets = True
        bot, _, _ = make_bot(ex=ex, clock=clock)
        self.assertFalse(bot.market_ok); self.assertEqual(bot.allowed, set())
        self.assertIn("unverified", bot.entry_blocked(5e6, clock.now()))
        bot.step()
        self.assertEqual(ex.calls["place"], 0); self.assertEqual(bot.positions, {})
        ex.fail_markets = False; clock.sleep(700)
        bot.state["last_bucket"] = None
        bot.step()                                                        # بازار دوباره تأیید می‌شود
        self.assertTrue(bot.market_ok)


class Gate(unittest.TestCase):
    def test_live_gate_blocks_orders_without_validation(self):
        cfg = tcfg(); ex = FakeExchange(Clock())
        with self.assertRaises(LiveBlocked):
            from wallexbot.bot import Bot
            Bot(cfg, client=ex)                                           # دروازه واقعی، بدون فایل‌های تأیید
        om = OrderManager(ex, cfg, {}, lambda: None, gate=LiveGate(cfg), allowed=lambda: {"BTCTMN"})
        with self.assertRaises(LiveBlocked):
            om.execute(ex.mk["BTCTMN"], "BUY", 0.001, 1e9, "entry")
        self.assertEqual(ex.calls["place"] + ex.calls["balances"] + ex.calls["order"], 0)

    def _ok_setup(self):
        cfg = tcfg(wallex_api_key="k", anthropic_api_key="k", ai_enabled=True, challenge_end="2026-10-25T20:00:00+03:30")
        cfg.explicit = {"max_drawdown", "symbols", "challenge_end"}
        cfg.live_ack = cfg.fingerprint()
        now = 1_800_000_000.0
        val = {"source": "live_api", "ts": now - 100, "validator_version": S.VERSION, "schemas_hash": schemas_hash(),
               "all_passed": True, "checks": {k: {"ok": True} for k in REQUIRED_CHECKS}}
        tst = {"code_hash": code_hash(), "passed_tests": list(REQUIRED_TESTS), "failures": 0, "errors": 0}
        self._write(cfg, "api_validation.json", val)
        self._write(cfg, "safety_tests.json", tst)
        return cfg, now, val, tst

    def _write(self, cfg, name, obj):
        with open(os.path.join(cfg.log_dir, name), "w") as f:
            json.dump(obj, f)

    def test_live_gate_requires_all_conditions(self):
        cfg, now, val, tst = self._ok_setup()
        self.assertEqual(LiveGate(cfg, lambda: now).failures(), [])
        def broken(label, fn):
            c, n, v, t = self._ok_setup(); fn(c, v, t)
            self.assertTrue(LiveGate(c, lambda: n).failures(), label)
        broken("no explicit max_drawdown", lambda c, v, t: c.explicit.discard("max_drawdown"))
        broken("bad ack", lambda c, v, t: setattr(c, "live_ack", "x"))
        broken("no AI key", lambda c, v, t: setattr(c, "anthropic_api_key", ""))
        broken("no api key", lambda c, v, t: setattr(c, "wallex_api_key", ""))
        broken("check failed", lambda c, v, t: (v["checks"].__setitem__("order_cancel", {"ok": False}), self._write(c, "api_validation.json", v)))
        broken("stale schemas hash", lambda c, v, t: (v.__setitem__("schemas_hash", "old"), self._write(c, "api_validation.json", v)))
        broken("wrong source", lambda c, v, t: (v.__setitem__("source", "fixture"), self._write(c, "api_validation.json", v)))
        broken("code changed", lambda c, v, t: (t.__setitem__("code_hash", "old"), self._write(c, "safety_tests.json", t)))
        broken("missing test", lambda c, v, t: (t["passed_tests"].pop(), self._write(c, "safety_tests.json", t)))
        broken("failed test", lambda c, v, t: (t.__setitem__("failures", 1), self._write(c, "safety_tests.json", t)))
        broken("daily>=dd", lambda c, v, t: setattr(c, "daily_loss_limit", 0.2))
        self.assertTrue(LiveGate(cfg, lambda: now + 10 * 86400).failures())     # منقضی (فقط هنگام شروع)
        self.assertEqual(LiveGate(cfg, lambda: now + 10 * 86400).failures(check_age=False), [])


GOOD = {"approve": True, "confidence": 0.8, "size_mult": 0.8, "reason": "ok"}


class AI(unittest.TestCase):
    def _bot(self, transport):
        cfg = tcfg(mode="shadow", ai_enabled=True, anthropic_api_key="k")
        agent = AIAgent(cfg, transport=transport)
        return make_bot(cfg, agent=agent)

    def test_llm_error_does_not_reset_pause(self):
        def boom(s, p): raise TimeoutError("down")
        bot, ex, clock = self._bot(boom)
        bot.risk.s.update(ai_mode="pause", pause_since=clock.now() - 10_000)
        bot.review(5e6, clock.now())
        self.assertEqual(bot.risk.s["ai_mode"], "pause")
        bot.agent._transport = lambda s, p: "garbage"
        bot.review(5e6, clock.now()); self.assertEqual(bot.risk.s["ai_mode"], "pause")
        bot.agent._transport = lambda s, p: '{"risk_mode": "normal", "note": "calm"}'
        bot.risk.s["pause_since"] = clock.now() - 60                      # کمتر از حداقل مدت pause
        bot.review(5e6, clock.now()); self.assertEqual(bot.risk.s["ai_mode"], "pause")
        bot.risk.s["pause_since"] = clock.now() - 10_000
        bot.review(5e6, clock.now()); self.assertEqual(bot.risk.s["ai_mode"], "normal")

    def test_llm_invalid_types_rejected(self):
        self.assertIsNotNone(validate_gate(dict(GOOD)))
        for bad in ({"approve": "false"}, {"approve": 1}, {"confidence": 1.5}, {"confidence": True}, {"size_mult": 0.2},
                    {"size_mult": float("nan")}, {"size_mult": "0.8"}, {"reason": ""}, {"reason": 5}):
            self.assertIsNone(validate_gate({**GOOD, **bad}), bad)
        self.assertIsNone(validate_gate({k: v for k, v in GOOD.items() if k != "reason"}))
        self.assertIsNone(validate_gate([GOOD]))
        for bad in ({"risk_mode": "PAUSE"}, {"risk_mode": None}, {"risk_mode": "normal", "note": 3}):
            self.assertIsNone(validate_review(bad), bad)
        self.assertEqual(validate_review({"risk_mode": "pause"})["risk_mode"], "pause")
        self.assertEqual(_json_obj('blah {"a": 1} tail {"b": 2}'), {"a": 1})
        self.assertIsNone(_json_obj("no json"))

    def test_ai_gate_fails_closed(self):
        for answer in ("garbage", '{"approve": "true", "confidence": 0.9, "size_mult": 1, "reason": "x"}'):
            bot, ex, _ = self._bot(lambda s, p, a=answer: a)
            bot.scan(bot.equity({}), bot.now())
            self.assertEqual(bot.positions, {}, answer)
        def boom(s, p): raise ConnectionError("x")
        bot, ex, _ = self._bot(boom)
        bot.scan(bot.equity({}), bot.now()); self.assertEqual(bot.positions, {})
        bot, ex, _ = self._bot(lambda s, p: json.dumps(GOOD))            # پاسخ معتبر => ورود
        bot.scan(bot.equity({}), bot.now()); self.assertIn("BTCTMN", bot.positions)


class Schema(unittest.TestCase):
    def test_schema_rejects_malformed_responses(self):
        env = lambda r: {"success": True, "result": r}
        ok_book = env({"ask": [{"price": "101", "quantity": 1}], "bid": [{"price": "100", "quantity": 1}]})
        S.parse_book(ok_book)
        bad = [env({"ask": [{"price": "99", "quantity": 1}], "bid": [{"price": "100", "quantity": 1}]}),   # crossed
               env({"ask": [], "bid": [{"price": "100", "quantity": 1}]}),
               env({"ask": [{"price": "x", "quantity": 1}], "bid": [{"price": "1", "quantity": 1}]}),
               {"success": False, "result": {}}, {"result": {}}]
        for b in bad:
            with self.assertRaises(S.SchemaError): S.parse_book(b)
        t = [1000, 1900, 2800, 3700]
        good = {"s": "ok", "t": t, "o": ["1"] * 4, "h": ["2"] * 4, "l": ["1"] * 4, "c": ["1.5"] * 4, "v": ["1"] * 4}
        self.assertEqual(len(S.parse_candles(good, 15)), 4)
        for mut in ({"s": "no_data"}, {"v": ["1"] * 3}, {"h": ["0.5"] * 4}, {"t": [1000, 1000, 2800, 3700]}, {"o": ["abc"] * 4}):
            with self.assertRaises(S.SchemaError): S.parse_candles({**good, **mut}, 15)
        with self.assertRaises(S.SchemaError): S.parse_candles(good, 60)                      # رزولوشن نامطابق
        with self.assertRaises(S.SchemaError):
            S.parse_balances(env({"balances": {"TMN": {"value": "1", "locked": "2"}}}))
        with self.assertRaises(S.SchemaError):
            S.parse_balances(env({"balances": {"TMN": {"value": "1"}}}))
        o = {"symbol": "BTCTMN", "side": "BUY", "price": "100", "origQty": "1", "executedQty": "0", "executedSum": "0",
             "status": "NEW", "active": True, "clientOrderId": "a"}
        S.parse_order(o)
        for mut in ({"active": "true"}, {"executedQty": "2"}, {"side": "buy"}, {"status": "FILLED", "active": True},
                    {"executedQty": "1", "executedSum": "150"}, {"clientOrderId": ""}, {"origQty": "0"}):
            with self.assertRaises(S.SchemaError): S.parse_order({**o, **mut})
        mk = {"symbols": {"A": {"symbol": "A", "baseAsset": "A", "quoteAsset": "TMN", "stepSize": 2, "tickSize": 0, "minQty": 1}}}
        with self.assertRaises(S.SchemaError): S.parse_markets(env(mk))                        # minNotional گم‌شده


class Learning(unittest.TestCase):
    def rec(self, n, pnl=-1.0, reason="stop_loss", mfe=1.5, kind="dip", sym=None, ts=None):
        return {"ts": ts or 1e9 + n * 900, "symbol": sym or f"S{n}", "kind": kind, "pnl_pct": pnl, "reason": reason,
                "hold_bars": 10, "mae_pct": -1.5, "mfe_pct": mfe, "tp_pct": 2.0, "sl_pct": 1.5, "feat": {}}

    def test_learner_stays_within_bounds_and_rolls_back(self):
        cfg = Config()
        L = Learner(cfg, {}); evs = []
        for n in range(1, 9):
            evs += L.record(self.rec(n))
        tune = [e for e in evs if e["event"] == "tune"]
        self.assertEqual(len(tune), 1); self.assertEqual(tune[0]["param"], "tp_atr")
        self.assertAlmostEqual(L.effective_cfg().tp_atr, cfg.tp_atr - 0.1)
        evs = []
        for n in range(9, 17):
            evs += L.record(self.rec(n, pnl=-2.0))                       # بدتر از baseline
        self.assertIn("rollback", [e["event"] for e in evs])
        self.assertAlmostEqual(L.effective_cfg().tp_atr, cfg.tp_atr)
        self.assertGreater(L.s["frozen"]["tp_atr"], L.s["n_total"])
        L2 = Learner(cfg, {})                                             # کران‌ها در برابر تکرار طولانی
        for n in range(1, 301):
            L2.record(self.rec(n))
            e = L2.effective_cfg()
            self.assertGreaterEqual(e.tp_atr, 1.8 - 1e-9); self.assertLessEqual(e.sl_atr, 2.4 + 1e-9)
            self.assertGreaterEqual(e.rsi_buy, 25.0); self.assertLessEqual(e.max_trend_dev, 0.06 + 1e-9)
        L3 = Learner(cfg, {"overrides": {"tp_atr": 99.0, "evil": 1}})     # state دست‌کاری‌شده
        self.assertEqual(L3.effective_cfg().tp_atr, 3.6)
        self.assertNotIn("evil", L3.s["overrides"])

    def test_learner_disables_losing_symbol(self):
        L = Learner(Config(), {})
        for n in range(3):
            ev = L.record(self.rec(n, sym="BTCTMN"))
        self.assertTrue(any(e["event"] == "disable" for e in ev))
        self.assertFalse(L.allowed("BTCTMN", "dip", 1e9 + 1000)[0])
        self.assertTrue(L.allowed("BTCTMN", "dip", 1e9 + 13 * 3600 + 3000)[0])
        self.assertTrue(L.allowed("ETHTMN", "dip", 1e9 + 1000)[0])
        for n in range(10, 16):
            L.record(self.rec(n))
        self.assertEqual(L.score_bias("dip"), 5.0)
        self.assertEqual(L.score_bias("breakout"), 0.0)


if __name__ == "__main__":
    unittest.main()
