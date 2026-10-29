import os
import time
import requests

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BASE = "https://api.gateio.ws/api/v4"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PumpHunterBot/1.1"})


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
    response = SESSION.get(BASE + path, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def scan():
    # Gate.io public USDT perpetual market data; no API key required.
    tickers = get_json("/futures/usdt/tickers")
    liquid = []
    print(f"Gate.io tickers received: {len(tickers)}")
    if tickers:
        print(f"Ticker fields sample: {sorted(tickers[0].keys())}")
    for item in tickers:
        contract = item.get("contract", "")
        turnover = float(item.get("volume_24h_quote") or item.get("volume_24h_usd") or item.get("volume_24h") or 0)
        if contract.endswith("_USDT") and turnover >= 5_000_000:
            liquid.append((item, turnover))
    coins = sorted(liquid, key=lambda pair: pair[1], reverse=True)[:30]
    print(f"Gate.io liquid contracts checked: {len(coins)}")

    alerts = 0
    candle_checks = 0
    volume_spikes = 0
    oi_checks = 0
    for ticker, _turnover24h in coins:
        contract = ticker["contract"]
        try:
            raw = get_json(
                "/futures/usdt/candlesticks",
                {"contract": contract, "interval": "5m", "limit": 25},
            )
            # Normalize response order using the candle timestamp.
            candles = sorted(raw, key=lambda candle: int(candle["t"]))
            if len(candles) < 22:
                print(f"{contract}: not enough candle data ({len(candles)})")
                continue

            # Exclude the current, still-forming candle.
            candle_checks += 1
            closed = candles[-2]
            previous = candles[-22:-2]
            avg_turnover = sum(float(c.get("sum", 0) or 0) for c in previous) / len(previous)
            turnover = float(closed.get("sum", 0) or 0)
            open_price = float(closed["o"])
            close_price = float(closed["c"])

            if avg_turnover <= 0 or turnover < avg_turnover * 2 or close_price <= open_price:
                continue
            volume_spikes += 1
            print(f"{contract}: volume spike {turnover / avg_turnover:.2f}x, candle change {(close_price / open_price - 1) * 100:.2f}%")

            now = int(time.time())
            oi_rows = get_json(
                "/futures/usdt/contract_stats",
                {
                    "contract": contract,
                    "from": now - 900,
                    "to": now,
                    "interval": "5m",
                    "limit": 3,
                },
            )
            oi_rows = sorted(oi_rows, key=lambda row: int(row.get("time", 0)))
            oi_rows = [row for row in oi_rows if row.get("open_interest") not in (None, "")]
            if len(oi_rows) < 2:
                print(f"{contract}: not enough OI history")
                continue
            oi_checks += 1
            old_oi = float(oi_rows[-2]["open_interest"])
            new_oi = float(oi_rows[-1]["open_interest"])
            if old_oi <= 0:
                continue
            oi_change = (new_oi / old_oi - 1) * 100
            print(f"{contract}: OI change {oi_change:.3f}%")
            if oi_change < 0.5:
                print(f"{contract}: skipped because OI change is below 0.5%")
                continue

            price_change = (close_price / open_price - 1) * 100
            message = (
                f"🚨 احتمال شروع حرکت: {contract.replace('_', '/')} (Gate.io USDT Perpetual)\n"
                f"تغییر کندل بسته‌شده ۵ دقیقه: +{price_change:.2f}%\n"
                f"ارزش معاملات نسبت به میانگین ۲۰ کندل: {turnover / avg_turnover:.1f} برابر\n"
                f"تغییر OI در داده‌های اخیر: +{oi_change:.2f}%\n"
                f"قیمت بسته‌شدن کندل: {close_price}\n"
                "⚠️ هشدار برای بررسی بیشتر است؛ سیگنال قطعی خرید نیست."
            )
            send_message(message)
            alerts += 1
            print(f"Alert sent: {contract}")
            time.sleep(1)
        except Exception as error:
            print(f"{contract}: {type(error).__name__}: {error}")

    print(f"Diagnostics: candle_checks={candle_checks}, volume_spikes={volume_spikes}, oi_checks={oi_checks}")
    print(f"Scan finished. Alerts sent: {alerts}")


if __name__ == "__main__":
    if not TOKEN or not CHAT_ID:
        raise SystemExit("BOT_TOKEN یا CHAT_ID در GitHub Secrets تنظیم نشده است.")
    scan()
