import os
import time
import requests

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

BASE = "https://fapi.binance.com"

def send_message(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=15)

def scan():
    tickers = requests.get(
        BASE + "/fapi/v1/ticker/24hr", timeout=15
    ).json()

    coins = [
        t["symbol"] for t in sorted(
            [x for x in tickers if x["symbol"].endswith("USDT")],
            key=lambda x: float(x["quoteVolume"]),
            reverse=True
        )[:30]
    ]

    for symbol in coins:
        try:
            candles = requests.get(
                BASE + "/fapi/v1/klines",
                params={"symbol": symbol, "interval": "5m", "limit": 22},
                timeout=15
            ).json()

            closed = candles[-2]
            previous = candles[:-2]
            avg_volume = sum(float(c[5]) for c in previous[-20:]) / 20
            volume = float(closed[5])

            price_change = float(closed[4]) / float(closed[1]) - 1

            if avg_volume <= 0 or volume < avg_volume * 2:
                continue

            oi = requests.get(
                BASE + "/fapi/v1/openInterest",
                params={"symbol": symbol}, timeout=15
            ).json()
            open_interest = float(oi["openInterest"])

            # این نسخه فقط افزایش حجم و کندل مثبت را بررسی می‌کند.
            # تغییر واقعی OI بین دو زمان در نسخه بعد اضافه می‌شود.
            if price_change > 0:
                send_message(
                    f"🔎 احتمال شروع حرکت: {symbol}\n"
                    f"تغییر کندل ۵ دقیقه: {price_change*100:.2f}%\n"
                    f"حجم نسبت به میانگین: {volume/avg_volume:.1f} برابر\n"
                    f"قیمت فعلی: {closed[4]}\n"
                    f"⚠️ هشدار بررسی؛ سیگنال قطعی خرید نیست."
                )
                time.sleep(2)

        except Exception as e:
            print(symbol, e)

while True:
    try:
        if TOKEN and CHAT_ID:
            scan()
        else:
            print("BOT_TOKEN یا CHAT_ID تنظیم نشده است.")
    except Exception as e:
        print("خطا:", e)

    time.sleep(300)
