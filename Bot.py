import os
import time
import requests

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BASE = "https://fapi.binance.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PumpHunterBot/1.0"})


def send_message(message):
    if not TOKEN or not CHAT_ID:
        print("BOT_TOKEN یا CHAT_ID تنظیم نشده است.")
        return
    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": message},
        timeout=15,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


def get_json(path, params=None):
    response = SESSION.get(BASE + path, params=params, timeout=15)
    response.raise_for_status()
    return response.json()


def scan():
    tickers = get_json("/fapi/v1/ticker/24hr")
    liquid = [
        item for item in tickers
        if item.get("symbol", "").endswith("USDT")
        and float(item.get("quoteVolume", 0)) >= 5_000_000
    ]
    coins = sorted(
        liquid, key=lambda item: float(item.get("quoteVolume", 0)), reverse=True
    )[:30]

    alerts = 0
    for ticker in coins:
        symbol = ticker["symbol"]
        try:
            candles = get_json(
                "/fapi/v1/klines",
                {"symbol": symbol, "interval": "5m", "limit": 22},
            )
            if not isinstance(candles, list) or len(candles) < 22:
                continue

            # Last fully closed candle; exclude the current, still-forming candle.
            closed = candles[-2]
            previous = candles[-22:-2]
            avg_volume = sum(float(candle[5]) for candle in previous) / len(previous)
            volume = float(closed[5])
            open_price = float(closed[1])
            close_price = float(closed[4])

            if avg_volume <= 0 or volume < avg_volume * 2 or close_price <= open_price:
                continue

            oi_history = get_json(
                "/futures/data/openInterestHist",
                {"symbol": symbol, "period": "5m", "limit": 2},
            )
            if not isinstance(oi_history, list) or len(oi_history) < 2:
                continue
            old_oi = float(oi_history[-2]["sumOpenInterest"])
            new_oi = float(oi_history[-1]["sumOpenInterest"])
            if old_oi <= 0:
                continue
            oi_change = (new_oi / old_oi - 1) * 100

            # Require OI growth too, to reduce alerts from price/volume alone.
            if oi_change < 0.5:
                continue

            price_change = (close_price / open_price - 1) * 100
            message = (
                f"🚨 احتمال شروع حرکت: {symbol}\n"
                f"تغییر کندل بسته‌شده ۵ دقیقه: +{price_change:.2f}%\n"
                f"حجم نسبت به میانگین ۲۰ کندل: {volume / avg_volume:.1f} برابر\n"
                f"تغییر OI در داده‌های ۵ دقیقه‌ای: +{oi_change:.2f}%\n"
                f"قیمت بسته‌شدن کندل: {close_price}\n"
                "⚠️ هشدار برای بررسی بیشتر است؛ سیگنال قطعی خرید نیست."
            )
            send_message(message)
            alerts += 1
            print(f"Alert sent: {symbol}")
            time.sleep(1)
        except Exception as error:
            print(f"{symbol}: {error}")

    print(f"Scan finished. Alerts sent: {alerts}")


if __name__ == "__main__":
    if not TOKEN or not CHAT_ID:
        raise SystemExit("BOT_TOKEN یا CHAT_ID در GitHub Secrets تنظیم نشده است.")
    try:
        scan()
    except Exception as error:
        print(f"خطای اصلی اسکن: {error}")
        raise
