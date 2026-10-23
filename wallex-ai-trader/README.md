# Wallex AI Trader — موتور معاملاتی الگوریتمی + عامل AI

> چالش معاملات الگوریتمی والکس / استارکوچ — اسپات، فقط خرید (Long-only)، بازارهای تومانی
>
> **وضعیت فعلی:** موتور، بک‌تست، Replay، کلاینت MCP و پنل کنترل وب کامل و تست‌شده‌اند.
> **ارسال سفارش واقعی (live) عمداً قفل است** تا همهٔ دروازه‌های ایمنی (`docs/RUNBOOK.md`) روی حساب واقعی پاس شوند.

| موضوع | نتیجه |
|---|---|
| تست‌ها | `101` تست، `1` skip (نیازمند پاسخ واقعی API)، ~۷ ثانیه |
| بک‌تست دادهٔ واقعی | ۸ نماد × ۱۹۲ کندل ۱۵دقیقه‌ای (۲ روز) — بازده **−۰٫۹۷٪**، افت حداکثر **۱٫۷۱٪**، **۴** معامله، نرخ برد ۵۰٪ |
| Replay (همان داده، حلقهٔ کامل همراه با تسویهٔ پایان چالش) | **۴** معامله، **−۱٫۲۵٪**، افت **۱٫۴۶٪**، ۱ برد/۳ باخت، تسویهٔ کامل به تومان |
| پنل | `python -m wallexbot ui --demo` — بدون کلید، بدون اینترنت، روی دادهٔ ضبط‌شده |

> عددهای بالا روی **۲ روز** داده هستند و ادعای سودآوری نیستند. برای اعتبار آماری، `fetch --days 30` و
> `tune --grid full` را اجرا کنید؛ روش و هشدارهای overfit در `docs/REPRODUCIBILITY.md` آمده است.

---

## ۱) شروع سریع (۳۰ ثانیه، بدون کلید و بدون اینترنت)

```bash
pip install -r requirements.txt          # فقط requests
python -m wallexbot ui --demo            # http://127.0.0.1:8800
```

- پنل به‌صورت خودکار یک **Replay** روی دادهٔ واقعی ضبط‌شده شروع می‌کند: همان حلقهٔ زندهٔ ربات با ساعت مجازی،
  اما با `ShadowExecutor` — هیچ سفارشی ارسال نمی‌شود.
- تب‌ها: داشبورد (equity/پوزیشن/تغذیهٔ تصمیم‌ها)، معاملات، بک‌تست، تنظیمات، ابزارها (MCP/تست/دستورها)، راهنما.
- برای پنل روی شبکه/کانتینر: `--host 0.0.0.0 --port 8800`. برای امنیت: `--token <secret>`.
- POSTها بدون هدر `X-Requested-With: wallexbot` رد می‌شوند (ضد CSRF)؛ کلیدها فقط ماسک‌شده به مرورگر می‌روند.

## ۲) شروع واقعی (shadow روی بازار زنده)

```bash
python -m wallexbot fetch --days 30         # دادهٔ ۱ دقیقه‌ای واقعی → تجمیع ۱۵ دقیقه‌ای + manifest
python -m wallexbot backtest                # خروجی: logs/backtest_real/
python -m wallexbot run --mode shadow       # بدون کلید هم کار می‌کند (فقط دادهٔ عمومی)
python -m wallexbot ui                      # کنترل و مشاهده
```

## ۳) نقشهٔ معماری

```text
                 ┌────────────────────── data / discover ──────────────────────┐
exchange.py ──► schemas.py         aggregate.py (1m→15m، اعتبارسنجی شکاف/تکرار)
   │  (REST، fail-closed)                 │
   ▼                                      ▼
strategy.py (dip/breakout، ATR، RSI، BB، حجم، روند) ──► learner.py (کران‌دار، rollback)
   │                                                         │
   ▼                                                         ▼
ai_agent.py (gate / review / reflect؛ JSON سخت‌گیرانه، cache، لاگ هر فراخوانی)
   │
   ▼
risk.py (اندازه، استاپ/تارگت/تریلینگ، سقف پوزیشن/اکسپوژر، روزانه، kill-switch)
   │
   ▼
executors.py — ShadowExecutor │ LiveExecutor ──► orders.py (journal write-ahead، تأیید لغو، partial، reconcile)
   │                                      ▲ safety.py (LiveGate: ACK، selfcheck، safetytests)
   ▼                                      │
bot.py (حلقهٔ ۱۵ثانیه‌ای، heartbeat، بازیابی، تسویهٔ پایان چالش) ──► logs/decisions.jsonl + logs/trades.csv
   │
   ├── backtest.py (همان strategy/risk/learner؛ بدون نگاه به آینده)
   ├── replay.py   (همان Bot روی کندل ضبط‌شده با ساعت مجازی)
   └── web.py      (پنل کنترل؛ ThreadingHTTPServer + JSON API + UI استاتیک)
```

## ۴) قوانین تصمیم (خلاصه؛ کامل در `docs/ALGORITHM.md`)

فقط کندل‌های **بسته‌شده** ارزیابی می‌شوند؛ هر کندل ۱۵دقیقه یک‌بار.

- **فیلتر هزینه‌محور:** `ATR% ≥ min_atr_pct` (پیش‌فرض ۰٫۲۵٪). با کارمزد ۰٫۳۵٪ در هر سمت، حرکتی که
  چند برابر هزینهٔ رفت‌وبرگشت نباشد از پیش بازنده است. این فیلتر دلیل اصلی «معاملهٔ کم» است.
- **dip (بازگشت از حمایت):** `RSI ≤ 45`، `bb_pos ≤ 0.30`، کندل سبز، `trend_gap > −4٪`، `ret_4h > −6٪`.
- **breakout (شکست با حجم):** بسته‌شدن بالای سقف ۲۰ کندل، حجم ≥ ۱٫۸× میانگین، بالای EMA96، `55 ≤ RSI ≤ 72`.
- **دروازهٔ AI:** هر سیگنال باید `{approve, confidence ≥ 0.60, size_mult, reason}` معتبر از مدل بگیرد؛
  خطا/JSON نامعتبر = **رد** (fail-closed). در نبود کلید، مسیر rules-only با آستانهٔ `rules_min_score` اجرا می‌شود.
- **خروج:** استاپ اولیه (ATR×2.5، کف ۳٪، سقف ۶٪)، تارگت (ATR×5، حداقل ۶٪)، تریلینگ بعد از +۴٪،
  خروج زمانی ۴۸ ساعت، و **تسویهٔ اجباری** ۴۵ دقیقه قبل از پایان چالش (ورود جدید از ۴ ساعت قبل ممنوع).

## ۵) هوش مصنوعی: کجا و چطور؟ (جزئیات: `docs/AI_USAGE.md`)

مدل AI پارامتر عددی را تغییر نمی‌دهد و سفارش نمی‌سازد؛ سه نقش قابل‌ممیزی دارد:

1. **دروازهٔ ورود** — سیگنال + ویژگی‌ها + اسپرد + پوزیشن‌ها + درس‌های قبلی را می‌بیند و approve/reject می‌دهد.
2. **ناظر ریسک** — هر ۳ ساعت `normal | defensive | pause` را پیشنهاد می‌دهد؛ `pause` حداقل ۶۰ دقیقه قفل است.
3. **تحلیل‌گر پس از معامله** — از آمار اشتباهات، «درس» متنی می‌نویسد که به پرامپت دروازه برمی‌گردد.

هر فراخوانی در `logs/ai_calls.jsonl` (خلاصه، بدون کلید) و نتیجهٔ آن در `logs/decisions.jsonl` ثبت می‌شود.
پیش‌فرض `claude-sonnet-5-5` است؛ هر سرویس سازگار با OpenAI با `WTB_AI_PROVIDER=openai` + `WTB_AI_BASE_URL` پشتیبانی می‌شود.

## ۶) ریسک و ایمنی سفارش (کامل: `docs/RISK.md`)

- حداکثر ۳ پوزیشن، هر کدام ≤۲۵٪ سرمایه، اکسپوژر کل ≤۷۵٪، همیشه ≥۱۰٪ نقد؛ ریسک هر معامله ۰٫۸٪.
- حد ضرر روزانه ۲٫۵٪؛ **Kill-Switch** با افت ۵٪ از اوج: ورود متوقف + تسویهٔ کامل.
- ژورنال **پیش از** ارسال سفارش؛ ثبت سفارش هرگز خودکار retry نمی‌شود؛ ۴۰۹/۴۲۹/۵xx/پاسخ نامعتبر =
  «نامعلوم» → جست‌وجو با `client_id`، وگرنه توقف کامل (`halt`)؛ لغو فقط با GET و `active=false` تأیید می‌شود.
- دروازهٔ live: `WTB_MAX_DRAWDOWN`/`SYMBOLS`/`CHALLENGE_END` صریح + `WTB_LIVE_ACK` + `selfcheck` واقعی +
  `safetytests` هم‌خوان با هش کد + کلید AI. هر تغییر کد، `safetytests` را باطل می‌کند.

## ۷) بک‌تست و Replay

```bash
python -m wallexbot backtest                 # rules-only (محافظه‌کارانه)
python -m wallexbot backtest --learn         # با learner در حلقه
python -m wallexbot backtest --ai-gate       # با AI واقعی در حلقه (هزینهٔ توکن)
python -m wallexbot backtest --synthetic     # فقط smoke؛ برچسب SYNTHETIC_NOT_EVIDENCE
python -m wallexbot replay                   # Replay سریع در ترمینال (بدون UI)
python -m wallexbot tune --grid quick        # walk-forward؛ انتخاب روی train، گزارش روی test
python -m wallexbot optimize --ai --rounds 2 # چرخهٔ کامل: سناریوها + تحلیل AI + پیشنهاد برای تأیید
```

**نتیجهٔ فعلی (دادهٔ واقعی ۲ روزه):**

| سنجه | بک‌تست | Replay |
|---|---|---|
| بازده | −۰٫۹۷٪ | −۱٫۲۵٪ |
| معاملات | ۴ | ۴ |
| نرخ برد | ۵۰٪ | ۲۵٪ |
| حداکثر افت | ۱٫۷۱٪ | ۱٫۴۶٪ |
| Profit Factor | ۰٫۲۶ | — |
| وضعیت پایان | تسویهٔ نقدی | `flat_check: flat` |

**تفسیر صادقانه:** روی این نمونهٔ کوچک، استراتژی از کارمزدها و استاپ‌ها ضرر خورده است؛ هیچ ترکیبی در sweep
۳۰۶ حالتی سود پایدار نداد. علت و مسیر بهبود در `docs/ZERO_TRADES_REPORT.md` و `docs/REPRODUCIBILITY.md` آمده است.
اگر دادهٔ ۳۰ روزه جمع شود، `tune` می‌تواند پارامتر را روی بازهٔ آموزش انتخاب و روی بازهٔ آزمون بسنجد.

### چرخهٔ بهینه‌سازی (کامل در [`docs/OPTIMIZER.md`](docs/OPTIMIZER.md))

```bash
python -m wallexbot fetch --days 30         # پیش‌نیاز: دادهٔ کافی (۳۰ روز)
python -m wallexbot optimize --ai --rounds 2
```

سه پنجرهٔ پیوسته (۶۰٪ آموزش / ۲۰٪ اعتبارسنجی / ۲۰٪ آزمون) با شکاف purge؛ ۸ سناریوی نام‌دار
(محافظه‌کار، فعال، تک‌الگویی، استاپ تنگ/گشاد …) + جهش محلی + پیشنهادهای AI. انتخاب فقط با آموزش،
رتبه‌بندی با اعتبارسنجی، و پنجرهٔ آزمون **یک‌بار** در پایان و **بدون حضور در انتخاب** اجرا می‌شود
(به AI هم نشان داده نمی‌شود). خروجی `logs/optimizer/<run>/` شامل `report.json`، `proposal.json`،
`summary.md` است و `status: pending_approval` دارد؛ اعمال فقط با تأیید صریح:

```bash
python -m wallexbot optimize --list
python -m wallexbot optimize --approve <run-id> --yes   # سپس params و safetytests دوباره
```

## ۸) پنل وب

| مسیر | کار |
|---|---|
| `GET /api/health` | سلامت، حالت دمو/فقط‌خواندنی |
| `GET /api/status` | equity، پوزیشن‌ها، ریسک، curve، وضعیت replay |
| `GET /api/trades`، `/api/decisions` | لاگ معاملات و تصمیم‌ها (قابل فیلتر با `action=`) |
| `GET /api/backtest?name=real|learn|web|synthetic` | گزارش + curve + معاملات |
| `GET /api/gate`، `/api/settings`، `/api/profiles`، `/api/jobs` | دروازهٔ live، تنظیمات، پروفایل‌ها، کارهای پس‌زمینه |
| `POST /api/bot/start|stop`، `/api/flatten`، `/api/reset-kill` | کنترل ربات |
| `POST /api/settings`، `/api/test/connection`، `/api/test/ai` | تنظیم و آزمون اتصال |
| `POST /api/backtest/run`، `/api/tests/run`، `/api/replay/start|stop` | اجرای بک‌تست/تست/Replay |
| `GET /api/optimizer`، `/api/optimizer/<run>`، `POST /api/optimizer/start|stop|approve` | چرخهٔ بهینه‌سازی و تأیید پیشنهاد |
| `GET/POST /api/mcp/tools`، `/api/mcp/call` | ابزارهای MCP والکس |

نکتهٔ امنیتی: پنل به‌طور پیش‌فرض فقط روی `127.0.0.1` گوش می‌دهد. اگر `--host 0.0.0.0` می‌دهید، حتماً
`--token` بگذارید و پنل را پشت VPN/فایروال نگه دارید.

## ۹) MCP والکس

```bash
python -m wallexbot mcp --list
python -m wallexbot mcp --call market_snapshot --args '{"symbol":"BTCTMN"}'
```

کلاینت JSON-RPC 2.0 روی HTTP با پشتیبانی SSE، پروتکل `2025-06-18`، نگه‌داشتن `Mcp-Session-Id`، هدر
`Accept: application/json, text/event-stream`، و تبدیل خطای ابزار به استثنا. در صورت فعال‌سازی
(`WTB_MCP_ENABLED=true`) خروجی snapshot به‌عنوان **زمینهٔ اختیاری** به پرامپت AI اضافه می‌شود؛ نبود آن مانع معامله نیست.

## ۱۰) دستورهای CLI

```bash
python -m wallexbot run --mode shadow|live
python -m wallexbot fetch --days 30 [--allow-partial]
python -m wallexbot backtest [--learn] [--ai-gate] [--synthetic]
python -m wallexbot replay [--speed 5000] [--loop]
python -m wallexbot tune [--grid quick|full] [--split 0.7]
python -m wallexbot optimize [--ai] [--rounds 2] [--synthetic] [--list] [--approve <run-id> --yes]
python -m wallexbot profiles [--apply active --write]
python -m wallexbot selfcheck --yes-place-test-order
python -m wallexbot safetytests
python -m wallexbot params
python -m wallexbot flatten | report | reset-kill | account | doctor
python -m wallexbot mcp --list | --call <tool> --args '<json>'
python -m wallexbot ui [--demo] [--host] [--port] [--token] [--autostart] [--speed]
python -m unittest discover -s tests -t .
```

## ۱۱) پیکربندی

همهٔ متغیرها با پیشوند `WTB_` (به‌جز `WALLEX_API_KEY`) و از `.env` یا محیط خوانده می‌شوند؛ فایل نمونه:
[`.env.example`](.env.example). مهم‌ترین‌ها:

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `WALLEX_API_KEY` | — | کلید والکس (`X-API-Key`؛ حداکثر ۹۰ روز اعتبار) |
| `WTB_SYMBOLS` | ۸ جفت TMN | **برای live باید صریح تعیین شود** |
| `WTB_MAX_DRAWDOWN` | 0.05 | **برای live باید صریح باشد**؛ انتخاب خودتان را ثبت کنید |
| `WTB_CHALLENGE_END` | — | ISO-8601 با منطقهٔ زمانی؛ **برای live الزامی** |
| `WTB_LIVE_ACK` | — | اثرانگشت پارامترها از `params` |
| `WTB_AI_PROVIDER` / `WTB_AI_MODEL` | anthropic / `claude-sonnet-5-5` | یا `openai` + `WTB_AI_BASE_URL` |
| `WTB_FEE_RATE` | 0.0035 | در live با `max(این، taker واقعی)` جایگزین می‌شود |
| `WTB_MIN_ATR_PCT` | 0.0025 | فیلتر نوسان؛ پایین‌تر = معامله بیشتر و کارمزد بیشتر |
| `WTB_MAX_TRADES_PER_DAY` | 20 | سقف معاملات بسته‌شده در روز |

## ۱۲) مستندات داوری

| فایل | محتوا |
|---|---|
| [`docs/ALGORITHM.md`](docs/ALGORITHM.md) | قوانین سیگنال، اندیکاتورها، خروج، learner |
| [`docs/AI_USAGE.md`](docs/AI_USAGE.md) | نقش‌ها، پرامپت‌ها، اعتبارسنجی JSON، fail-closed، لاگ |
| [`docs/RISK.md`](docs/RISK.md) | سقف‌ها، sizing، kill-switch، ایمنی سفارش، سناریوهای شکست |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | راه‌اندازی، دروازهٔ live، عملیات، بازیابی، عیب‌یابی، راهنمای UI |
| [`docs/OPTIMIZER.md`](docs/OPTIMIZER.md) | چرخهٔ بهینه‌سازی: پنجره‌ها، امتیاز مرکب، سناریوها، نقش AI، تأیید |
| [`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md) | داده، دستورها، هش‌ها، تست‌ها، محدودیت‌ها |
| [`docs/SCORING_MAP.md`](docs/SCORING_MAP.md) | نقشهٔ ۳۰۰ امتیاز داوری به فایل/قابلیت |
| [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) | سناریوی ویدیوی ۵ دقیقه‌ای |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | تصمیم‌های طراحی و چرایی آن‌ها |
| [`docs/DATA_FETCH.md`](docs/DATA_FETCH.md) | روش دریافت/تجمیع داده |
| [`docs/ZERO_TRADES_REPORT.md`](docs/ZERO_TRADES_REPORT.md) | تحلیل قیف سیگنال و صفر/کم‌معامله بودن |
| [`docs/FIXES_2026-10.md`](docs/FIXES_2026-10.md) | فهرست باگ‌های رفع‌شده و شکاف‌های باقی‌مانده |
| [`docs/evidence/replay_summary.json`](docs/evidence/replay_summary.json) | شاهد خروجی Replay (۴ معامله، flat_check) |
| [`docs/evidence/optimizer/`](docs/evidence/optimizer/) | گزارش خروجی چرخهٔ بهینه‌سازی + پیام مسدودشدن دادهٔ کوتاه |

## ۱۳) محدودیت‌ها (صادقانه)

- دادهٔ موجود فقط **۲ روز** است؛ هر عدد عملکردی روی این نمونه **نویزی** است. برای داوری، دادهٔ بیشتر بگیرید.
- مدل درون‌کندلی بک‌تست محافظه‌کارانه است (استاپ مقدم بر تارگت)؛ نتیجهٔ واقعی می‌تواند بهتر یا بدتر باشد.
- تست‌های API روی حساب واقعی (selfcheck) در محیط توسعهٔ فعلی ممکن نبود؛ کد بر اساس مستندات رسمی و
  fixtureها نوشته و با fake server تست شده است. **قبل از live، `selfcheck` را روی حساب خود اجرا کنید.**
- کلید AI و دسترسی به Anthropic ممکن است در ایران نیاز به پروکسی/سرویس واسط داشته باشد؛ مسیر `openai`-compatible
  برای همین پیش‌بینی شده است.
- هیچ سودآوری‌ای تضمین نمی‌شود. این یک موتور قابل‌ممیزی است، نه سیگنال فروش.

## ۱۴) ساختار مخزن

```text
wallex-ai-trader/
├── wallexbot/            موتور (config, exchange, schemas, strategy, risk, orders, executors,
│                         bot, backtest, replay, learner, ai_agent, mcp, safety, web)
│   └── webui/            پنل (HTML/CSS/JS بدون build)
├── tests/                ۱۰۱ تست + fake exchange + fixtures مستندات
├── data/                 کندل واقعی ۱۵m + manifest (۸ نماد)
├── logs/backtest_real/   گزارش بک‌تست قابل‌بازتولید (JSON/CSV)
├── docs/                 مستندات فارسی داوری
├── .env.example          نمونهٔ تنظیمات
└── requirements.txt      requests
```
