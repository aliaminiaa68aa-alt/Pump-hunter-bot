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
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    # Backward-compatible with the earlier flat state format.
    if "alerts" not in data or "candidates" not in data:
        data = {"alerts": data if isinstance(data, dict) else {}, "candidates": {}}
    data.setdefault("early_alerts", {})
    return data


def save_alert_state(state):
    import json
    from pathlib import Path
    path = Path(".bot_state/alerts.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def find_recent_impulse(candles):
    """Return a recent closed 5m high-volume impulse candle, if present."""
    if len(candles) < 24:
        return None
    first_idx = max(20, len(candles) - 8)
    last_idx = len(candles) - 2
    found = None
    for idx in range(first_idx, last_idx + 1):
        previous = candles[idx - 20:idx]
        avg_turnover = sum(float(item.get("sum", 0) or 0) for item in previous) / 20
        turnover = float(candles[idx].get("sum", 0) or 0)
        candle = candles[idx]
        open_price = float(candle["o"])
        close_price = float(candle["c"])
        change_pct = (close_price / open_price - 1) * 100 if open_price else 0
        ratio = turnover / avg_turnover if avg_turnover > 0 else 0
        # Keep the impulse selective, but not so strict that valid early moves are missed.
        if ratio >= 3 and change_pct >= 0.7 and close_price > open_price:
            found = {
                "impulse_time": str(candle.get("t", idx)),
                "impulse_low": float(candle["l"]),
                "impulse_change": change_pct,
                "impulse_ratio": ratio,
            }
    return found


def check_derivatives_data(tickers):
    """Log a lightweight health check for the public funding and OI fields."""
    btc_ticker = next((item for item in tickers if item.get("contract") == "BTC_USDT"), {})
    funding_available = btc_ticker.get("funding_rate") not in (None, "")
    try:
        stats = get_json(
            "/futures/usdt/contract_stats",
            {
                "contract": "BTC_USDT",
                "from": int(time.time()) - 900,
                "interval": "5m",
                "limit": 3,
            },
        )
        oi_rows = [row for row in stats if row.get("open_interest") not in (None, "")]
        print(
            "Data-source check: BTC funding_rate_available="
            f"{funding_available}, OI_records={len(oi_rows)}"
        )
    except Exception as error:
        print(f"Data-source check failed for BTC OI: {type(error).__name__}: {error}")




BYBIT_BASE = "https://api.bybit.com"


def bybit_get_json(path, params=None):
    response = SESSION.get(BYBIT_BASE + path, params=params, timeout=15)
    response.raise_for_status()
    payload = response.json()
    if payload.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error: {payload.get('retMsg', payload)}")
    return payload.get("result", {})


def public_derivatives_check(contract):
    """
    Validate the setup using Bybit's public linear-perpetual OI history and
    funding endpoint. No API key is required. Gate.io remains the primary
    price/candle source and its OI/funding are independently checked as well.
    """
    base_asset = contract[:-5] if contract.endswith("_USDT") else contract
    symbol = base_asset + "USDT"
    try:
        oi_result = bybit_get_json(
            "/v5/market/open-interest",
            {"category": "linear", "symbol": symbol, "intervalTime": "5min", "limit": 3},
        )
        rows = oi_result.get("list", [])
        rows = sorted(rows, key=lambda row: int(row.get("timestamp", 0)))
        rows = [row for row in rows if row.get("openInterest") not in (None, "")]
        if len(rows) < 2:
            return {"valid": False, "reason": "تاریخچه OI عمومی Bybit کافی نیست"}

        old_oi = float(rows[-2]["openInterest"])
        new_oi = float(rows[-1]["openInterest"])
        if old_oi <= 0:
            return {"valid": False, "reason": "مقدار OI عمومی Bybit نامعتبر است"}
        oi_change = (new_oi / old_oi - 1) * 100

        # Ticker returns the current/next funding estimate, unlike funding history
        # which may only show the last settled rate from several hours ago.
        ticker_result = bybit_get_json(
            "/v5/market/tickers",
            {"category": "linear", "symbol": symbol},
        )
        ticker_rows = ticker_result.get("list", [])
        if not ticker_rows or ticker_rows[0].get("fundingRate") in (None, ""):
            return {"valid": False, "reason": "Funding فعلی عمومی Bybit در دسترس نیست"}
        funding_rate = float(ticker_rows[0]["fundingRate"])
        return {
            "valid": True,
            "oi_change": oi_change,
            "funding_rate": funding_rate,
            "symbol": symbol,
        }
    except Exception as error:
        print(f"{contract}: public Bybit derivatives check failed: {type(error).__name__}: {error}")
        return {"valid": False, "reason": "داده عمومی OI/Funding از Bybit دریافت نشد"}

def scan():
    tickers = get_json("/futures/usdt/tickers")
    check_derivatives_data(tickers)
    # Probe Bybit once per scan so logs show source availability even when no
    # coin reaches the final confirmation stage.
    try:
        bybit_health = public_derivatives_check("BTC_USDT")
        if bybit_health.get("valid"):
            print(
                "Bybit public-source check: OK; BTC OI change="
                f"{float(bybit_health['oi_change']):+.3f}%, "
                f"current funding={float(bybit_health['funding_rate']) * 100:+.4f}%"
            )
        else:
            print(f"Bybit public-source check: unavailable ({bybit_health.get('reason', 'unknown')})")
    except Exception as error:
        print(f"Bybit public-source check failed: {type(error).__name__}: {error}")
    liquid = []
    print(f"Gate.io tickers received: {len(tickers)}")
    for item in tickers:
        contract = item.get("contract", "")
        turnover24 = float(item.get("volume_24h_quote") or item.get("volume_24h_usd") or item.get("volume_24h") or 0)
        if contract.endswith("_USDT") and turnover24 >= 1_000_000:
            liquid.append((item, turnover24))
    coins = sorted(liquid, key=lambda pair: pair[1], reverse=True)[:100]
    print(f"Gate.io liquid contracts checked: {len(coins)}")

    state = load_alert_state()
    alerts_state = state["alerts"]
    candidates = state["candidates"]
    early_alerts_state = state["early_alerts"]

    btc_ok, btc_change = btc_market_filter()
    if not btc_ok:
        print("BTC filter: paused confirmed entry alerts due to a sharply bearish 15m candle.")
        save_alert_state(state)
        return

    alerts = 0
    candle_checks = 0
    recent_impulses = 0
    bos_checks = 0
    confirms15 = 0
    confirms1h = 0
    oi_checks = 0
    funding_checks = 0
    public_derivatives_checks = 0
    now = int(time.time())

    for ticker, _turnover24h in coins:
        contract = ticker["contract"]
        try:
            candles = closed_candles(contract, "5m", 40)
            if len(candles) < 24:
                continue
            candle_checks += 1
            latest = candles[-1]
            latest_time = int(latest.get("t", now))
            latest_open = float(latest["o"])
            latest_close = float(latest["c"])

            impulse = find_recent_impulse(candles)
            candidate = candidates.get(contract)

            # Track a fresh impulse across runs; preserve any BOS already observed
            # when the same impulse is seen again.
            if impulse:
                recent_impulses += 1
                # Do not reset an active setup whenever a newer qualifying impulse
                # appears in the rolling candle window. That can repeatedly erase
                # the BOS/confirmation progress and leave the bot sending only
                # early warnings. Track the first active setup until it confirms,
                # invalidates, or expires.
                if not candidate:
                    candidate = {
                        **impulse,
                        "bos_level": None,
                        "bos_time": None,
                        "pullback_low": impulse["impulse_low"],
                    }
                    candidates[contract] = candidate

            if not candidate:
                continue

            # Send one early heads-up per impulse, then keep scanning for full confirmation.
            if early_alerts_state.get(contract) != candidate["impulse_time"]:
                early_message = (
                    f"👀 هشدار زودهنگام؛ هنوز تأیید نشده: {contract.replace('_', '/')} (Gate.io Perpetual)\n"
                    f"حرکت اولیه: {float(candidate['impulse_change']):+.2f}%؛ حجم {float(candidate['impulse_ratio']):.1f} برابر میانگین ۲۰ کندل قبلی\n"
                    f"قیمت فعلیِ کندل بسته‌شده ۵دقیقه: {latest_close}\n"
                    f"کف حرکت اولیه: {float(candidate['impulse_low'])}\n\n"
                    "هنوز سیگنال ورود نیست. ربات منتظر شکست ساختار ۵دقیقه، تأیید ۱۵دقیقه و روند ۱ساعته، سپس بررسی OI و Funding می‌ماند."
                )
                send_message(early_message)
                early_alerts_state[contract] = candidate["impulse_time"]
                print(f"Early warning sent: {contract}")

            try:
                impulse_time = int(candidate["impulse_time"])
            except (TypeError, ValueError):
                impulse_time = latest_time
            # Expire unconfirmed setups after one hour.
            if latest_time - impulse_time > 3600:
                candidates.pop(contract, None)
                continue

            # Find the impulse candle in the recent candle window and validate its low.
            impulse_idx = next((i for i, candle in enumerate(candles)
                                if str(candle.get("t", i)) == str(candidate["impulse_time"])), None)
            if impulse_idx is None:
                continue
            post_impulse = candles[impulse_idx + 1:]
            if not post_impulse:
                continue
            lowest_after_impulse = min(float(item["l"]) for item in post_impulse)
            if lowest_after_impulse <= float(candidate["impulse_low"]):
                print(f"{contract}: setup invalidated; price broke the impulse low")
                candidates.pop(contract, None)
                continue
            candidate["pullback_low"] = min(float(candidate.get("pullback_low", candidate["impulse_low"])),
                                              lowest_after_impulse)

            # Detect and remember a closed-candle break above the previous 3-candle high.
            previous_three = candles[-4:-1]
            if len(previous_three) == 3 and latest_close > latest_open:
                short_term_high = max(float(item["h"]) for item in previous_three)
                if latest_close > short_term_high:
                    candidate["bos_level"] = short_term_high
                    candidate["bos_time"] = str(latest.get("t", ""))
                    print(f"{contract}: 5m break of structure detected at {short_term_high}")

            # If BOS occurred earlier, wait for confirmation while price remains above its level.
            bos_level = candidate.get("bos_level")
            if bos_level is None or latest_close <= float(bos_level):
                continue
            bos_checks += 1

            confirmed15, change15 = confirms_15m(contract)
            if not confirmed15:
                print(f"{contract}: BOS seen; waiting for closed 15m bullish confirmation")
                continue
            confirms15 += 1

            confirmed1h, change1h, _ema20 = confirms_1h(contract)
            if not confirmed1h:
                print(f"{contract}: 15m confirmed; waiting for price above rising 1h EMA20")
                continue
            confirms1h += 1

            oi_rows = get_json(
                "/futures/usdt/contract_stats",
                {"contract": contract, "from": now - 900, "interval": "5m", "limit": 3},
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

            # Funding must be available and not excessively positive; very high positive
            # funding can indicate a crowded long trade. Gate.io returns this as a ratio.
            raw_funding = ticker.get("funding_rate")
            try:
                funding_rate = float(raw_funding) if raw_funding not in (None, "") else None
            except (TypeError, ValueError):
                funding_rate = None
            if funding_rate is None:
                print(f"{contract}: skipped; funding rate unavailable")
                continue
            funding_checks += 1
            if funding_rate >= 0.0005:  # 0.05% funding rate
                print(f"{contract}: skipped; funding is crowded at {funding_rate * 100:.4f}%")
                continue

            # Use Bybit as an additional public cross-check when reachable.
            # Some hosted runners receive HTTP 403 from Bybit; that must not
            # permanently disable the bot because Gate.io OI/funding are already validated.
            public_derivatives_checks += 1
            cross_check = public_derivatives_check(contract)
            derivatives_text = (
                f"OI گیت: {oi_change:+.2f}% | Funding گیت: {funding_rate * 100:+.4f}%"
            )
            if cross_check.get("valid"):
                public_oi_change = float(cross_check["oi_change"])
                public_funding = float(cross_check["funding_rate"])
                if public_oi_change < 0.2:
                    print(f"{contract}: Bybit OI change {public_oi_change:+.3f}% below +0.2%; keeping Gate.io-only validation")
                elif public_funding >= 0.0005:
                    print(f"{contract}: Bybit funding crowded at {public_funding * 100:.4f}%; keeping Gate.io-only validation")
                else:
                    derivatives_text += (
                        f"\nتأیید مستقل Bybit ({cross_check['symbol']}): "
                        f"OI {public_oi_change:+.2f}% | Funding {public_funding * 100:+.4f}%"
                    )
            else:
                print(
                    f"{contract}: Bybit cross-check unavailable; using validated Gate.io OI/funding "
                    f"({cross_check.get('reason', 'داده نامعتبر')})"
                )
                derivatives_text += "\nBybit: در دسترس نبود؛ بررسی OI/Funding بر اساس Gate.io انجام شد"

            # One final alert per impulse candle.
            if alerts_state.get(contract) == candidate["impulse_time"]:
                print(f"{contract}: final alert already sent for this impulse")
                candidates.pop(contract, None)
                continue

            entry = latest_close
            stop = float(candidate["pullback_low"])
            risk = entry - stop
            if risk <= 0:
                print(f"{contract}: invalid entry/stop distance")
                continue
            tp1 = entry + risk
            tp2 = entry + 2 * risk
            btc_text = f"{btc_change:+.2f}%" if btc_change is not None else "نامشخص"
            change1h_text = f"{change1h:+.2f}%" if change1h is not None else "نامشخص"
            funding_pct = funding_rate * 100
            funding_text = f"{funding_pct:+.4f}%" if funding_pct is not None else "نامشخص"

            message = (
                f"✅ تأیید نهایی ستاپ صعودی: {contract.replace('_', '/')} (Gate.io Perpetual)\n"
                f"شکست ساختار ۵دقیقه‌ای ثبت شده و قیمت بالای سطح شکست است\n"
                f"کندل ۱۵دقیقه: تأیید صعودی ({change15:+.2f}%)\n"
                f"روند ۱ساعته: قیمت بالای EMA20 صعودی ({change1h_text})\n"
                f"حرکت اولیه: {float(candidate['impulse_change']):+.2f}%؛ حجم {float(candidate['impulse_ratio']):.1f} برابر میانگین\n"
                f"{derivatives_text}\n"
                f"BTC در ۱۵دقیقه: {btc_text}\n\n"
                f"ورود پیشنهادی: {entry}\n"
                f"حد ضرر پیشنهادی زیر کف پول‌بک: {stop}\n"
                f"هدف ۱ (ریسک‌به‌بازده ۱:۱): {tp1}\n"
                f"هدف ۲ (ریسک‌به‌بازده ۱:۲): {tp2}\n"
                "⚠️ این هشدار تأیید الگوریتمی است، نه تضمین سود؛ قبل از ورود نقدشوندگی، اسپرد و ریسک را بررسی کن."
            )
            send_message(message)
            alerts_state[contract] = candidate["impulse_time"]
            candidates.pop(contract, None)
            alerts += 1
            print(f"Confirmed entry alert sent: {contract}")
            time.sleep(1)
        except Exception as error:
            print(f"{contract}: {type(error).__name__}: {error}")

    save_alert_state(state)
    print(
        f"Diagnostics: candle_checks={candle_checks}, recent_impulses={recent_impulses}, "
        f"bos_confirmations={bos_checks}, confirmations_15m={confirms15}, "
        f"confirmations_1h={confirms1h}, oi_checks={oi_checks}, funding_checks={funding_checks}, "
        f"public_derivatives_checks={public_derivatives_checks}"
    )
    print(f"Scan finished. Confirmed entry alerts sent: {alerts}")


if __name__ == "__main__":
    if not TOKEN or not CHAT_ID:
        raise SystemExit("BOT_TOKEN یا CHAT_ID در GitHub Secrets تنظیم نشده است.")
    scan()
