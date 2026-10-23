# نقشهٔ ۳۰۰ امتیاز داوری → شواهد در مخزن

این سند ادعای امتیاز نیست؛ فقط می‌گوید هر معیار کجاست و چطور بررسی شود.

## الف) عملکرد معاملاتی (۱۵۰)

| معیار | وضعیت فعلی | شاهد |
|---|---|---|
| بازده | −۰٫۹۷٪ در بک‌تست ۲روزه؛ −۱٫۲۵٪ در Replay | `logs/backtest_real/backtest_report.json`، `logs/replay_test/trades.csv` |
| کنترل افت سرمایه | حداکثر افت ۱٫۷۱٪ (بک‌تست) / ۱٫۴۶٪ (Replay)؛ سقف سخت ۵٪ + حد روزانه ۲٫۵٪ | `docs/RISK.md`، `risk.py`، `safety.py` |
| فعالیت | ۴ معامله در ۲ روز، ۸ نماد، ۱۴۳ ارزیابی و ۵۳ اسکن مسدودشدهٔ ثبت‌شده؛ ورودها فقط با سیگنال معتبر | `funnel` در گزارش، `decisions.jsonl` |
| پایداری | بدون Kill-Switch، بدون halt، تسویهٔ کامل در پایان (`flat_check`) | `logs/replay_test/state.json` |
| تحقق بازده در تومان | همهٔ معاملات با `cost_tmn`/`proceeds_tmn`/`pnl_tmn` ثبت و در پایان `final_liquidation` | `logs/trades.csv` |

**صداقت:** نمونهٔ ۲روزه برای قضاوت بازده کافی نیست. مسیر رسیدن به عدد معتبر (`fetch --days 30`،
`tune` walk-forward، `backtest --learn`) در `docs/REPRODUCIBILITY.md` آمده است.

## ب) فنی (۱۵۰)

### ۱) منطق استراتژی (حدود ۴۰)

- قوانین صریح و تک‌معنایی: `docs/ALGORITHM.md` + `strategy.py`
- تشخیص قابل‌آزمون (`diagnose`) هم‌خوان با `evaluate`؛ تست: `test_diagnose_is_consistent_with_evaluate`
- قیف کامل دلیل ردها: `funnel` در `backtest_report.json`
- فیلتر هزینه‌محور با توضیح ریاضی: بخش ۳ سند الگوریتم
- تست‌های سناریوی کنترل‌شده (dip/breakout/quiet/crash): `tests/test_strategy.py`, `tests/scenarios.py`

### ۲) استفادهٔ واقعی از AI (حدود ۴۰)

- چهار نقش با اثر روی تصمیم: دروازه، ناظر ریسک، تحلیل‌گر پس از معامله، تحلیل‌گر بهینه‌سازی — `docs/AI_USAGE.md`
- fail-closed + اعتبارسنجی نوع/بازه JSON — `ai_agent.py`، تست `test_ai_gate_fails_closed`
- لاگ هر فراخوانی (`ai_calls.jsonl`) و هر تصمیم (`DECISION` با approve/confidence/reason)
- پشتیبانی دو provider و مدل به‌روز `claude-sonnet-5-5`
- AI هرگز سفارش نمی‌سازد و پارامتر ریسک را عوض نمی‌کند (سند + کد)؛ در بهینه‌سازی فقط پیشنهاد می‌دهد و
  پیشنهادش با کران سخت سنجیده می‌شود و بدون تأیید انسان اعمال نمی‌شود — `optimizer.py`

### ۳) مدیریت ریسک (حدود ۳۵)

- سقف پوزیشن/اکسپوژر/نقد، sizing ریسک‌محور، استاپ/تارگت/تریلینگ — `docs/RISK.md`
- Kill-Switch و حد ضرر روزانه با تست‌های E2E — `tests/test_safety.py`
- دروازهٔ live چندلایه + هش کد + ACK + selfcheck واقعی — `safety.py`
- ایمنی سفارش: journal، بدون retry، تأیید لغو، reconcile — `orders.py` + ۹ تست بازیابی

### ۴) بازتولیدپذیری (حدود ۲۰)

- داده و manifest قفل‌شده، بدون نگاه به آینده، همان کد در بک‌تست/Replay/live
- هش کد در `safety_tests.json`، اثرانگشت پارامتر در `params`/`WTB_LIVE_ACK`
- ۱۰۰ تست خودکار (~۷ ثانیه، شامل ۱۷ تست چرخهٔ بهینه‌سازی) و `safetytests` با فهرست الزامی
- `docs/REPRODUCIBILITY.md` با اعداد مرجع

### ۵) بک‌تست و Shadow (حدود ۱۵)

- بک‌تست با کارمزد/لغزش و مدل درون‌کندلی محافظه‌کارانه
- Replay = همان `Bot` با `ShadowExecutor` و ساعت مجازی؛ قابل اجرا در UI با سرعت دلخواه
- تیونر walk-forward برای جلوگیری از overfit — `tune.py`
- چرخهٔ بهینه‌سازی سه‌پنجره‌ای (train/validation/test با purge)، سناریوهای نام‌دار، تحلیل AI و
  پیشنهاد `pending_approval` — `optimizer.py` + تب «بهینه‌سازی» + `docs/OPTIMIZER.md`
- گزارش قیف و `SYNTHETIC_NOT_EVIDENCE` برای دادهٔ مصنوعی

## ج) تحویل‌شدنی‌ها

| تحویل | فایل |
|---|---|
| سورس + مستندات MD | همین مخزن؛ `README.md` و `docs/*` |
| معماری | بخش ۳ README + `docs/SCORING_MAP.md` |
| قوانین خرید/فروش | `docs/ALGORITHM.md` |
| مدیریت ریسک | `docs/RISK.md` |
| استفادهٔ AI | `docs/AI_USAGE.md` |
| بازتولیدپذیری | `docs/REPRODUCIBILITY.md` |
| لاگ تصمیم هر معامله | `logs/decisions.jsonl`، `logs/trades.csv` |
| ویدیوی ۵ دقیقه‌ای | سناریو در `docs/DEMO_SCRIPT.md` |
| پنل کنترل | `python -m wallexbot ui` + `docs/RUNBOOK.md` |

## د) نقاط ضعف شناخته‌شده (برای شفافیت)

- نمونهٔ داده کوچک؛ عدد بازده قابل‌تعمیم نیست.
- `selfcheck` واقعی در محیط توسعه اجرا نشده (دسترسی به API نبود)؛ مسیر و تست‌های fake آماده است.
- UI ساده است (اولویت داوری روی موتور بوده)، اما همهٔ عملیات لازم را دارد.
- بازار اسپات TMN با کارمزد ۰٫۳۵٪ ذاتاً برای استراتژی‌های پرمعامله مناسب نیست؛ طراحی فعلی محافظه‌کار است.
