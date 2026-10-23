"""پروفایل‌های آمادهٔ ریسک/استراتژی. هیچ پروفایلی «توصیهٔ سود» نیست؛ فقط مجموعه‌ای از پارامترهاست.

پیش‌فرض Config (فایل config.py) با پروفایل active هم‌خوان است؛ این پروفایل‌ها فقط میان‌برِ تنظیم‌اند. تغییر پروفایل باید با بک‌تست روی دادهٔ واقعی و سپس shadow تأیید شود.
"""
from __future__ import annotations

PROFILES = {
    "conservative": {
        "title": "محافظه‌کار",
        "note": "فیلتر نوسان سخت‌گیرانه‌تر و آستانه بالاتر؛ معاملات کمتر، مناسب بازار کم‌نوسان",
        "params": {"min_atr_pct": 0.004, "rsi_buy": 40.0, "bb_max_pos": 0.20,
                   "min_score": 60.0, "rules_min_score": 65.0, "max_trades_per_day": 10},
    },
    "active": {
        "title": "فعال (هم‌خوان با پیش‌فرض)",
        "note": "آستانهٔ نوسان هزینه‌محور؛ تعداد معامله بیشتر، ریسک کارمزد بیشتر",
        "params": {"min_atr_pct": 0.0025, "rsi_buy": 45.0, "bb_max_pos": 0.30,
                   "min_score": 55.0, "rules_min_score": 55.0, "max_trades_per_day": 20},
    },
    "aggressive": {
        "title": "تهاجمی (فقط آزمایش)",
        "note": "برای بازار پرنوسان؛ در بازار کم‌نوسان کارمزدها را می‌خورَد",
        "params": {"min_atr_pct": 0.0015, "rsi_buy": 50.0, "bb_max_pos": 0.40,
                   "min_score": 50.0, "rules_min_score": 50.0, "max_trades_per_day": 30},
    },
}
