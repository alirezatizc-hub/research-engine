from __future__ import annotations
import dataclasses, json, math, os, time, logging
from .config import Config
from .exchange import Wallex, HTTPFailure, TransientError
from .schemas import SchemaError
from .executors import ShadowExecutor, LiveExecutor
from .orders import OrderManager, ExecResult, Halted, NotAllowed
from .safety import LiveGate, LiveBlocked
from .strategy import evaluate
from .risk import RiskManager, Position, check_exit
from .ai_agent import AIAgent
from .learner import Learner, make_record
from .utils import DecisionLog, iso

log = logging.getLogger("wtb")
RECOVERABLE = (Halted, NotAllowed, LiveBlocked, ValueError, HTTPFailure, TransientError, SchemaError, KeyError)


def closed_only(candles, res_min, now):
    return [c for c in candles if c["t"] + res_min * 60 <= now + 1]


def entry_risk_fields(value, sl_pct, tp_pct):
    """فیلدهای تشخیصی ورود برای لاگ (فقط ثبت؛ روی هیچ تصمیمی اثر ندارد).
    value  = مبلغ پرشده به تومان، بدون کارمزد (executed_sum)
    sl_pct = فاصله‌ی استاپ به‌صورت کسر قیمت ورود (مثلاً 0.012 = ۱٫۲٪)؛ tp_pct همین‌طور
    est_loss_tmn = value × sl_pct: زیان قیمتیِ رسیدن به استاپ (بدون کارمزد و لغزش)
    rr = tp_pct / sl_pct. اگر sl_pct صفر/منفی/نامعتبر باشد: مقدار None و دلیل در rr_status (بدون تقسیم بر صفر)."""
    def ok(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
    sl_ok = ok(sl_pct) and sl_pct > 0
    out = {"sl_pct": round(sl_pct, 5) if ok(sl_pct) else None,
           "est_loss_tmn": round(value * sl_pct) if sl_ok and ok(value) and value >= 0 else None}
    if not sl_ok:
        out["rr"], out["rr_status"] = None, "invalid_sl_pct"
    elif not (ok(tp_pct) and tp_pct >= 0):
        out["rr"], out["rr_status"] = None, "invalid_tp_pct"
    else:
        out["rr"], out["rr_status"] = round(tp_pct / sl_pct, 2), "ok"
    return out


class Bot:
    def __init__(self, cfg: Config, client=None, agent=None, now=time.time, sleep=time.sleep, gate=None):
        errs = cfg.validate()
        if errs:
            raise SystemExit("config نامعتبر: " + "; ".join(errs))
        self.now, self.sleep = now, sleep
        self.cfg_state_path = cfg.state_path
        self.client = client or Wallex(cfg.wallex_api_key, now=now, sleep=sleep)
        self.dlog = DecisionLog(cfg.log_dir)
        self.state = self._load()
        self.journal = self.state.setdefault("journal", {})
        self.allowed: set = set()
        self.market_ok, self.mkt_reason = False, "not checked"
        self._last_mkt = self._last_recon = 0.0
        self.snapshot: dict = {}
        if cfg.mode == "live":
            self.gate = gate or LiveGate(cfg, now)
            self.gate.assert_open()                      # raises LiveBlocked => ربات live بالا نمی‌آید
            try:                                         # کارمزد واقعی؛ محافظه‌کارانه: بیشتر از config و API
                taker = max(v["taker"] for v in self.client.fee().values())
                cfg = dataclasses.replace(cfg, fee_rate=max(cfg.fee_rate, taker))
            except Exception as e:
                log.warning("fee endpoint failed, using config fee: %s", e)
        else:
            self.gate = None
        self.cfg = cfg
        self.positions = {s: Position(**d) for s, d in self.state.get("positions", {}).items()}
        self.risk = RiskManager(cfg, self.state.setdefault("risk", {}))
        self.learner = Learner(cfg, self.state.setdefault("learner", {}))
        self.agent = agent or AIAgent(cfg)
        self.recent_trades = self.state.setdefault("recent_trades", [])
        self.cooldown = self.state.setdefault("cooldown", {})
        if cfg.mode == "live":
            self.om = OrderManager(self.client, cfg, self.journal, self.save, self.gate, now, sleep,
                                   allowed=lambda: self.allowed, held=lambda: set(self.positions), fee_rate=cfg.fee_rate)
            self.broker = LiveExecutor(self.client, self.om, cfg)
        else:
            self.om = None
            self.broker = ShadowExecutor(cfg, self.state.get("shadow_cash", cfg.initial_capital),
                                         self.state.get("shadow_hold"))
        self.refresh_markets()
        if self.om:
            self.reconcile()

    # ------------------------------------------------------------ state
    @property
    def eff(self):
        return self.learner.effective_cfg()

    def _load(self):
        if os.path.exists(self.cfg_state_path):
            with open(self.cfg_state_path, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def save(self):
        self.state["positions"] = {s: dataclasses.asdict(p) for s, p in self.positions.items()}
        if isinstance(self.broker, ShadowExecutor):
            self.state["shadow_cash"], self.state["shadow_hold"] = self.broker._cash, self.broker.hold
        tmp = self.cfg.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, self.cfg.state_path)

    # ------------------------------------------------------------ markets / recovery
    def refresh_markets(self):
        self._last_mkt = self.now()
        try:
            mk = self.client.markets(max_age=0)
            ok = {s for s in self.cfg.symbols if s in mk and mk[s].quote == self.cfg.quote_asset}
            miss = set(self.cfg.symbols) - ok
            if miss:
                log.warning("configured symbols missing on exchange: %s", sorted(miss))
            self.allowed, self.market_ok = ok, bool(ok)
            self.mkt_reason = None if ok else "none of the configured symbols exist on the exchange"
        except Exception as e:                           # فهرست تأییدنشده هرگز استفاده نمی‌شود
            self.allowed, self.market_ok, self.mkt_reason = set(), False, f"market list unavailable: {e}"
            log.error("market verification failed -> new entries blocked: %s", e)
            self.dlog.write(action="MARKETS_UNVERIFIED", reason=str(e))

    def _price_of(self, sym):
        try:
            return self.client.book(sym).bid
        except Exception:
            return None

    def reconcile(self):
        self._last_recon = self.now()
        issues = self.om.reconcile(self.positions, self._price_of, self._apply_journal_entry)
        self.dlog.write(action="RECONCILE", issues=issues)
        self.save()
        return issues

    def _apply_journal_entry(self, e):
        if e.get("applied"):
            return
        value = e["executed_sum"]
        if e["side"] == "BUY":
            self.apply_buy(e["symbol"], e["net_qty"], e["avg_price"], value, value * self.cfg.fee_rate, e["meta"], e["id"])
        else:
            res = ExecResult("FILLED", e["net_qty"], e["avg_price"], value, value * self.cfg.fee_rate, e["id"])
            self.apply_sell(e["symbol"], res, (e["meta"] or {}).get("reason", "recovered"), e["id"])

    # ------------------------------------------------------------ accounting
    def equity(self, books):
        e = self.broker.cash()
        for s, p in self.positions.items():
            b = books.get(s)
            e += p.qty * (b.bid if b else (p.last_px or p.entry))
        return e

    def _books(self):
        out = {}
        for s in self.positions:
            try:
                out[s] = self.client.book(s)
            except Exception as e:
                log.warning("book %s: %s", s, e)
        return out

    def _min_notional(self, sym):
        try:
            return max(self.cfg.min_notional, self.client.market(sym).min_notional)
        except Exception:
            return self.cfg.min_notional

    def apply_buy(self, sym, qty, price, value, fee, meta, jid=None):
        eff, now = self.eff, self.now()
        atr = (meta or {}).get("atr_pct", 0.01)
        stop, tp, sl_pct, tp_pct = self.risk.levels(price, atr)
        cost = value + fee
        if sym in self.positions:                          # ورود جزئی قبلی: ادغام
            p = self.positions[sym]
            p.entry = (p.entry * p.qty + price * qty) / (p.qty + qty)
            p.qty += qty
            p.cost_tmn += cost
        else:
            self.positions[sym] = Position(sym, qty, price, now, stop, tp, price, (meta or {}).get("kind", "?"), cost,
                                           "; ".join((meta or {}).get("reasons", [])), low=price, sl_pct=sl_pct,
                                           tp_pct=tp_pct, feat=(meta or {}).get("features"), last_px=price)
        if jid and jid in self.journal:
            self.journal[jid]["applied"] = True
        self.dlog.write(action="BUY", symbol=sym, qty=qty, fill_price=price, notional=round(value), stop=round(stop, 2),
                        take_profit=round(tp, 2), kind=(meta or {}).get("kind"), why=(meta or {}).get("reasons"),
                        ai=(meta or {}).get("ai"), order_id=jid, **entry_risk_fields(value, sl_pct, tp_pct))
        self.save()

    def apply_sell(self, sym, res, reason, jid=None):
        pos = self.positions.get(sym)
        if jid and jid in self.journal:
            self.journal[jid]["applied"] = True
        if not pos:
            self.dlog.write(action="SELL_ORPHAN", symbol=sym, qty=res.qty, reason=reason)
            self.save()
            return
        frac = min(1.0, res.qty / pos.qty) if pos.qty else 1.0
        cost_part = pos.cost_tmn * frac
        pos.proceeds_acc += res.value - res.fee
        pos.cost_sold += cost_part
        pos.cost_tmn -= cost_part
        pos.qty -= res.qty
        self.dlog.write(action="SELL", symbol=sym, qty=res.qty, fill_price=res.avg_price, reason=reason,
                        partial=pos.qty > 1e-12, order_id=jid)
        if pos.qty <= 1e-12 or pos.qty * res.avg_price < self.cfg.dust_tmn:
            self._finish_position(sym, pos, res.avg_price, reason)
        self.save()

    def _finish_position(self, sym, pos, px, reason):
        cost = pos.cost_sold + pos.cost_tmn                # باقی‌مانده خاک = زیان ثبت‌شده (محافظه‌کارانه)
        pnl = pos.proceeds_acc - cost
        pct = 100 * pnl / cost if cost else 0.0
        now = self.now()
        self.dlog.trade({"open_ts": iso(pos.entry_ts), "close_ts": iso(now), "symbol": sym, "kind": pos.kind,
                         "entry": pos.entry, "exit": px, "qty": pos.qty, "cost_tmn": round(cost),
                         "proceeds_tmn": round(pos.proceeds_acc), "pnl_tmn": round(pnl), "pnl_pct": round(pct, 3),
                         "exit_reason": reason})
        bars = int((now - pos.entry_ts) / (self.cfg.resolution * 60))
        rec = make_record(pos, now, px, pct, reason, bars)
        for ev in self.learner.record(rec):
            self.dlog.write(action="LEARN", **ev)
        self.recent_trades.append({"symbol": sym, "pnl_pct": round(pct, 3), "reason": reason, "tag": rec.get("tag")})
        del self.recent_trades[:-10]
        if pnl < 0:
            self.cooldown[sym] = now + self.cfg.cooldown_min * 60
        del self.positions[sym]
        if self.cfg.learn_enabled and self.learner.s["n_total"] % self.cfg.learn_every == 0:
            r = self.agent.reflect({"learner": self.learner.summary(), "lessons": self.learner.lessons(3)})
            if r:
                self.learner.add_lesson(r["lesson"], now)
                self.dlog.write(action="AI_REFLECTION", **r)

    # ------------------------------------------------------------ actions
    def close(self, sym, book, reason):
        pos = self.positions[sym]
        try:
            mi = self.client.market(sym)
            if pos.qty * book.bid < max(self.cfg.min_notional, mi.min_notional):
                self.dlog.write(action="DUST_ABANDONED", symbol=sym, qty=pos.qty, reason="below exchange minimum")
                self._finish_position(sym, pos, book.bid, reason + "+dust")
                self.save()
                return True
            res = self.broker.sell(sym, pos.qty, book, mi, {"reason": reason})
        except RECOVERABLE as e:
            self.dlog.write(action="SELL_BLOCKED", symbol=sym, reason=reason, error=str(e))
            return False
        if res.status in ("FILLED", "PARTIAL") and res.qty > 0:
            self.apply_sell(sym, res, reason, res.journal_id)
            return True
        self.dlog.write(action="SELL_NOT_FILLED", symbol=sym, status=res.status, reason=reason)
        return False

    def exit_all(self, books, reason):
        for sym in list(self.positions):
            b = books.get(sym)
            if b is None:
                try:
                    b = self.client.book(sym)
                except Exception as e:
                    log.warning("flatten %s: no book (%s)", sym, e); continue
            for _ in range(3):
                if sym not in self.positions or self.close(sym, b, reason):
                    break
                if self.om and self.om.halted:
                    return
                self.sleep(2)

    def _ctx(self, candles, book, equity):
        closes = [c["c"] for c in candles[-13:]]
        return {"recent_closes_pct": [round(100 * (x / closes[0] - 1), 2) for x in closes],
                "book": {"spread_pct": round(100 * book.spread, 3)}, "market_snapshot": self.snapshot,
                "open_positions": list(self.positions), "recent_trades": self.recent_trades[-5:],
                "lessons": self.learner.lessons(3),
                "drawdown_pct": round(100 * (1 - equity / self.risk.s["peak"]), 2) if self.risk.s["peak"] else 0,
                "risk_mode": self.risk.s["ai_mode"]}

    def entry_blocked(self, equity, now):
        end = self.cfg.end_ts()
        if end and now >= end - self.cfg.stop_entries_h * 3600:
            return "entries closed (end of challenge window)"
        if not self.market_ok:
            return f"allowed markets unverified: {self.mkt_reason}"
        if self.om and self.om.halted:
            return f"order manager halted: {self.om.halted}"
        return self.risk.entry_block(equity, len(self.positions))

    def scan(self, equity, now):
        cfg, eff = self.cfg, self.eff
        cands = []
        for sym in [s for s in cfg.symbols if s in self.allowed]:
            if sym in self.positions or self.cooldown.get(sym, 0) > now:
                continue
            try:
                cs = closed_only(self.client.candles(sym, cfg.resolution, cfg.candle_count), cfg.resolution, now)
                sig, f = evaluate(sym, cs, eff)
            except RECOVERABLE as e:
                log.warning("scan %s: %s", sym, e); self.dlog.write(action="SCAN_ERROR", symbol=sym, error=str(e)); continue
            if f:
                self.snapshot[sym] = {k: f[k] for k in ("rsi", "trend_gap", "ret_1h", "ret_4h", "atr_pct")}
            if not sig:
                continue
            ok, why = self.learner.allowed(sym, sig.kind, now)
            thr = eff.min_score + self.learner.score_bias(sig.kind)
            if not ok or sig.score < thr:
                self.dlog.write(action="SKIP", symbol=sym, kind=sig.kind, score=sig.score,
                                reason=why if not ok else f"score<{thr}")
                continue
            cands.append((sig, cs))
        cands.sort(key=lambda x: -x[0].score)
        self.dlog.write(action="SCAN", candidates=[(s.symbol, s.kind, s.score) for s, _ in cands],
                        equity=round(equity), risk_mode=self.risk.s["ai_mode"], params=self.learner.s["overrides"])
        for sig, cs in cands:
            if self.entry_blocked(equity, now):
                break
            try:
                book = self.client.book(sig.symbol)
                mi = self.client.market(sig.symbol)
            except RECOVERABLE as e:
                log.warning("book/market %s: %s", sig.symbol, e); continue
            if book.spread > cfg.max_spread:
                self.dlog.write(action="SKIP", symbol=sig.symbol, reason=f"spread {book.spread:.4f}"); continue
            if self.agent.enabled:
                ai = self.agent.decide(sig, self._ctx(cs, book, equity))
                if ai is None:                           # fail-closed
                    self.dlog.write(action="AI_FAILED", symbol=sig.symbol, reason="no valid AI answer -> no entry"); continue
            else:
                ok = sig.score >= cfg.rules_min_score
                ai = {"approve": ok, "confidence": sig.score / 100, "size_mult": 0.7, "source": "rules_only",
                      "reason": f"rules-only: score {sig.score} vs {cfg.rules_min_score}"}
            self.dlog.write(action="DECISION", symbol=sig.symbol, kind=sig.kind, score=sig.score,
                            reasons=sig.reasons, features=sig.features, ai=ai)
            if not ai["approve"]:
                continue
            _, _, sl_pct, _ = self.risk.levels(book.ask, sig.atr_pct)
            notional = self.risk.size(equity, self.broker.cash(), sl_pct) * ai["size_mult"]
            if notional < max(cfg.min_notional, mi.min_notional) * 1.02:
                self.dlog.write(action="SKIP", symbol=sig.symbol, reason=f"notional {notional:.0f} below minimum"); continue
            meta = {"kind": sig.kind, "atr_pct": sig.atr_pct, "reasons": sig.reasons, "features": sig.features,
                    "score": sig.score, "ai": ai}
            try:
                res = self.broker.buy(sig.symbol, notional, book, mi, meta)
            except RECOVERABLE as e:
                self.dlog.write(action="BUY_BLOCKED", symbol=sig.symbol, error=str(e)); continue
            if res.status in ("FILLED", "PARTIAL") and res.qty > 0:
                self.apply_buy(sig.symbol, res.qty, res.avg_price, res.value, res.fee, meta, res.journal_id)
            else:
                self.dlog.write(action="BUY_NOT_FILLED", symbol=sig.symbol, status=res.status)

    def review(self, equity, now):
        r = self.agent.review({"equity": round(equity), "peak": round(self.risk.s["peak"]),
                               "market_snapshot": self.snapshot, "open_positions": list(self.positions),
                               "recent_trades": self.recent_trades, "current_mode": self.risk.s["ai_mode"]})
        if r is None:                                    # خطا => حالت قبلی بدون تغییر (pause به normal برنمی‌گردد)
            self.dlog.write(action="AI_REVIEW_FAILED", kept_mode=self.risk.s["ai_mode"])
            return
        cur = self.risk.s["ai_mode"]
        if cur == "pause" and r["risk_mode"] != "pause" and now - self.risk.s["pause_since"] < self.cfg.pause_min_minutes * 60:
            self.dlog.write(action="AI_REVIEW_IGNORED", wanted=r["risk_mode"], reason="pause minimum duration")
            return
        if r["risk_mode"] == "pause" and cur != "pause":
            self.risk.s["pause_since"] = now
        self.risk.s["ai_mode"] = r["risk_mode"]
        self.dlog.write(action="AI_REVIEW", **r)

    # ------------------------------------------------------------ main loop
    def step(self):
        cfg, now = self.cfg, self.now()
        end = cfg.end_ts()
        if now - self._last_mkt > 600:
            self.refresh_markets()
        if self.om and self.om.halted:
            if now - self._last_recon >= 30:
                self.reconcile()
            if self.om.halted:
                self.dlog.write(action="HALTED", reason=self.om.halted); self.save(); return True
        self.risk.cfg = self.eff
        books = self._books()
        equity = self.equity(books)
        self.risk.update(equity, now)
        finishing = bool(end and now >= end - cfg.flatten_min * 60)
        if (finishing or self.risk.killed) and self.positions:
            why = "end_of_challenge" if finishing else "kill_switch"
            self.dlog.write(action="FLATTEN", reason=why, equity=round(equity))
            self.exit_all(books, why)
            self.save()
            return True
        for sym, pos in list(self.positions.items()):
            b = books.get(sym)
            if b:
                ex = check_exit(pos, b.bid, now, self.eff)
                if ex:
                    self.close(sym, b, ex[0])
        if not self.positions and (finishing or self.risk.killed):
            self.dlog.write(action="STOP", reason="end_of_challenge" if finishing else "kill_switch_flat")
            self.save()
            return False
        block = self.entry_blocked(equity, now)
        bucket = int(now // (cfg.resolution * 60))
        if bucket != self.state.get("last_bucket") and now % (cfg.resolution * 60) >= 8:
            self.state["last_bucket"] = bucket
            if block:
                self.dlog.write(action="SCAN_SKIP", reason=block, equity=round(equity))
            else:
                self.scan(equity, now)
        if not block and self.agent.enabled and now - self.state.get("last_review", 0) > cfg.review_interval_min * 60:
            self.state["last_review"] = now
            self.review(equity, now)
        self.save()
        log.info("equity=%.0f pos=%s mode=%s block=%s", equity, list(self.positions), self.risk.s["ai_mode"], block)
        return True

    def run(self):
        log.info("start mode=%s allowed=%s end=%s", self.cfg.mode, sorted(self.allowed), self.cfg.challenge_end or "-")
        while True:
            try:
                if not self.step():
                    break
            except KeyboardInterrupt:
                self.save(); break
            except Exception as e:
                log.exception("step error: %s", e)
                self.dlog.write(action="ERROR", error=str(e))
            self.sleep(self.cfg.poll_s)
        self.finish()

    def finish(self):
        try:
            cash = self.broker.cash()
        except Exception:
            cash = None
        self.dlog.write(action="FINISHED", final_tmn=cash, open_positions=list(self.positions))
        log.info("finished. final TMN=%s open=%s", cash, list(self.positions))
