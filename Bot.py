import os
import time
import requests

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BASE = "https://api.bybit.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PumpHunterBot/1.0"})


def send_message(message):
    response = SESSION.post(
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
    data = response.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error: {data.get('retCode')} {data.get('retMsg')}")
    return data.get("result", {})


def scan():
    result = get_json("/v5/market/tickers", {"category": "linear"})
    tickers = result.get("list", [])
    liquid = [
        item for item in tickers
        if item.get("symbol", "").endswith("USDT")
        and float(item.get("turnover24h", 0)) >= 5_000_000
    ]
    coins = sorted(
        liquid, key=lambda item: float(item.get("turnover24h", 0)), reverse=True
    )[:30]

    alerts = 0
    for ticker in coins:
        symbol = ticker["symbol"]
        try:
            kline_result = get_json(
                "/v5/market/kline",
                {"category": "linear", "symbol": symbol, "interval": "5", "limit": 22},
            )
            # Bybit returns candles newest-first, so reverse to oldest-first.
            candles = list(reversed(kline_result.get("list", [])))
            if len(candles) < 22:
                continue

            # Ignore the current, still-forming candle.
            closed = candles[-2]
            previous = candles[-22:-2]
            avg_turnover = sum(float(candle[6]) for candle in previous) / len(previous)
            turnover = float(closed[6])
            open_price = float(closed[1])
            close_price = float(closed[4])

            if avg_turnover <= 0 or turnover < avg_turnover * 2 or close_price <= open_price:
                continue

            oi_result = get_json(
                "/v5/market/open-interest",
                {"category": "linear", "symbol": symbol, "intervalTime": "5min", "limit": 2},
            )
            oi_rows = list(reversed(oi_result.get("list", [])))
            if len(oi_rows) < 2:
                continue
            old_oi = float(oi_rows[-2]["openInterest"])
            new_oi = float(oi_rows[-1]["openInterest"])
            if old_oi <= 0:
                continue
            oi_change = (new_oi / old_oi - 1) * 100

            if oi_change < 0.5:
                continue

            price_change = (close_price / open_price - 1) * 100
            message = (
                f"🚨 احتمال شروع حرکت: {symbol} (Bybit USDT Perpetual)\n"
                f"تغییر کندل بسته‌شده ۵ دقیقه: +{price_change:.2f}%\n"
                f"ارزش معاملات نسبت به میانگین ۲۰ کندل: {turnover / avg_turnover:.1f} برابر\n"
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
    scan()
