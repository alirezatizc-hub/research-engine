"""OrderManager: ثبت سفارش با ژورنال write-ahead، تأیید لغو، سفارش ناقص، خطای شبکه و بازیابی پس از ری‌استارت.
اصل: اگر وضعیت نامعلوم ماند => halted (هیچ سفارش جدیدی ثبت نمی‌شود) تا reconcile() همه چیز را روشن کند."""
from __future__ import annotations
import time, uuid, logging
from dataclasses import dataclass
from .exchange import HTTPFailure, NotFound, AmbiguousOrder, TransientError

log = logging.getLogger("wtb")


class Halted(Exception):
    pass


class NotAllowed(Exception):
    pass


@dataclass
class ExecResult:
    status: str            # FILLED | PARTIAL | NONE | REJECTED | UNKNOWN
    qty: float = 0.0       # حجم خالص دریافتی/فروخته‌شده (خرید: پس از کسر کارمزد کوین)
    avg_price: float = 0.0
    value: float = 0.0     # تومان
    fee: float = 0.0       # تخمینی (تومان)
    journal_id: str | None = None


class OrderManager:
    def __init__(self, ex, cfg, journal, persist, gate=None, now=time.time, sleep=time.sleep,
                 allowed=lambda: set(), held=lambda: set(), fee_rate=None):
        self.ex, self.cfg, self.journal, self.persist, self.gate = ex, cfg, journal, persist, gate
        self.now, self.sleep, self.allowed, self.held = now, sleep, allowed, held
        self.fee_rate = fee_rate if fee_rate is not None else cfg.fee_rate
        self.halted: str | None = None

    # ------------------------------------------------------------ helpers
    def _halt(self, why):
        self.halted = why
        log.error("ORDER MANAGER HALTED: %s", why)

    def _total(self, asset):
        b = self.ex.balances().get(asset)
        return b.total if b else 0.0

    def _cancel_confirm(self, xid):
        """لغو + تأیید با GET (active=false). None یعنی تأیید نشد."""
        for attempt in range(2):
            try:
                self.ex.cancel(xid)
            except NotFound:
                pass
            except (HTTPFailure, TransientError) as e:
                log.warning("cancel %s failed: %s", xid, e)
            for _ in range(self.cfg.cancel_confirm_tries):
                self.sleep(self.cfg.order_poll_s)
                try:
                    info = self.ex.order(xid)
                except (NotFound, HTTPFailure, TransientError) as e:
                    log.warning("confirm-poll %s: %s", xid, e)
                    continue
                if not info.active:
                    return info
        return None

    def _resolve(self, e):
        """پس از خطای مبهم ثبت: سفارش را با شناسه خودمان پیدا کن."""
        for i in range(4):
            try:
                return self.ex.order(e["id"])
            except NotFound:
                self.sleep(1 + i)
            except (HTTPFailure, TransientError):
                self.sleep(1 + i)
        try:
            cands = [o for o in self.ex.open_orders(e["symbol"])
                     if o.side == e["side"] and abs(o.orig_qty - e["qty_req"]) <= 1e-9 * max(1, e["qty_req"])]
            known = {x.get("xid") for x in self.journal.values()}
            cands = [o for o in cands if o.client_id not in known]
            if len(cands) == 1:
                return cands[0]
        except (HTTPFailure, TransientError):
            pass
        return None

    def _finalize(self, e, info):
        e["xid"], e["executed_qty"], e["executed_sum"] = info.client_id, info.executed_qty, info.executed_sum
        e["avg_price"] = info.avg_price
        net = info.executed_qty
        if e["side"] == "BUY" and info.executed_qty > 0:
            try:
                measured = self._total(e["base"]) - e["bal_before"]
                if measured <= 0.5 * info.executed_qty:
                    e["note"] = f"balance mismatch after buy (measured={measured})"
                    self._halt(e["note"])
                elif measured < info.executed_qty:
                    net = measured                       # کارمزد از خود کوین کسر شده
            except (HTTPFailure, TransientError) as err:
                self._halt(f"cannot verify balance after buy: {err}")
        e["net_qty"], e["state"] = net, "TERMINAL"
        self.persist()
        fill = info.executed_qty / info.orig_qty if info.orig_qty else 0
        st = "FILLED" if fill >= 0.999 else ("PARTIAL" if info.executed_qty > 0 else "NONE")
        value = info.executed_sum
        return ExecResult(st, net, info.avg_price, value, value * self.fee_rate, e["id"])

    # ------------------------------------------------------------ main API
    def execute(self, mi, side, qty, price, intent, meta=None):
        if self.gate is not None:
            self.gate.assert_open(check_age=False)          # live gate: قبل از هر تماس شبکه
        if self.halted:
            raise Halted(self.halted)
        sym = mi.symbol
        if side == "BUY" and sym not in self.allowed():
            raise NotAllowed(f"{sym} not in verified allowed markets")
        if side == "SELL" and sym not in self.allowed() and sym not in self.held():
            raise NotAllowed(f"{sym} not allowed and not held")
        if qty <= 0 or qty < mi.min_qty or qty * price < mi.min_notional:
            raise ValueError(f"order below exchange minimum (qty={qty}, notional={qty * price:.0f})")
        from .exchange import fmt_qty, fmt_price
        bal_before = self._total(mi.base)                   # اگر خطا بدهد، هنوز سفارشی ارسال نشده
        cid = "wtb-" + uuid.uuid4().hex[:20]
        e = {"id": cid, "xid": None, "symbol": sym, "base": mi.base, "side": side, "intent": intent,
             "qty_req": qty, "price": price, "state": "PENDING_SEND", "ts": self.now(),
             "bal_before": bal_before, "meta": meta or {}, "executed_qty": 0.0, "net_qty": 0.0,
             "executed_sum": 0.0, "avg_price": 0.0, "applied": False}
        self.journal[cid] = e
        self.persist()                                      # write-ahead
        try:
            info = self.ex.place(mi, side, fmt_price(mi, price), fmt_qty(mi, qty), cid)
        except HTTPFailure as err:                          # رد قطعی
            e["state"], e["applied"], e["note"] = "TERMINAL", True, f"rejected: {err}"
            self.persist()
            return ExecResult("REJECTED", journal_id=cid)
        except AmbiguousOrder as err:
            log.warning("ambiguous place %s: %s", cid, err)
            info = self._resolve(e)
            if info is None:
                e["state"] = "UNRESOLVED"
                self.persist()
                self._halt(f"ambiguous place unresolved: {cid}")
                return ExecResult("UNKNOWN", journal_id=cid)
        e["xid"], e["state"] = info.client_id, "SENT"
        self.persist()
        deadline = self.now() + self.cfg.fill_timeout_s
        while info.active and self.now() < deadline:
            self.sleep(self.cfg.order_poll_s)
            try:
                info = self.ex.order(e["xid"])
            except (NotFound, HTTPFailure, TransientError) as err:
                log.warning("poll %s: %s", e["xid"], err)
        if info.active:
            done = self._cancel_confirm(e["xid"])
            if done is None:
                e["state"] = "UNRESOLVED"
                self.persist()
                self._halt(f"cancel not confirmed: {e['xid']}")
                return ExecResult("UNKNOWN", journal_id=cid)
            info = done
        return self._finalize(e, info)

    # ------------------------------------------------------------ recovery
    def reconcile(self, positions, price_of, apply_cb):
        """وضعیت را با صرافی یکی می‌کند. خروجی: فهرست مشکلات؛ خالی = همه چیز شناخته‌شده است."""
        issues = []
        try:
            for e in list(self.journal.values()):
                if e["state"] == "TERMINAL":
                    if not e["applied"] and e["executed_qty"] > 0:
                        apply_cb(e)
                    continue
                info = None
                for oid in [x for x in (e["xid"], e["id"]) if x]:
                    try:
                        info = self.ex.order(oid)
                        break
                    except NotFound:
                        continue
                if info is None:        # روی صرافی نیست: ثبت نشده فرض می‌شود؛ پشتوانه = مقایسه موجودی در ادامه
                    e.update(state="TERMINAL", applied=True, note="not found on exchange; assumed never placed")
                    continue
                if info.active:
                    info = self._cancel_confirm(info.client_id)
                    if info is None:
                        issues.append(f"cannot confirm cancel of {e['id']}")
                        continue
                self._finalize(e, info)
                if e["executed_qty"] > 0 and not e["applied"]:
                    apply_cb(e)
            known = {e["id"] for e in self.journal.values()} | {e["xid"] for e in self.journal.values() if e["xid"]}
            for o in self.ex.open_orders():
                if o.client_id in known:
                    continue
                if self.cfg.cancel_unknown_orders and self._cancel_confirm(o.client_id) is not None:
                    log.warning("unknown open order cancelled: %s %s", o.client_id, o.symbol)
                    self.journal[f"unknown-{o.client_id}"] = {
                        "id": f"unknown-{o.client_id}", "xid": o.client_id, "symbol": o.symbol, "side": o.side,
                        "state": "TERMINAL", "applied": True, "executed_qty": 0.0, "note": "unknown order cancelled"}
                else:
                    issues.append(f"unknown active order {o.client_id} ({o.symbol})")
            bal = self.ex.balances()
            q = self.cfg.quote_asset
            for asset, b in bal.items():
                if asset == q:
                    continue
                sym = asset + q
                exp = positions[sym].qty if sym in positions else 0.0
                diff, tol = b.total - exp, max(1e-9, 0.02 * exp)
                if diff > tol:
                    px = price_of(sym)
                    if px is None or diff * px >= self.cfg.dust_tmn:
                        issues.append(f"unknown holding {asset}: {diff}")
                elif diff < -tol:
                    issues.append(f"position {sym} exceeds balance by {-diff}")
            for sym, p in positions.items():
                if sym[:-len(q)] not in bal:
                    issues.append(f"position {sym} has no balance")
        except (HTTPFailure, TransientError) as err:
            issues.append(f"reconcile API error: {err}")
        self.persist()
        self.halted = "; ".join(issues) if issues else None
        return issues
