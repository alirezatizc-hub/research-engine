"""تجمیع کندل‌های ۱ دقیقه‌ای به کندل‌های N دقیقه‌ای (سمت کلاینت) + اعتبارسنجی سخت‌گیرانه.

اصول:
- مرز کندل بر اساس Unix timestamp: bucket = t - t % (N*60)  (UTC epoch؛ مستقل از منطقه زمانی).
- open = open اولین دقیقه، close = close آخرین دقیقه، high = max، low = min، volume = مجموع.
- یک کندل N دقیقه‌ای فقط وقتی ساخته می‌شود که *همه‌ی* N دقیقه‌اش موجود باشد.
- هیچ کندل یا دقیقه‌ای ساخته/پر/درون‌یابی نمی‌شود. داده ناقص => DataIntegrityError با جزئیات دقیق.
- کندل انتهایی ناقص/در حال تشکیل (بازه‌ای که هنوز تمام نشده) هرگز در خروجی نمی‌آید.
"""
from __future__ import annotations

MAX_LISTED = 8          # حداکثر تعداد موردهای ذکرشده در پیام خطا


class DataIntegrityError(ValueError):
    """داده دریافتی برای ساخت کندل واقعی و کامل کافی/معتبر نیست."""


def _fmt(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def validate_minutes(minutes):
    """ورودی ۱ دقیقه‌ای: اعداد صحیح، هم‌تراز با ۶۰ ثانیه، به‌ترتیب صعودی، بدون تکرار، OHLC سازگار."""
    prev = None
    for c in minutes:
        t = c["t"]
        if not isinstance(t, int) or isinstance(t, bool) or t <= 0:
            raise DataIntegrityError(f"timestamp نامعتبر: {t!r}")
        if t > 10 ** 11:
            raise DataIntegrityError(f"timestamp={t} احتمالاً میلی‌ثانیه است، نه ثانیه (Unix seconds مورد انتظار است)")
        if t % 60:
            raise DataIntegrityError(f"timestamp={t} ({_fmt(t)}) روی مرز دقیقه نیست")
        if prev is not None:
            if t == prev:
                raise DataIntegrityError(f"timestamp تکراری: {t} ({_fmt(t)})")
            if t < prev:
                raise DataIntegrityError(f"ترتیب زمانی نادرست: {t} ({_fmt(t)}) بعد از {prev} ({_fmt(prev)})")
        prev = t
        if not (c["h"] >= max(c["o"], c["c"]) and c["l"] <= min(c["o"], c["c"]) and c["l"] > 0 and c["v"] >= 0):
            raise DataIntegrityError(f"OHLCV ناسازگار در t={t} ({_fmt(t)})")


def aggregate(minutes, res_min, start, end):
    """minutes: لیست dict با کلیدهای t,o,h,l,c,v (۱ دقیقه‌ای، صعودی، بدون تکرار).
    start/end: مرزهای بازه‌ی [start, end) که باید هم‌تراز با res_min*60 باشند؛ همه‌ی کندل‌های این بازه الزامی‌اند.
    خروجی: لیست کندل‌های N دقیقه‌ای کامل. هر کمبود => DataIntegrityError."""
    step = res_min * 60
    if res_min < 1 or res_min * 60 % 60:
        raise ValueError("res_min نامعتبر")
    if start % step or end % step or end <= start:
        raise DataIntegrityError(f"بازه هم‌تراز با {step}s نیست: start={start} end={end}")
    validate_minutes(minutes)
    by_t = {c["t"]: c for c in minutes}
    outside = [t for t in by_t if not (start <= t < end)]
    if outside:
        raise DataIntegrityError(f"{len(outside)} کندل خارج از بازه‌ی [{_fmt(start)}, {_fmt(end)}): مثلاً {_fmt(min(outside))}")
    per = res_min
    out, problems = [], []
    for b in range(start, end, step):
        mins = [by_t.get(b + 60 * k) for k in range(per)]
        miss = [k for k, m in enumerate(mins) if m is None]
        if miss:
            problems.append((b, len(miss)))
            continue
        out.append({"t": b, "o": mins[0]["o"], "h": max(m["h"] for m in mins), "l": min(m["l"] for m in mins),
                    "c": mins[-1]["c"], "v": sum(m["v"] for m in mins)})
    if problems:
        missing_total = sum(n for _, n in problems)
        sample = "; ".join(f"{_fmt(b)} (کمبود {n} از {per} دقیقه)" for b, n in problems[:MAX_LISTED])
        raise DataIntegrityError(
            f"داده ناقص: {len(problems)} از {(end - start) // step} کندل {res_min} دقیقه‌ای کامل نیست "
            f"({missing_total} دقیقه کم). نمونه: {sample}. هیچ داده‌ای جعل/پر نشد.")
    validate_series(out, res_min)
    return out


def validate_series(bars, res_min):
    """سری نهایی: صعودی اکید، بدون تکرار، هم‌تراز، و *بدون هیچ شکاف* (فاصله دقیقاً res_min*60)."""
    step = res_min * 60
    if not bars:
        raise DataIntegrityError("سری کندل خالی است")
    for i, c in enumerate(bars):
        if c["t"] % step:
            raise DataIntegrityError(f"کندل t={c['t']} روی مرز {res_min} دقیقه نیست")
        if not (c["h"] >= max(c["o"], c["c"]) and c["l"] <= min(c["o"], c["c"]) and c["v"] >= 0):
            raise DataIntegrityError(f"OHLCV ناسازگار در t={c['t']}")
        if i:
            d = c["t"] - bars[i - 1]["t"]
            if d == 0:
                raise DataIntegrityError(f"timestamp تکراری: {c['t']}")
            if d < 0:
                raise DataIntegrityError("سری به‌ترتیب زمانی نیست")
            if d != step:
                raise DataIntegrityError(f"شکاف/فاصله‌ی نامنظم در {_fmt(c['t'])}: {d}s به‌جای {step}s")


def last_closed_boundary(now, res_min):
    """آخرین مرز N دقیقه‌ای که تا now کامل بسته شده است (کندل در حال تشکیل بعد از آن است)."""
    step = res_min * 60
    return int(now) // step * step
