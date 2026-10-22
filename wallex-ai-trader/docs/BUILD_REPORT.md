# گزارش ساخت — اصلاح حداقلی لاگ ورود

## تغییرات (دقیق)
1. `wallexbot/bot.py`
   - `import math` اضافه شد.
   - تابع خالص و بدون اثر جانبی `entry_risk_fields(value, sl_pct, tp_pct)` اضافه شد.
   - در `Bot.apply_buy` فراخوانی `dlog.write(action="BUY", ...)` سه فیلد جدید (و `rr_status`) را ثبت می‌کند.
2. `tests/test_entry_log.py` — فایل جدید با ۴ تست. هیچ تست قبلی حذف یا تغییر نکرد.

## فیلدهای جدید در ردیف BUY (`logs/decisions.jsonl`)
| فیلد | معنا / واحد |
|---|---|
| `sl_pct` | فاصله‌ی استاپ، کسر قیمت ورود (۰٫۰۱۲ = ۱٫۲٪) |
| `est_loss_tmn` | `notional × sl_pct` به تومان؛ زیان قیمتیِ رسیدن به استاپ، **بدون کارمزد و لغزش** |
| `rr` | `tp_pct / sl_pct` |
| `rr_status` | `ok` \| `invalid_sl_pct` \| `invalid_tp_pct` |

اگر `sl_pct` صفر، منفی، NaN/Inf، None یا غیرعددی باشد: `rr=null`، `est_loss_tmn=null`، `rr_status="invalid_sl_pct"` (بدون تقسیم بر صفر و بدون مقدار ساختگی).
نکته: در ورود جزئیِ ادغام‌شده با پوزیشن موجود، فیلدها مربوط به همین پرشدن‌اند و همان رفتار قبلیِ فیلد `stop` را دارند.

## دست‌نخورده
`strategy.py`، `risk.py`، `backtest.py`، `config.py` (آستانه‌ها و حد ضرر)، `orders.py`، `safety.py`، `executors.py`. اثرانگشت پارامترها (`WTB_LIVE_ACK`) تغییر نکرد: `3d63cf573c66`.

## هش‌ها و نتایج
- code_hash قبل: `b3f3b24366ae7910`
- code_hash بعد: `7f72b976e0c6415d` (مطابق `logs/safety_tests.json`)
- تست‌ها: ۴۱ اجرا، OK، ۱ skip (fixture واقعی والکس هنوز ضبط نشده)
- safetytests: ۴۰ passed، ۰ failure، ۰ error، همه‌ی ۲۸ تست الزامی گذشتند
- هر تغییر بعدی در فایل‌های `.py` هش را باطل می‌کند و `safetytests` باید دوباره اجرا شود.

## اجرا نشد (عمداً)
`selfcheck --yes-place-test-order`، Live، هر سفارش واقعی، fetch/backtest (نبود اینترنت).
قفل Live همچنان بسته است (حد ضرر/نمادها/پایان چالش صریح نیست، ACK خالی، کلیدها نیست، `api_validation.json` نیست).
