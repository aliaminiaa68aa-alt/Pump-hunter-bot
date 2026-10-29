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


def load_alert_state():
    import json
    from pathlib import Path
    path = Path(".bot_state/alerts.json")
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def save_alert_state(state):
    import json
    from pathlib import Path
    path = Path(".bot_state/alerts.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def find_recent_setup(candles):
    """
    Find a recent high-volume 5m impulse, a pullback that holds its low,
    and a closed 5m candle that breaks above the short-term pullback highs.
    Returns setup details only when the latest closed candle confirms the break.
    """
    if len(candles) < 24:
        return None

    latest = candles[-1]
    latest_open = float(latest["o"])
    latest_close = float(latest["c"])
    if latest_close <= latest_open:
        return None

    # Look for an impulse in the previous 2-7 closed candles, not the breakout candle.
    first_idx = max(20, len(candles) - 8)
    last_idx = len(candles) - 2
    impulse_idx = None
    impulse_ratio = 0.0
    for idx in range(first_idx, last_idx + 1):
        previous = candles[idx - 20:idx]
        avg_turnover = sum(float(item.get("sum", 0) or 0) for item in previous) / 20
        candle = candles[idx]
        turnover = float(candle.get("sum", 0) or 0)
        open_price = float(candle["o"])
        close_price = float(candle["c"])
        change_pct = (close_price / open_price - 1) * 100 if open_price else 0
        ratio = turnover / avg_turnover if avg_turnover > 0 else 0
        if ratio >= 5 and change_pct >= 1 and close_price > open_price:
            impulse_idx = idx
            impulse_ratio = ratio

    if impulse_idx is None:
        return None

    impulse = candles[impulse_idx]
    after_impulse = candles[impulse_idx + 1:-1]
    if len(after_impulse) < 1:
        return None

    impulse_low = min(float(impulse["l"]), float(impulse["o"]), float(impulse["c"]))
    pullback_low = min(float(item["l"]) for item in after_impulse)
    # Reject the setup if any post-impulse candle closed below the impulse low.
    if pullback_low <= impulse_low:
        return None

    # Break of structure: current closed candle closes above the prior 3 candles' highs.
    recent_before_break = candles[-4:-1]
    if len(recent_before_break) < 3:
        return None
    short_term_high = max(float(item["h"]) for item in recent_before_break)
    if latest_close <= short_term_high:
        return None

    stop = min(float(item["l"]) for item in candles[max(impulse_idx, len(candles) - 6):-1])
    if stop >= latest_close:
        return None

    return {
        "impulse_time": str(impulse.get("t", impulse_idx)),
        "impulse_change": (float(impulse["c"]) / float(impulse["o"]) - 1) * 100,
        "impulse_ratio": impulse_ratio,
        "entry": latest_close,
        "stop": stop,
        "break_high": short_term_high,
        "risk": latest_close - stop,
        "candle_time": str(latest.get("t", "")),
    }


def scan():
    # Public Gate.io USDT perpetual market data; no exchange API key required.
    tickers = get_json("/futures/usdt/tickers")
    liquid = []
    print(f"Gate.io tickers received: {len(tickers)}")
    for item in tickers:
        contract = item.get("contract", "")
        turnover24 = float(item.get("volume_24h_quote") or item.get("volume_24h_usd") or item.get("volume_24h") or 0)
        if contract.endswith("_USDT") and turnover24 >= 1_000_000:
            liquid.append((item, turnover24))
    coins = sorted(liquid, key=lambda pair: pair[1], reverse=True)[:100]
    print(f"Gate.io liquid contracts checked: {len(coins)}")

    btc_ok, btc_change = btc_market_filter()
    if not btc_ok:
        print("Scan stopped before alerts because BTC 15m candle is sharply bearish.")
        return

    state = load_alert_state()
    alerts = 0
    candle_checks = 0
    impulses = 0
    confirms15 = 0
    confirms1h = 0
    oi_checks = 0
    structure_breaks = 0

    for ticker, _turnover24h in coins:
        contract = ticker["contract"]
        try:
            candles = closed_candles(contract, "5m", 40)
            if len(candles) < 24:
                continue
            candle_checks += 1

            setup = find_recent_setup(candles)
            if not setup:
                continue
            impulses += 1

            confirmed15, change15 = confirms_15m(contract)
            if not confirmed15:
                print(f"{contract}: setup watched, waiting for bullish 15m confirmation")
                continue
            confirms15 += 1

            confirmed1h, change1h, ema20 = confirms_1h(contract)
            if not confirmed1h:
                print(f"{contract}: 15m confirmed, but 1h trend filter not confirmed")
                continue
            confirms1h += 1

            now = int(time.time())
            oi_rows = get_json(
                "/futures/usdt/contract_stats",
                {"contract": contract, "from": now - 900, "to": now, "interval": "5m", "limit": 3},
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
            if oi_change < 0.2:
                print(f"{contract}: skipped; OI change {oi_change:.3f}% is below +0.2%")
                continue
            structure_breaks += 1

            # One confirmed alert per impulse candle, persisted between scheduled runs.
            if state.get(contract) == setup["impulse_time"]:
                print(f"{contract}: confirmed setup already alerted for this impulse")
                continue

            entry = setup["entry"]
            stop = setup["stop"]
            risk = setup["risk"]
            tp1 = entry + risk
            tp2 = entry + 2 * risk
            btc_text = f"{btc_change:+.2f}%" if btc_change is not None else "نامشخص"
            change1h_text = f"{change1h:+.2f}%" if change1h is not None else "نامشخص"
            raw_funding = ticker.get("funding_rate")
            try:
                funding_pct = float(raw_funding) * 100 if raw_funding not in (None, "") else None
            except (TypeError, ValueError):
                funding_pct = None
            funding_text = f"{funding_pct:+.4f}%" if funding_pct is not None else "نامشخص"

            message = (
                f"✅ تأیید نهایی ستاپ صعودی: {contract.replace('_', '/')} (Gate.io Perpetual)\n"
                f"کندل ۵دقیقه: شکست سقف کوتاه‌مدت تأیید شد\n"
                f"کندل ۱۵دقیقه: تأیید صعودی ({change15:+.2f}%)\n"
                f"روند ۱ساعته: بالای EMA20 صعودی ({change1h_text})\n"
                f"حرکت اولیه: {setup['impulse_change']:+.2f}%؛ حجم {setup['impulse_ratio']:.1f} برابر میانگین\n"
                f"تغییر OI: {oi_change:+.2f}% | Funding: {funding_text}\n"
                f"BTC در ۱۵دقیقه: {btc_text}\n\n"
                f"ورود پیشنهادی (بسته‌شدن کندل تأیید): {entry}\n"
                f"حد ضرر پیشنهادی: {stop}\n"
                f"هدف ۱ (ریسک‌به‌بازده ۱:۱): {tp1}\n"
                f"هدف ۲ (ریسک‌به‌بازده ۱:۲): {tp2}\n"
                "⚠️ سطوح محاسباتی و تقریبی‌اند، تضمین سود نیستند؛ قبل از ورود اسپرد، نقدشوندگی و فاصله حد ضرر را بررسی کن."
            )
            send_message(message)
            state[contract] = setup["impulse_time"]
            save_alert_state(state)
            alerts += 1
            print(f"Confirmed alert sent: {contract}")
            time.sleep(1)
        except Exception as error:
            print(f"{contract}: {type(error).__name__}: {error}")

    print(
        f"Diagnostics: candle_checks={candle_checks}, recent_impulses={impulses}, "
        f"confirmations_15m={confirms15}, confirmations_1h={confirms1h}, "
        f"oi_checks={oi_checks}, structure_breaks={structure_breaks}"
    )
    print(f"Scan finished. Confirmed alerts sent: {alerts}")


if __name__ == "__main__":
    if not TOKEN or not CHAT_ID:
        raise SystemExit("BOT_TOKEN یا CHAT_ID در GitHub Secrets تنظیم نشده است.")
    scan()


if __name__ == "__main__":
    if not TOKEN or not CHAT_ID:
        raise SystemExit("BOT_TOKEN یا CHAT_ID در GitHub Secrets تنظیم نشده است.")
    scan()
