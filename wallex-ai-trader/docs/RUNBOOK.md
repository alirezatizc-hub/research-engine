# Runbook — نصب، اجرا، دروازهٔ live، عملیات و عیب‌یابی

## ۰) پیش‌نیاز

- Python 3.10+ (تست‌شده روی 3.11) و `pip`
- فقط یک dependency: `requests` (پنل با `http.server` استاندارد است)
- دسترسی شبکه به `api.wallex.ir` (از ایران معمولاً مستقیم) و به provider AI

```bash
cd wallex-ai-trader
python3 -m venv .venv && source .venv/bin/activate     # اختیاری
pip install -r requirements.txt
cp .env.example .env
```

## ۱) پنل کنترل (ساده‌ترین راه)

```bash
python -m wallexbot ui --demo         # دمو: Replay روی دادهٔ ضبط‌شده، بدون کلید، خودکار
python -m wallexbot ui                # واقعی: Shadow/Live/Replay از داخل پنل
python -m wallexbot ui --host 0.0.0.0 --port 8800 --token 'secret'
```

- پیش‌فرض روی `127.0.0.1:8800`. اگر روی شبکه باز می‌کنید، **حتماً** `--token` بگذارید.
- `--read-only` همهٔ POSTها را می‌بندد؛ `--demo` تنظیمات را قفل و اجرا را مجبور به Replay می‌کند.
- POSTها بدون هدر `X-Requested-With: wallexbot` رد می‌شوند؛ UI خودش این هدر را می‌فرستد.
- `.env` در **ریشهٔ پروژه** نوشته می‌شود؛ پنل را از همان‌جا اجرا کنید.

### تب‌ها

| تب | کار |
|---|---|
| داشبورد | equity/بازده/افت، نمودار، پوزیشن‌ها، قیف موتور، تغذیهٔ تصمیم‌ها، وضعیت Replay |
| معاملات | `logs/trades.csv` و فیلتر لاگ تصمیم‌ها |
| بک‌تست | اجرای rules-only/learn/AI، نمایش گزارش، نمودار و قیف |
| بهینه‌سازی | چرخهٔ سه‌پنجره‌ای (train/validation/test)، نوار پیشرفت، جدول سناریوها، پیشنهاد و دکمهٔ تأیید |
| تنظیمات | ویرایش `.env`، آزمون اتصال والکس، آزمون AI، وضعیت دروازهٔ live، پروفایل‌های آماده |
| ابزارها | ابزارهای MCP، اجرای unit/safety tests، دستورهای CLI |
| راهنما | تغییرات سریع، هشدارها، مسیر لاگ‌ها |

## ۲) چرخهٔ روزمره

```bash
# شب قبل: داده، بک‌تست و بهینه‌سازی
python -m wallexbot fetch --days 30
python -m wallexbot backtest --learn
python -m wallexbot optimize --ai --rounds 2      # پیشنهاد می‌دهد؛ اعمال نمی‌کند
python -m wallexbot optimize --list
python -m wallexbot optimize --approve <run-id> --yes   # فقط بعد از مرور summary.md
python -m wallexbot tune --grid quick        # فقط اگر داده ≥ ~۳۰۰ کندل باشد

# صبح: shadow روی بازار زنده (بدون ریسک)
python -m wallexbot run --mode shadow
python -m wallexbot report
```

لاگ‌ها:

```text
logs/decisions.jsonl     هر SCAN / DECISION / BUY / SELL / LEARN / FLATTEN / ERROR
logs/trades.csv          معاملات بسته‌شده (qty, cost, proceeds, pnl, exit_reason)
logs/ai_calls.jsonl      فراخوانی‌های AI (بدون کلید)
logs/backtest_real/      گزارش و curve قابل‌بازتولید
state.json               پوزیشن‌ها، آمار، learner، curve، heartbeat
```

## ۳) دروازهٔ live (همهٔ موارد الزامی)

1. در `.env` صریحاً تنظیم کنید:

```bash
WTB_SYMBOLS=BTCTMN,ETHTMN,SOLTMN,...
WTB_MAX_DRAWDOWN=0.05
WTB_DAILY_LOSS_LIMIT=0.025
WTB_CHALLENGE_END=2026-10-23T23:50:00+03:30
WALLEX_API_KEY=...
WTB_AI_API_KEY=...
```

2. پارامترها را ببینید و ACK را ثبت کنید:

```bash
python -m wallexbot params
# خروجی: WTB_LIVE_ACK=<fingerprint>   (در .env بنویسید)
```

3. اعتبارسنجی API روی حساب واقعی (یک سفارش LIMIT غیرقابل‌پر‌شدن + لغوِ تأییدشده):

```bash
python -m wallexbot selfcheck --yes-place-test-order
# خروجی: logs/api_validation.json
```

4. تست‌های ایمنی (به هش کد وابسته است؛ هر تغییر کد → اجرای دوباره):

```bash
python -m wallexbot safetytests
# خروجی: logs/safety_tests.json
```

5. وضعیت دروازه را ببینید:

```bash
python -m wallexbot doctor
# یا در پنل: تب تنظیمات → «دروازهٔ live»
```

6. اجرا:

```bash
python -m wallexbot run --mode live
```

اگر یک مورد نباشد، `LiveGate` با فهرست دلایل جلوی اجرا را می‌گیرد و `OrderManager` پیش از **هر**
سفارش دوباره بررسی می‌کند.

> **هشدار:** `selfcheck` یک سفارش واقعی ثبت و لغو می‌کند. فقط روی حسابی اجرا کنید که اجازهٔ این کار را دارد.
> مبلغ/قیمت سفارش تست عمداً خارج از بازار است تا پر نشود.

## ۴) کنترل در حین اجرا

| کار | CLI | پنل |
|---|---|---|
| توقف تمیز | `Ctrl+C` (state ذخیره، قفل آزاد) | Stop |
| تسویهٔ فوری | `python -m wallexbot flatten` | «تسویهٔ کامل» |
| برداشتن Kill-Switch | `python -m wallexbot reset-kill` | «ریست Kill-Switch» |
| خلاصهٔ معاملات | `python -m wallexbot report` | تب معاملات |
| بررسی حساب | `python -m wallexbot account` | «آزمون اتصال» |
| Replay سریع | `python -m wallexbot replay --speed 5000` | تب داشبورد → Replay |

اگر `halt` شد، دلیل در `decisions.jsonl` (`action=HALTED`) و در پنل (`halted`) دیده می‌شود.
ترتیب رفع: ۱) علت را در صرافی بررسی کنید (سفارش باز/موجودی)، ۲) `reconcile` با ری‌استارت ربات اجرا می‌شود،
۳) اگر مشکل حل شد، ربات را دوباره شروع کنید. **هرگز بدون بررسی، halt را دستی پاک نکنید.**

## ۵) Replay و Shadow

- **Replay** = همان `Bot` + `ShadowExecutor` روی کندل ضبط‌شده با ساعت مجازی. هیچ تماس خصوصی/سفارشی ندارد.
- دادهٔ واقعی فقط اگر `manifest.source == api.wallex.ir/v1/udf/history` و از ۱ دقیقه‌ای تجمیع شده باشد پذیرفته می‌شود؛
  وگرنه برچسب `SYNTHETIC_NOT_EVIDENCE`.
- گرم‌شدن: `max(0, min(115, n_bars−10))` کندل؛ پایان چالش = آخرین کندل داده؛ ورود از ۲ ساعت آخر و
  تسویهٔ ۳۰ دقیقهٔ آخر (در replay) تا داده هدر نرود.
- در پنل: `POST /api/replay/start` با `{speed, loop}`؛ `GET /api/replay/status` پیشرفت را می‌دهد.
- دموی عمومی: `--demo` پروفایل «فعال‌تر» را فقط در حافظه اعمال می‌کند (`demo_profile=true`) و در `.env` ذخیره نمی‌کند.

## ۶) عیب‌یابی

| نشانه | علت محتمل | راه‌حل |
|---|---|---|
| `market list unavailable` | شبکه/DNS/فایروال یا خطای ۵xx | اتصال به `api.wallex.ir` را بررسی کنید؛ ورود تا تأیید بازار مسدود است |
| `401` در API خصوصی | کلید اشتباه/منقضی/هدر اشتباه | کلید را در پنل تنظیم کنید؛ `WTB_API_KEY_HEADER=X-API-Key` |
| `402` هنگام خرید | موجودی کافی نیست | اندازه/رزرو را بررسی کنید؛ کارمزد هم باید جا شود |
| `409` | `client_id` تکراری | خودکار به‌عنوان نامعلوم بررسی می‌شود؛ سفارش تکراری ثبت نمی‌شود |
| `429` | محدودیت نرخ | GETها retry می‌شوند؛ POST نامعلوم تلقی و با client_id بررسی می‌شود |
| `s=no_data` در UDF | بازهٔ بدون داده/مشکل موقت | `fetch` را با بازهٔ دیگر تکرار کنید؛ علت قطعی فرض نمی‌شود |
| سیگنال صفر | `atr_below_min_atr_pct` غالب است | قیف بک‌تست را ببینید؛ `min_atr_pct` را آگاهانه کم کنید |
| AI همیشه رد می‌کند | کلید/مدل/سازگار نبودن پاسخ | «آزمون AI» در پنل؛ `ai_calls.jsonl` را ببینید |
| پنل «قطع ارتباط» | سرویس/پورت/توکن | `curl /api/health`؛ `--token` را در URL بگذارید |
| دروازه با «کد تغییر کرده» بسته است | `safetytests` قدیمی | `python -m wallexbot safetytests` را دوباره اجرا کنید |

## ۷) پشتیبان‌گیری و پاک‌سازی

```bash
cp state.json logs/trades.csv logs/decisions.jsonl  /backup/$(date +%F)/
rm -rf logs/replay logs/replay_cli    # فقط لاگ‌های قابل‌بازسازی
```

`state.json` را در گیت نگذارید؛ در `.gitignore` هست (کلیدها جدا هستند اما مسیرهای محلی و تاریخچه هم داخلش است).

## ۸) چک‌لیست پیش از تحویل/ویدیو

- [ ] `python -m unittest discover -s tests -t .` سبز
- [ ] `python -m wallexbot backtest` اجرا و `logs/backtest_real/backtest_report.json` تازه
- [ ] `python -m wallexbot ui --demo` باز و Replay در حال حرکت
- [ ] `python -m wallexbot optimize --list` اجراها را نشان می‌دهد و آخرین پیشنهاد `pending_approval` است
- [ ] `logs/trades.csv` حداقل یک معامله با `qty` غیرصفر
- [ ] `docs/DEMO_SCRIPT.md` را دنبال کنید
- [ ] `.env` در گیت نیست (`git status`)
