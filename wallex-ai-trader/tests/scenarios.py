"""داده مصنوعی کنترل‌شده برای تست استراتژی. هر سناریو طوری ساخته شده که نتیجه مورد انتظارش از «قوانین مستند»
(README / strategy.py) معلوم است، نه از خروجی کد. هیچ کندل آینده‌ای در ساخت سیگنال نقش ندارد.
همه سناریوها با Config() پیش‌فرض کار می‌کنند (هیچ آستانه‌ای برای فعال‌شدن سیگنال تغییر نمی‌کند)."""
import math

T0, RES = 1_700_000_000, 900


def bar(i, o, c, wick=0.005, v=100.0):
    return {"t": T0 + i * RES, "o": o, "h": max(o, c) * (1 + wick), "l": min(o, c) * (1 - wick), "c": c, "v": v}


def base(n=200, start=100.0, drift=0.0004, amp=0.0015, wick=0.005):
    """بازار سالم با روند ملایم و نوسان شمع‌ها (ATR ≈ ۱٪)، بدون الگوی ورود."""
    out, px = [], start
    for i in range(n):
        o, px = px, px * (1 + drift + amp * math.sin(i * 0.9))
        out.append(bar(i, o, px, wick))
    return out


def extend(bars, moves, vols=None, wick=0.005):
    """moves: بازده هر کندل جدید؛ open هر کندل = close قبلی."""
    for k, m in enumerate(moves):
        o = bars[-1]["c"]
        bars.append(bar(len(bars), o, o * (1 + m), wick, vols[k] if vols else 100.0))
    return bars


def dip_scenario(drops=5, drop=-0.008, bounce=0.002, bounce_vol=300.0, n_base=200):
    """روند سالم → ۵ کندل افت ۰٫۸٪ → کندل برگشتی سبز با حجم ۳×. آخرین کندل (اندیس ۲۰۵) = کندل سیگنال."""
    return extend(extend(base(n_base), [drop] * drops), [bounce], [bounce_vol])


def breakout_scenario(jump=0.02, vol=400.0, n_base=200):
    """بازار نوسانی بدون روند (RSI میانی) → یک کندل شکست با حجم ۴×."""
    return extend(base(n_base, drift=0.0, amp=0.005, wick=0.003), [jump], [vol], wick=0.003)


def quiet_market(n=300):
    """بازار آرام: ATR ≈ ۰٫۲٪ (زیر کف ۰٫۶٪) => هیچ سیگنالی نباید ساخته شود."""
    return base(n, drift=0.0, amp=0.0005, wick=0.0008)


def path_after(bars, kind, px0, stop, tp, tp_pct):
    """آینده‌ی دلخواه پس از کندل ورود: 'tp' => چند کندل آرام و سپس صعود تدریجی تا تارگت (اسپایک تک‌کندلی در مدل محافظه‌کارانه trail_stop می‌شود)؛ 'sl' => سقوط تا زیر استاپ؛
    'flat' => فقط آرام. (فقط برای ساخت داده‌ی آینده؛ سیگنال هرگز از آن‌ها ساخته نمی‌شود.)"""
    out = list(bars)
    for k in range(3):                                   # کندل‌های آرام: نه به استاپ می‌رسند نه به تارگت
        o = out[-1]["c"]
        out.append(bar(len(out), o, o * (1 + 0.0004 * (-1) ** k), wick=0.0005))
    o = out[-1]["c"]
    if kind == "tp":                                     # صعود تدریجی +۰٫۴٪ در هر کندل تا high به تارگت برسد
        while True:
            o = out[-1]["c"]
            b = bar(len(out), o, o * 1.004, wick=0.0005)
            out.append(b)
            if b["h"] >= tp:
                break
    elif kind == "sl":
        c = stop * 0.995
        out.append({"t": T0 + len(out) * RES, "o": o, "h": o, "l": c, "c": c, "v": 100.0})
    return out
