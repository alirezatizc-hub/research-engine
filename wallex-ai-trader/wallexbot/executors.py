from __future__ import annotations
from .exchange import fmt_qty, fmt_price, ROUND_DOWN, ROUND_UP
from .orders import ExecResult


class ShadowExecutor:
    """Shadow Testing: روی دفتر سفارش زنده، با موجودی مجازی، کارمزد و لغزش."""
    def __init__(self, cfg, cash, hold=None):
        self.cfg, self._cash, self.hold = cfg, float(cash), dict(hold or {})

    def cash(self):
        return self._cash

    def buy(self, symbol, notional, book, mi, meta=None):
        p = book.ask * (1 + self.cfg.slippage)
        qty = notional / p
        fee = notional * self.cfg.fee_rate
        if notional + fee > self._cash:
            return ExecResult("NONE")
        self._cash -= notional + fee
        self.hold[symbol] = self.hold.get(symbol, 0.0) + qty
        return ExecResult("FILLED", qty, p, notional, fee)

    def sell(self, symbol, qty, book, mi, meta=None):
        qty = min(qty, self.hold.get(symbol, 0.0))
        if qty <= 0:
            return ExecResult("NONE")
        p = book.bid * (1 - self.cfg.slippage)
        fee = qty * p * self.cfg.fee_rate
        self._cash += qty * p - fee
        self.hold[symbol] -= qty
        return ExecResult("FILLED", qty, p, qty * p, fee)


class LiveExecutor:
    """سفارش LIMIT قابل‌اجرا (marketable) از مسیر OrderManager."""
    def __init__(self, ex, om, cfg):
        self.ex, self.om, self.cfg = ex, om, cfg

    def cash(self):
        b = self.ex.balances().get(self.cfg.quote_asset)
        return b.free if b else 0.0

    def buy(self, symbol, notional, book, mi, meta=None):
        price = float(fmt_price(mi, book.ask * (1 + self.cfg.order_slip), ROUND_UP))
        qty = float(fmt_qty(mi, notional / price, ROUND_DOWN))
        return self.om.execute(mi, "BUY", qty, price, "entry", meta)

    def sell(self, symbol, qty, book, mi, meta=None):
        b = self.ex.balances().get(mi.base)
        q = float(fmt_qty(mi, min(qty, b.free if b else 0.0), ROUND_DOWN))
        price = float(fmt_price(mi, book.bid * (1 - self.cfg.order_slip), ROUND_DOWN))
        if q <= 0:
            return ExecResult("NONE")
        return self.om.execute(mi, "SELL", q, price, "exit", meta)
