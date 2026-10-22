"""اعتبارسنجی API روی حساب واقعی (کاربر اجرا می‌کند). خروجی logs/api_validation.json شرط باز شدن live است.
سفارش آزمایشی: LIMIT خرید به قیمت ۵۰٪ زیر بازار با حداقل مبلغ مجاز؛ فقط با --yes-place-test-order؛
بلافاصله لغو و لغو تأیید می‌شود. این تنها سفارشی است که پیش از باز شدن دروازه live ارسال می‌شود."""
from __future__ import annotations
import json, os, time, uuid
from .exchange import Wallex, fmt_price, fmt_qty, ROUND_DOWN, ROUND_UP
from . import schemas as S
from .safety import REQUIRED_CHECKS, schemas_hash, ROOT


class RecordingWallex(Wallex):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.samples = {}

    def _send(self, method, path, params=None, body=None, auth=False):
        status, data = super()._send(method, path, params, body, auth)
        self.samples.setdefault(f"{method} {path.split('?')[0]}".replace("/", "_").replace(" ", ""), []).append(
            {"status": status, "request_params": params, "request_body": body, "response": data})
        return status, data


def run_selfcheck(cfg, place_test_order=False, ex=None):
    ex = ex or RecordingWallex(cfg.wallex_api_key)
    checks = {}
    sym = cfg.symbols[0]

    def check(name, fn):
        try:
            checks[name] = {"ok": True, "detail": fn()}
        except Exception as e:
            checks[name] = {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    def c_markets():
        mk = ex.markets(max_age=0)
        miss = [s for s in cfg.symbols if s not in mk]
        if miss:
            raise RuntimeError(f"symbols missing: {miss}")
        return {s: vars(mk[s]) for s in cfg.symbols[:2]}

    def c_candles():
        cs = ex.candles(sym, cfg.resolution, 150)
        if len(cs) < 100:
            raise RuntimeError(f"only {len(cs)} candles")
        return {"n": len(cs), "last": cs[-1]}

    def c_balances():
        b = ex.balances()
        if cfg.quote_asset not in b:
            raise RuntimeError(f"{cfg.quote_asset} balance missing")
        return {k: vars(v) for k, v in b.items() if v.total > 0}

    check("markets", c_markets)
    check("depth", lambda: vars(ex.book(sym)))
    check("candles", c_candles)
    check("balances", c_balances)
    check("fee", lambda: {k: v for k, v in list(ex.fee().items())[:2]})
    check("open_orders", lambda: len(ex.open_orders()))
    state = {}
    if place_test_order:
        def c_place():
            mi, bk = ex.market(sym), ex.book(sym)
            price = float(fmt_price(mi, bk.bid * 0.5, ROUND_DOWN))
            qty = float(fmt_qty(mi, max(mi.min_qty, mi.min_notional * 1.2 / price), ROUND_UP))
            free = ex.balances()[cfg.quote_asset].free
            if qty * price < mi.min_notional or qty * price > free:
                raise RuntimeError(f"test order notional {qty * price:.0f} outside [{mi.min_notional}, free={free}]")
            cid = "wtb-selfcheck-" + uuid.uuid4().hex[:12]
            info = ex.place(mi, "BUY", fmt_price(mi, price), fmt_qty(mi, qty), cid)
            state.update(info=info, sent=cid, mi=mi)
            if not info.active or info.executed_qty != 0:
                raise RuntimeError(f"test order unexpectedly not resting: {info}")
            return {"returned_id": info.client_id, "id_matches_sent": info.client_id == cid, "status": info.status}

        def c_status():
            info = state["info"]
            got = ex.order(info.client_id)
            if got.client_id != info.client_id or not got.active:
                raise RuntimeError(f"status mismatch {got}")
            if info.client_id not in [o.client_id for o in ex.open_orders(state["mi"].symbol)]:
                raise RuntimeError("order not in openOrders")
            return {"status": got.status}

        def c_cancel():
            xid = state["info"].client_id
            ex.cancel(xid)
            for _ in range(10):
                time.sleep(1)
                got = ex.order(xid)
                if not got.active:
                    locked = ex.balances()[cfg.quote_asset].total - ex.balances()[cfg.quote_asset].free
                    return {"final_status": got.status, "locked_after": locked}
            raise RuntimeError("cancel not confirmed within 10s")

        check("order_place", c_place)
        check("order_status", c_status if "order_place" in checks and checks["order_place"]["ok"] else
              (lambda: (_ for _ in ()).throw(RuntimeError("skipped: place failed"))))
        check("order_cancel", c_cancel if state.get("info") else
              (lambda: (_ for _ in ()).throw(RuntimeError("skipped: place failed"))))
        if state.get("info") and not checks["order_cancel"]["ok"]:
            try:
                ex.cancel(state["info"].client_id)          # تلاش نهایی؛ کاربر باید دستی هم چک کند
            except Exception:
                pass
    else:
        for n in ("order_place", "order_status", "order_cancel"):
            checks[n] = {"ok": False, "detail": "not run (use --yes-place-test-order)"}
    rep = {"source": "live_api", "ts": time.time(), "validator_version": S.VERSION, "schemas_hash": schemas_hash(),
           "checks": checks, "all_passed": all(checks.get(k, {}).get("ok") for k in REQUIRED_CHECKS)}
    os.makedirs(cfg.log_dir, exist_ok=True)
    with open(os.path.join(cfg.log_dir, "api_validation.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2, ensure_ascii=False, default=str)
    if hasattr(ex, "samples"):
        d = os.path.join(ROOT, "tests", "fixtures", "real")
        os.makedirs(d, exist_ok=True)
        for k, v in ex.samples.items():
            with open(os.path.join(d, k + ".json"), "w", encoding="utf-8") as f:
                json.dump(v, f, indent=1, ensure_ascii=False)
    return rep
