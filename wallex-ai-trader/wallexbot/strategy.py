"""سیگنال‌ساز کمّی (قابل توضیح و قطعی). فقط خرید اسپات (Long-only)."""
from __future__ import annotations
from dataclasses import dataclass, field
from .indicators import sma, stdev, ema_series, rsi, atr


@dataclass
class Signal:
    symbol: str
    kind: str                 # dip | breakout
    score: float
    price: float
    atr_pct: float
    features: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)


def features(c, bars_per_hour=4):
    if len(c) < 110:
        return None
    closes = [x["c"] for x in c]
    vols = [x["v"] for x in c]
    px = closes[-1]
    ma, sd = sma(closes, 20), stdev(closes, 20)
    lower, upper = ma - 2 * sd, ma + 2 * sd
    e96 = ema_series(closes, 96)[-1]
    a = atr(c, 14)
    base_vol = sma(vols[-21:-1], 20) or 1e-9
    hh = max(x["h"] for x in c[-21:-1])
    b1, b4 = bars_per_hour, bars_per_hour * 4
    return {
        "price": px,
        "rsi": round(rsi(closes), 1),
        "bb_pos": round((px - lower) / (upper - lower), 3) if upper > lower else 0.5,
        "z": round((px - ma) / sd, 2) if sd else 0.0,
        "trend_gap": round(px / e96 - 1, 4),
        "atr_pct": round(a / px, 5),
        "vol_ratio": round(vols[-1] / base_vol, 2),
        "ret_1h": round(px / closes[-1 - b1] - 1, 4),
        "ret_4h": round(px / closes[-1 - b4] - 1, 4),
        "breakout": px > hh,
        "green": closes[-1] > closes[-2],
    }


def evaluate(symbol, candles, cfg):
    """برمی‌گرداند (Signal|None, features|None). candles باید فقط کندل‌های بسته‌شده باشند."""
    f = features(candles, max(1, 60 // cfg.resolution))
    if not f:
        return None, None
    if f["atr_pct"] < cfg.min_atr_pct:      # نوسان کم = سود کمتر از کارمزد
        return None, f
    kind, score, why = None, 0.0, []
    if (f["rsi"] <= cfg.rsi_buy and f["bb_pos"] <= cfg.bb_max_pos and f["green"]
            and f["trend_gap"] > -cfg.max_trend_dev and f["ret_4h"] > -0.06):
        kind = "dip"
        score = 50 + (cfg.rsi_buy - f["rsi"]) * 1.5 + min(f["vol_ratio"], 3) * 4 + (10 if f["trend_gap"] > 0 else 0)
        why = [f"RSI={f['rsi']} oversold", f"near lower Bollinger (pos={f['bb_pos']})",
               "bounce candle (green)", f"not a crash (trend_gap={f['trend_gap']}, ret_4h={f['ret_4h']})"]
    elif (f["breakout"] and f["vol_ratio"] >= 1.8 and f["trend_gap"] > 0 and 55 <= f["rsi"] <= 72):
        kind = "breakout"
        score = 50 + min(f["vol_ratio"], 4) * 6 + (f["rsi"] - 55) * 0.5
        why = ["close above 20-bar high", f"volume x{f['vol_ratio']}", f"above EMA96 (gap={f['trend_gap']})",
               f"RSI={f['rsi']} not overbought"]
    if not kind:
        return None, f
    return Signal(symbol, kind, round(min(score, 100), 1), f["price"], f["atr_pct"], f, why), f


def diagnose(f, cfg):
    """توضیح شرایط هر الگو به‌تفکیک (فقط تشخیصی؛ evaluate را تغییر نمی‌دهد و تست هم‌خوانی دارد)."""
    return {
        "atr_ok": f["atr_pct"] >= cfg.min_atr_pct,
        "dip": {"rsi<=rsi_buy": f["rsi"] <= cfg.rsi_buy,
                "bb_pos<=bb_max_pos": f["bb_pos"] <= cfg.bb_max_pos,
                "green_candle": bool(f["green"]),
                "trend_gap>-max_trend_dev": f["trend_gap"] > -cfg.max_trend_dev,
                "ret_4h>-6%": f["ret_4h"] > -0.06},
        "breakout": {"close>20bar_high": bool(f["breakout"]),
                     "vol_ratio>=1.8": f["vol_ratio"] >= 1.8,
                     "trend_gap>0": f["trend_gap"] > 0,
                     "55<=rsi<=72": 55 <= f["rsi"] <= 72},
    }
