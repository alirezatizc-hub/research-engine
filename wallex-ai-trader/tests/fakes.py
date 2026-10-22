"""صرافی جعلی با ماشین‌حالت سفارش و تزریق خطا (بدون شبکه)."""
import os, tempfile
from collections import Counter
from wallexbot.config import Config
from wallexbot.schemas import MarketInfo, Book, Balance, OrderInfo
from wallexbot.exchange import NotFound, AmbiguousOrder, TransientError, HTTPFailure
from wallexbot.backtest import synthetic

T0 = 1_800_000_010          # مضرب ۹۰۰ + ۱۰ ثانیه => اسکن مجاز


class Clock:
    def __init__(self, t=T0):
        self.t = float(t)
    def now(self):
        return self.t
    def sleep(self, dt):
        self.t += dt


class FakeGate:
    def assert_open(self, check_age=True):
        return None


def dip_candles(clock):
    cs = synthetic(400, seed=5)
    last = cs[-1]["c"]
    for k in range(10):
        last *= 0.9965
        cs[-11 + k].update(o=last / 0.9965, c=last, h=last / 0.9965, l=last * 0.999, v=300)
    prev = cs[-2]["c"]
    cs[-1].update(o=prev, c=prev * 1.012, h=prev * 1.013, l=prev * 0.999, v=500)
    end = int(clock.now()) - 900
    for i, c in enumerate(cs):
        c["t"] = end - (len(cs) - 1 - i) * 900
    return cs


class FakeExchange:
    def __init__(self, clock, price=None):
        self.clock, self.calls = clock, Counter()
        self.cs = dip_candles(clock)
        self.price = price or self.cs[-1]["c"]
        self.mk = {s: MarketInfo(s, s[:-3], "TMN", 6, 0, 1e-6, 1000.0) for s in ("BTCTMN", "DOGETMN")}
        self.bal = {"TMN": 5_000_000.0}
        self.orders = {}
        self.fill_fraction, self.rest_if_partial = 1.0, True
        self.fail_markets = False
        self.raise_before_accept = self.raise_after_accept = 0
        self.cancel_ignored = False
        self.base_fee = 0.0

    # --- public ---
    def markets(self, max_age=600):
        self.calls["markets"] += 1
        if self.fail_markets:
            raise TransientError("markets down")
        return self.mk
    def market(self, s):
        return self.mk[s]
    def book(self, s):
        return Book(self.price * 0.9995, self.price * 1.0005, 1, 1)
    def candles(self, s, res=15, count=220, to_ts=None):
        return list(self.cs)
    def fee(self):
        return {"BTCTMN": {"maker": 0.002, "taker": 0.002}}
    # --- private ---
    def balances(self):
        self.calls["balances"] += 1
        return {a: Balance(v, v) for a, v in self.bal.items()}
    def _info(self, o):
        st = "NEW" if o["active"] and o["exq"] == 0 else ("PARTIALLY_FILLED" if o["active"] else
             ("FILLED" if o["exq"] >= o["qty"] * 0.999999 else "CANCELED"))
        return OrderInfo(o["id"], o["symbol"], o["side"], st, o["active"], o["price"], o["qty"], o["exq"], o["exq"] * o["price"])
    def _fill(self, o, frac):
        q = o["qty"] * frac
        base = o["symbol"][:-3]
        if o["side"] == "BUY":
            self.bal["TMN"] -= q * o["price"]; self.bal[base] = self.bal.get(base, 0) + q * (1 - self.base_fee)
        else:
            self.bal[base] -= q; self.bal["TMN"] += q * o["price"]
        o["exq"] = q
        o["active"] = frac < 0.999999 and self.rest_if_partial
    def place(self, mi, side, price_s, qty_s, cid):
        self.calls["place"] += 1
        if self.raise_before_accept:
            self.raise_before_accept -= 1
            raise AmbiguousOrder("timeout before accept")
        o = {"id": cid, "symbol": mi.symbol, "side": side, "price": float(price_s), "qty": float(qty_s), "exq": 0.0, "active": True}
        self.orders[cid] = o
        if self.fill_mode_none:
            pass
        else:
            self._fill(o, self.fill_fraction)
        if self.raise_after_accept:
            self.raise_after_accept -= 1
            raise AmbiguousOrder("timeout after accept")
        return self._info(o)
    fill_mode_none = False
    def order(self, cid):
        self.calls["order"] += 1
        if cid not in self.orders:
            raise NotFound(404, "no such order")
        return self._info(self.orders[cid])
    def cancel(self, cid):
        self.calls["cancel"] += 1
        if cid not in self.orders:
            raise NotFound(404)
        if not self.cancel_ignored:
            self.orders[cid]["active"] = False
    def open_orders(self, symbol=None):
        return [self._info(o) for o in self.orders.values() if o["active"] and (symbol in (None, o["symbol"]))]


def tcfg(**kw):
    d = tempfile.mkdtemp()
    base = dict(mode="live", symbols=["BTCTMN"], ai_enabled=False, rules_min_score=0.0, min_score=0.0,
                min_atr_pct=0.0, rsi_buy=60.0, bb_max_pos=0.5, max_trend_dev=1.0, min_notional=1000.0,
                state_path=os.path.join(d, "state.json"), log_dir=d, fill_timeout_s=5, order_poll_s=1.0,
                cancel_confirm_tries=3, dust_tmn=1000.0)
    base.update(kw)
    return Config(**base)


def make_bot(cfg=None, ex=None, clock=None, agent=None, gate="fake"):
    from wallexbot.bot import Bot
    clock = clock or Clock()
    ex = ex or FakeExchange(clock)
    cfg = cfg or tcfg()
    bot = Bot(cfg, client=ex, agent=agent, now=clock.now, sleep=clock.sleep, gate=FakeGate() if gate == "fake" else gate)
    return bot, ex, clock
