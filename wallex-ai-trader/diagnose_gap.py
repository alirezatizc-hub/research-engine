
import requests
from datetime import datetime, timezone

URL = "https://api.wallex.ir/v1/udf/history"
SYMBOLS = ("BTCTMN", "USDTTMN")
DAY = (2026, 9, 22)


def ts(hour, minute):
    return int(datetime(
        *DAY, hour, minute, tzinfo=timezone.utc
    ).timestamp())


def fetch(symbol, start, end, resolution="1"):
    params = {
        "symbol": symbol,
        "resolution": resolution,
        "from": start,
        "to": end - 1,
    }

    try:
        response = requests.get(URL, params=params, timeout=20)
        result = {
            "http": response.status_code,
            "status": None,
            "times": [],
            "error": None,
        }

        if response.status_code != 200:
            result["error"] = response.text[:200]
            return result

        data = response.json()
        result["status"] = data.get("s")

        if data.get("s") == "ok":
            result["times"] = data.get("t", [])

        return result

    except (requests.RequestException, ValueError) as exc:
        return {
            "http": None,
            "status": None,
            "times": [],
            "error": repr(exc),
        }


def show(label, result):
    times = result["times"]
    print(
        f"{label}: HTTP={result['http']} "
        f"status={result['status']} count={len(times)}"
    )

    if times:
        print(
            "  first:",
            datetime.fromtimestamp(times[0], timezone.utc).isoformat(),
            "| last:",
            datetime.fromtimestamp(times[-1], timezone.utc).isoformat(),
        )

    if result["error"]:
        print("  error:", result["error"])


start = ts(12, 0)
middle = ts(13, 0)
end = ts(14, 0)

for symbol in SYMBOLS:
    print(f"\n===== {symbol} | resolution=1 =====")

    # یک درخواست برای کل بازهٔ دوساعته
    whole = fetch(symbol, start, end)
    show("WHOLE 12:00-14:00", whole)

    # دو درخواست مجاور، با مرز مشترک
    left = fetch(symbol, start, middle)
    right = fetch(symbol, middle, end)
    show("LEFT 12:00-13:00", left)
    show("RIGHT 13:00-14:00", right)

    # درخواست‌های کوچک در اطراف شروع و پایان شکاف
    probes = (
        ("BEFORE 12:30-12:40", ts(12, 30), ts(12, 40)),
        ("GAP START 12:40-12:50", ts(12, 40), ts(12, 50)),
        ("GAP MIDDLE 13:20-13:30", ts(13, 20), ts(13, 30)),
        ("GAP END 13:50-14:00", ts(13, 50), ts(14, 0)),
        ("AFTER 14:00-14:10", ts(14, 0), ts(14, 10)),
    )

    for label, a, b in probes:
        show(label, fetch(symbol, a, b))

    # مقایسهٔ رزولوشن ساعتی؛ برای تشخیص رفتار endpoint،
    # نه برای ساختن کندل‌های یک‌دقیقه‌ای
    print("-- hourly comparison --")
    show(
        "HOURLY 12:00-13:00",
        fetch(symbol, start, middle, resolution="60"),
    )
    show(
        "HOURLY 13:00-14:00",
        fetch(symbol, middle, end, resolution="60"),
    )
