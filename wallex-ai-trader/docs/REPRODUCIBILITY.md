# بازتولید نتایج — داده، دستورها، هش‌ها، محدودیت‌ها

هدف: داور (یا خودتان در آینده) بتواند **همان اعداد** را با همان کد و همان داده بازتولید کند.

## ۱) دادهٔ موجود

| مورد | مقدار |
|---|---|
| منبع | `api.wallex.ir/v1/udf/history` (resolution=1، تجمیع سمت کلاینت به ۱۵m) |
| نمادها | BTCTMN, ETHTMN, SOLTMN, XRPTMN, DOGETMN, TRXTMN, ADATMN, BNBTMN |
| تعداد | ۱۹۲ کندل ۱۵دقیقه‌ای برای هر نماد = ۲۸۸۰ کندل ۱ دقیقه‌ای |
| بازه | ~۲ روز، آخرین کندل `2026-10-22T13:45Z` |
| اعتبارسنجی | مرتب، بدون تکرار، هم‌تراز، بدون شکاف، فقط کندل کامل (`manifest.json`) |
| محدودیت‌ها | `source_resolution_min=1`, `aggregated_client_side=true` |

بازتولید داده:

```bash
python -m wallexbot fetch --days 30
```

> سندباکس توسعه به `api.wallex.ir` دسترسی نداشت؛ CSVها از اجرای قبلی `fetch` روی حساب/شبکهٔ کاربر
> در مخزن هستند. اگر manifest موجود نباشد، بک‌تست **fail** می‌کند و دادهٔ مصنوعی به‌عنوان واقعی پذیرفته نمی‌شود.

## ۲) دستورهای بازتولید

```bash
pip install -r requirements.txt

# تست‌ها (۱۰۱ تست، ۱ skip)
python -m unittest discover -s tests -t .

# بک‌تست rules-only (عدد جدول README)
python -m wallexbot backtest
# خروجی: logs/backtest_real/{backtest_report.json, backtest_trades.csv, backtest_equity.csv}

# بک‌تست با learner
python -m wallexbot backtest --learn          # → logs/backtest_real_learn/

# Replay کامل با تسویهٔ پایان چالش (بدون UI)
python -m wallexbot replay --speed 5000       # → logs/replay_cli/

# تیون walk-forward (با دادهٔ ۲روزه عمداً خطا می‌دهد: کمتر از ~۳۰۰ کندل)
python -m wallexbot tune --grid quick

# تست‌های ایمنی + هش کد
python -m wallexbot safetytests               # → logs/safety_tests.json
```

## ۳) اعداد مرجع (نسخهٔ فعلی)

### بک‌تست rules-only (`logs/backtest_real/backtest_report.json`)

```text
return_pct          -0.97
final_tmn           4,951,592
trades              4
win_rate_pct        50.0
profit_factor       0.26
avg_win_tmn         8,474
avg_loss_tmn        32,678
max_drawdown_pct    1.71
data_source         real:api.wallex.ir/v1/udf/history
thresholds.used     55.0 (rules-only)
فیلتر نوسان        min_atr_pct=0.0025
```

قیف: `signal:dip=5`, `passed_score:dip=5`, `entries_executed=4`,
`rejected:atr_below_min_atr_pct=13`.

### Replay (اجرای موتور زنده، خروجی `logs/replay_test/`)

```text
equity              4,937,524
return_pct          -1.25
trades              4  (1 برد / 3 باخت)
realized_pnl        -62,476 TMN
fees_paid           24,236 TMN
max_drawdown (curve) 1.46%
flat_check          {"flat": true, "source": "shadow_ledger"}
```

معاملات: ADATMN (`stop_loss`, −۳٫۷۸۹٪)، TRXTMN (`end_of_challenge`, +۰٫۷۸۴٪)،
XRPTMN (−۲٫۷۲۶٪)، ETHTMN (−۱٫۴۱۱٪). همه در تومان تسویه شدند.

> اختلاف کوچک بک‌تست و Replay طبیعی است: بک‌تست با مدل درون‌کندلی محافظه‌کارانه و بدون پنجرهٔ
> پایان چالش اجرا می‌شود؛ Replay همان حلقهٔ زنده با ساعت مجازی و تسویهٔ ۳۰ دقیقه قبل از آخرین کندل است.

## ۴) چه چیزی reproducibility را تضمین می‌کند؟

1. **همان کد برای همهٔ مسیرها:** `backtest.py` و `replay.py` از همان `strategy.evaluate`، `risk`،
   `learner` و `bot` استفاده می‌کنند؛ نسخهٔ جداگانه‌ای وجود ندارد.
2. **بدون نگاه به آینده:** فقط کندل بسته؛ ورود در open کندل بعد؛ تست `test_backtest_has_no_lookahead`
   با دو آیندهٔ متفاوت و یک پیشوند یکسان.
3. **داده قفل‌شده:** CSVها در گیت؛ manifest با منبع/رزولوشن/اعتبارسنجی؛ هر تغییر داده باید manifest بسازد.
4. **هش کد:** `safetytests` هش `wallexbot/*.py + tests/*.py` را ذخیره می‌کند؛ دروازهٔ live فقط با هش
   هم‌خوان باز می‌شود. هر ویرایش کد = اجرای دوبارهٔ تست‌ها.
5. **اثرانگشت پارامتر:** `params` هش SHA-256 کوتاه از پارامترهای ریسک/استراتژی می‌سازد؛ `WTB_LIVE_ACK`
   باید با آن یکی باشد.
6. **لاگ تصمیم:** هر SCAN/DECISION/BUY/SELL/LEARN/FLATTEN با زمان و اعداد در `decisions.jsonl` است.
7. **بذر تصادفی:** دادهٔ مصنوعی `synthetic(seed=...)` و برچسب `SYNTHETIC_NOT_EVIDENCE` دارد؛ هیچ عدد
   مصنوعی در گزارش اصلی استفاده نمی‌شود.

## ۵) محدودیت‌های آماری (مهم برای داوری)

- ۲ روز داده = **۴۸ کندل معاملاتی** برای هر نماد؛ ۴ معامله در بک‌تست و ۴ در Replay. با این نمونه
  **نمی‌توان** دربارهٔ سودآوری استراتژی حرف زد.
- Sweep ۳۰۶ حالتی (`min_atr × rsi × bb × threshold`) هیچ ترکیب سود پایداری نشان نداد؛ بهترین‌ها
  روی مرز نویز بودند. به همین دلیل انتخاب پیش‌فرض بر «کنترل هزینه و افت» است، نه «حداکثر بازده».
- کارمزد ۰٫۳۵٪ در هر سمت (رفت‌وبرگشت ۰٫۷٪) با ATR ۱۵دقیقه‌ای ~۰٫۳٪ یعنی هر معامله باید حداقل
  ~۱٪ حرکت مفید داشته باشد. این واقعیت فیزیکی بازار است، نه باگ کد.
- `tune` با split 70/30 روی ۲ روز خطا می‌دهد (کمتر از ۳۰۰ کندل). با `fetch --days 30` اجرا کنید؛
  آن‌گاه انتخاب پارامتر روی train و گزارش روی test معنادار می‌شود.

## ۶) چطور خودتان عدد را بهتر کنید (بدون تقلب)

1. `fetch --days 60` (حداقل ۳۰ روز) بگیرید.
2. `tune --grid full --split 0.7` اجرا کنید؛ فقط اگر «بهترین ترکیب» در بازهٔ آزمون هم مثبت بود،
   `profiles --apply` کنید.
3. `backtest --learn` و `replay --speed 2000` را ببینید.
4. اگر لازم بود AI را وارد حلقه کنید: `backtest --ai-gate` (هزینهٔ توکن؛ سقف ۸۰ فراخوانی).
5. هر بار تغییر پارامتر/کد → `safetytests` و `WTB_LIVE_ACK` جدید.

**هرگز** پارامتر را روی بازهٔ آزمون انتخاب نکنید و بعد همان بازه را «شاهد» معرفی نکنید.
