# Wallex AI Trader — چالش معاملات الگوریتمی والکس (استارکوچ)

ربات اسپات (Long-only) روی بازارهای تومانی؛ موتور سیگنال کمّی + عامل AI (دروازه‌ی ورود، ناظر ریسک، تحلیلگر پس از معامله) + یادگیری کران‌دار از اشتباهات.
**وضعیت: حالت live تا گذراندن همه‌ی دروازه‌های زیر مسدود است.** (جزئیات تصمیم‌ها: `docs/DECISIONS.md`)

## معماری
```
exchange.py (REST) ─► schemas.py (اعتبارسنجی سخت پاسخ‌ها، fail-closed)
strategy.py ─► learner.py (آستانه/پارامتر/غیرفعال‌سازی) ─► ai_agent.py (gate/review/reflect، JSON معتبر)
                              │
                 risk.py (سقف حجم، استاپ، تریلینگ، kill-switch)
                              │
   executors.py: ShadowExecutor | LiveExecutor ─► orders.py (ژورنال write-ahead، تأیید لغو، partial، reconcile)
                              │            ▲ safety.py (LiveGate)
                    bot.py (حلقه اصلی، halt، بازیابی) ─► logs/decisions.jsonl, trades.csv
backtest.py (همان strategy/risk/learner؛ kill-switch با تسویه محافظه‌کارانه)
```

## دروازه‌ی live (همه الزامی)
1. `WTB_SYMBOLS`، `WTB_MAX_DRAWDOWN`، `WTB_CHALLENGE_END` صریحاً تنظیم شوند.
2. `python -m wallexbot params` ← پارامترها را با بریف مطابقت دهید ← `WTB_LIVE_ACK=<اثرانگشت>`.
3. `python -m wallexbot selfcheck --yes-place-test-order` روی حساب اعتباری (یک سفارش LIMIT غیرقابل‌پر‌شدن و لغوِ تأییدشده) ← `logs/api_validation.json`.
4. `python -m wallexbot safetytests` ← `logs/safety_tests.json` (به هش کد وابسته؛ هر تغییر کد = اجرای دوباره).
5. کلید Anthropic (یا `WTB_AI_REQUIRED_LIVE=false`).
اگر نقصی باشد، `run --mode live` دلیل را چاپ می‌کند و خارج می‌شود؛ `OrderManager` هم پیش از هر سفارش دوباره بررسی می‌کند.

## اجرا
```bash
pip install -r requirements.txt
python -m wallexbot fetch --days 30                 # داده ۱ دقیقه‌ای واقعی → تجمیع ۱۵ دقیقه‌ای + manifest (docs/DATA_FETCH.md)
python -m wallexbot backtest [--learn]              # خروجی: logs/backtest_real[_learn]/
python -m wallexbot backtest --synthetic            # فقط تست دود → logs/synthetic/ (بی‌اعتبار)
python -m wallexbot run --mode shadow               # Shadow Testing روی بازار زنده (بدون کلید هم کار می‌کند)
python -m wallexbot run --mode live                 # فقط پس از دروازه‌ها
python -m wallexbot flatten | report | reset-kill
python -m unittest discover -s tests -t .           # تست‌ها
```

## ریسک
حداکثر ۳ پوزیشن، هر کدام ≤۳۰٪، ≥۱۰٪ نقد؛ ریسک هر معامله ≈۰٫۶٪؛ استاپ ATR + تریلینگ + خروج زمانی؛ توقف ورود با افت روزانه ۸٪؛ Kill-Switch با افت از اوج (مقدار: `docs/DECISIONS.md`)؛
ورود ممنوع ۴ ساعت آخر؛ تسویه کامل به تومان ۴۵ دقیقه قبل از پایان.

## ایمنی سفارش
ژورنال پیش از ارسال؛ ثبت سفارش هرگز خودکار retry نمی‌شود؛ خطای مبهم → جست‌وجو با شناسه/openOrders، وگرنه halt؛ لغو فقط با GET (`active=false`) تأیید می‌شود؛
سفارش ناقص → حجم واقعی ثبت و باقی‌مانده لغوِ تأییدشده؛ ری‌استارت → reconcile (سفارش/موجودی ناشناخته → لغو یا halt).

## یادگیری از اشتباهات
هر معامله با ویژگی‌های ورود، MAE/MFE و برچسب اشتباه (`gave_back`, `fast_stop`, `never_worked`, `fee_drag`, `stale`) ثبت می‌شود.
هر ۴ معامله (حداقل ۸) حداکثر **یک** پارامتر یک گام کوچک و در کران سخت تغییر می‌کند؛ اثرش سنجیده و در صورت بدتر شدن برگردانده و فریز می‌شود.
نماد/نوعِ سه‌ضرر پیاپی ۱۲ ساعت غیرفعال؛ آستانه امتیاز نوعِ ضررده +۵. AI «درس» متنی می‌نویسد که به پرامپت دروازه داده می‌شود (پارامتر عددی را AI تغییر نمی‌دهد).
همه‌ی تغییرات با شواهد در `decisions.jsonl` (action=LEARN) ثبت می‌شوند.

## تشخیص صفر معامله
`backtest` همیشه «قیف سیگنال» (تعداد سیگنال dip/breakout و دلیل هر رد) چاپ و در `backtest_report.json` ذخیره می‌کند. تحلیل کامل: `docs/ZERO_TRADES_REPORT.md`.

## محدودیت‌ها
- مدل درون‌کندلی بک‌تست محافظه‌کارانه است: در کندلی که هم استاپ و هم تارگت داخل بازه‌اش باشد، استاپ مقدم است؛ و صعود یک‌کندلی به تارگت با تریلینگ فعال‌شده ممکن است `trail_stop` شود نه `take_profit`.
- هیچ پاسخ واقعی والکس هنوز ضبط نشده؛ اعتبارسنجی فعلی بر نمونه‌های مستندات رسمی (`tests/fixtures/docs`) است.
- Backtest بدون AI است و روی داده مصنوعی هیچ ادعایی ندارد. سودآوری تضمین نمی‌شود.
- یادگیری با تعداد معامله‌ی کم نویزی است؛ کران‌ها و rollback برای مهار همین ریسک‌اند.
