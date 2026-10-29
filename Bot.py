import os
import time
import requests

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BASE = "https://api.gateio.ws/api/v4"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PumpHunterBot/1.3"})


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


def closed_candles(contract, interval, limit):
    raw = get_json(
        "/futures/usdt/candlesticks",
        {"contract": contract, "interval": interval, "limit": limit},
    )
    candles = sorted(raw, key=lambda candle: int(candle["t"]))
    # Gate.io includes the currently forming candle; discard it.
    return candles[:-1]


def btc_market_filter():
    """Avoid new alerts when the last closed 15m BTC candle is sharply bearish."""
    candles = closed_candles("BTC_USDT", "15m", 4)
    if len(candles) < 2:
        print("BTC filter: insufficient 15m candle data; allowing scan")
        return True, None
    candle = candles[-1]
    open_price = float(candle["o"])
    close_price = float(candle["c"])
    change = (close_price / open_price - 1) * 100
    print(f"BTC 15m closed-candle change: {change:+.2f}%")
    if change <= -0.5:
        print("BTC filter: paused alerts because BTC 15m candle is down 0.5% or more")
        return False, change
    return True, change


def confirms_15m(contract):
    """Require the last closed 15m candle to be bullish and close above prior close."""
    candles = closed_candles(contract, "15m", 4)
    if len(candles) < 2:
        return False, None
    last = candles[-1]
    previous = candles[-2]
    open_price = float(last["o"])
    close_price = float(last["c"])
    previous_close = float(previous["c"])
    confirmed = close_price > open_price and close_price > previous_close
    change = (close_price / open_price - 1) * 100 if open_price else 0
    return confirmed, change


def confirms_1h(contract):
    """Use closed 1h candles: price above a rising EMA20 is the trend filter."""
    candles = closed_candles(contract, "1h", 24)
    if len(candles) < 21:
        return False, None, None

    closes = [float(candle["c"]) for candle in candles]
    alpha = 2 / (20 + 1)
    ema = closes[0]
    ema_values = [ema]
    for close in closes[1:]:
        ema = close * alpha + ema * (1 - alpha)
        ema_values.append(ema)

    last_close = closes[-1]
    ema_now = ema_values[-1]
    ema_previous = ema_values[-2]
    last_open = float(candles[-1]["o"])
    change = (last_close / last_open - 1) * 100 if last_open else 0

    # Keep the filter useful but avoid requiring an exceptionally strong hourly candle.
    confirmed = last_close > ema_now and ema_now >= ema_previous
    return confirmed, change, ema_now


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
        if contract.endswith("_USDT") and turnover >= 1_000_000:
            liquid.append((item, turnover))
    coins = sorted(liquid, key=lambda pair: pair[1], reverse=True)[:100]
    print(f"Gate.io liquid contracts checked: {len(coins)}")

    btc_ok, btc_change = btc_market_filter()
    if not btc_ok:
        print("Scan stopped before altcoin alerts due to BTC 15m weakness.")
        return

    alerts = 0
    candle_checks = 0
    volume_spikes = 0
    confirmations_15m = 0
    confirmations_1h = 0
    oi_checks = 0
    for ticker, _turnover24h in coins:
        contract = ticker["contract"]
        try:
            candles = closed_candles(contract, "5m", 25)
            if len(candles) < 21:
                print(f"{contract}: not enough closed 5m candle data ({len(candles)})")
                continue

            candle_checks += 1
            closed = candles[-1]
            previous = candles[-21:-1]
            avg_turnover = sum(float(c.get("sum", 0) or 0) for c in previous) / len(previous)
            turnover = float(closed.get("sum", 0) or 0)
            open_price = float(closed["o"])
            close_price = float(closed["c"])

            if avg_turnover <= 0 or turnover < avg_turnover * 1.5 or close_price <= open_price or (close_price / open_price - 1) < 0.002:
                continue
            volume_spikes += 1
            print(f"{contract}: 5m volume spike {turnover / avg_turnover:.2f}x, candle change {(close_price / open_price - 1) * 100:.2f}%")

            confirmed, change_15m = confirms_15m(contract)
            if not confirmed:
                candle_change_pct = (close_price / open_price - 1) * 100
                relative_volume = turnover / avg_turnover
                print(f"{contract}: skipped; 15m candle did not confirm bullish momentum")
                # Early-watch alert: catch unusually strong 5m moves before 15m confirmation.
                # This is deliberately labeled as unconfirmed and uses stricter thresholds.
                if relative_volume >= 5 and candle_change_pct >= 1:
                    btc_text = f"{btc_change:+.2f}%" if btc_change is not None else "نامشخص"
                    watch_message = (
                        f"👀 هشدار زودهنگام (هنوز تأیید نشده): {contract.replace('_', '/')}\n"
                        f"کندل بسته‌شده ۵ دقیقه: +{candle_change_pct:.2f}%\n"
                        f"حجم نسبت به میانگین ۲۰ کندل: {relative_volume:.1f} برابر\n"
                        f"تغییر کندل ۱۵دقیقه: {change_15m:+.2f}%؛ هنوز تأیید صعودی نداده\n"
                        f"تغییر کندل ۱۵دقیقه BTC: {btc_text}\n"
                        f"قیمت بسته‌شدن ۵دقیقه: {close_price}\n"
                        "⚠️ فقط برای زیرنظرگرفتن؛ ورود خرید بدون تأیید پول‌بک/شکست ساختار پرریسک است."
                    )
                    send_message(watch_message)
                    alerts += 1
                    print(f"Early-watch alert sent: {contract}")
                continue
            confirmations_15m += 1

            confirmed_1h, change_1h, ema20_1h = confirms_1h(contract)
            if not confirmed_1h:
                print(f"{contract}: skipped; 1h close is not above a rising EMA20")
                continue
            confirmations_1h += 1

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
            if oi_change < 0.2:
                print(f"{contract}: skipped because OI change is below 0.2%")
                continue

            price_change = (close_price / open_price - 1) * 100
            btc_text = f"{btc_change:+.2f}%" if btc_change is not None else "نامشخص"
            change_1h_text = f"{change_1h:+.2f}%" if change_1h is not None else "نامشخص"

            # Gate.io ticker funding_rate is a decimal fraction; convert to percent.
            # Negative funding is informative for possible short squeezes, not a required filter.
            raw_funding = ticker.get("funding_rate")
            try:
                funding_pct = float(raw_funding) * 100 if raw_funding not in (None, "") else None
            except (TypeError, ValueError):
                funding_pct = None
            if funding_pct is None:
                funding_text = "نامشخص"
            elif funding_pct < 0:
                funding_text = f"{funding_pct:+.4f}% (منفی؛ امتیاز مثبت برای احتمال شورت‌اسکوئیز)"
            elif funding_pct > 0:
                funding_text = f"{funding_pct:+.4f}% (مثبت؛ به‌تنهایی نشانه نزولی نیست)"
            else:
                funding_text = "0.0000% (نزدیک صفر)"
            message = (
                f"🚨 احتمال شروع حرکت: {contract.replace('_', '/')} (Gate.io USDT Perpetual)\n"
                f"تغییر کندل بسته‌شده ۵ دقیقه: +{price_change:.2f}%\n"
                f"تغییر کندل بسته‌شده ۱۵ دقیقه: +{change_15m:.2f}% (تأیید حرکت)\n"
                f"تغییر کندل بسته‌شده ۱ ساعته: {change_1h_text} (قیمت بالای EMA20 صعودی)\n"
                f"ارزش معاملات نسبت به میانگین ۲۰ کندل ۵دقیقه: {turnover / avg_turnover:.1f} برابر\n"
                f"تغییر OI در داده‌های اخیر: {oi_change:+.2f}%\n"
                f"Funding Rate: {funding_text}\n"
                f"تغییر کندل ۱۵دقیقه BTC: {btc_text}\n"
                f"قیمت بسته‌شدن کندل ۵دقیقه: {close_price}\n"
                "⚠️ هشدار برای بررسی بیشتر است؛ سیگنال قطعی خرید نیست."
            )
            send_message(message)
            alerts += 1
            print(f"Alert sent: {contract}")
            time.sleep(1)
        except Exception as error:
            print(f"{contract}: {type(error).__name__}: {error}")

    print(
        f"Diagnostics: candle_checks={candle_checks}, volume_spikes={volume_spikes}, "
        f"confirmations_15m={confirmations_15m}, confirmations_1h={confirmations_1h}, "
        f"oi_checks={oi_checks}"
    )
    print(f"Scan finished. Alerts sent: {alerts}")


if __name__ == "__main__":
    if not TOKEN or not CHAT_ID:
        raise SystemExit("BOT_TOKEN یا CHAT_ID در GitHub Secrets تنظیم نشده است.")
    scan()
