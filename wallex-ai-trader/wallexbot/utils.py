from __future__ import annotations
import csv, json, os, time, logging
from datetime import datetime, timezone


def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return logging.getLogger("wtb")


def iso(ts=None):
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).isoformat(timespec="seconds")


class DecisionLog:
    """لاگ تصمیم (JSONL): برای هر اقدام، سیگنال و دلیل ثبت می‌شود — جزو تحویلی چالش."""
    TRADE_FIELDS = ["open_ts", "close_ts", "symbol", "kind", "entry", "exit", "qty",
                    "cost_tmn", "proceeds_tmn", "pnl_tmn", "pnl_pct", "exit_reason"]

    def __init__(self, log_dir):
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, "decisions.jsonl")
        self.trades = os.path.join(log_dir, "trades.csv")

    def write(self, **kw):
        kw = {"ts": iso(), **kw}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(kw, ensure_ascii=False, default=str) + "\n")

    def trade(self, row: dict):
        new = not os.path.exists(self.trades)
        with open(self.trades, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.TRADE_FIELDS)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k) for k in self.TRADE_FIELDS})
