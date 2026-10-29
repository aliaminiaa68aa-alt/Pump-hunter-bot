import os
import time
import requests

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BASE = "https://api.gateio.ws/api/v4"

# Explicitly exclude tokenized equities, equity indices, ETFs and common stock symbols.
# The scanner is for crypto assets only, even if a non-crypto perpetual is quoted in USDT.
NON_CRYPTO_BASES = {
    "AAPL", "AMZN", "AMD", "BA", "BABA", "COIN", "COST", "DIS", "GOOG", "GOOGL",
    "HOOD", "INTC", "JNJ", "JPM", "META", "MSFT", "MSTR", "NFLX", "NVDA", "ORCL",
    "PLTR", "TSLA", "TSM", "WMT", "XOM",
    "DIA", "IWM", "QQQ", "SPY", "SPX", "NDX",
    "GLD", "SLV", "USO",
}


def is_crypto_contract(contract):
    """Accept USDT-quoted contracts except known equity/index/ETF/commodity products."""
    if not isinstance(contract, str) or not contract.endswith("_USDT"):
        return False
    base = contract[:-5].upper()
    if not base or base in NON_CRYPTO_BASES:
        return False
    return True

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
    data.setdefault("fib_candidates", {})
    data.setdefault("fib_alerts", {})
    data.setdefault("fib_early_alerts", {})
    data.setdefault("early_any", {})
    data.setdefault("setup_alerts", {})
    data.setdefault("market_cap_cache", {})
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
    last_idx = len(candles) - 1
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



COINALYZE_BASE = "https://api.coinalyze.net/v1"
COINALYZE_API_KEY = os.getenv("COINALYZE_API_KEY", "").strip()


def coinalyze_request(path, params=None):
    """Authenticated Coinalyze request. API key is stored only in GitHub Actions Secrets."""
    if not COINALYZE_API_KEY:
        raise RuntimeError("COINALYZE_API_KEY secret is not configured")
    response = SESSION.get(
        COINALYZE_BASE + path,
        params=params,
        headers={"api_key": COINALYZE_API_KEY},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected Coinalyze response for {path}")
    return payload


def coinalyze_derivatives_check(contract, state):
    """
    Cross-check a potential regular-strategy entry with Coinalyze data from
    multiple USDT-margined perpetual exchanges. If the API key is not configured,
    preserve the existing Gate.io validation. If configured, fail closed on
    missing/weak Coinalyze OI or funding confirmation.
    """
    if not COINALYZE_API_KEY:
        return {"configured": False, "valid": False, "reason": "COINALYZE_API_KEY secret is not configured"}

    try:
        now = int(time.time())
        cache = state.get("coinalyze_market_cache", {})
        if not cache.get("fetched_at") or now - int(cache["fetched_at"]) >= 21600:
            markets = coinalyze_request("/future-markets")
            state["coinalyze_market_cache"] = {"fetched_at": now, "markets": markets}
        else:
            markets = cache.get("markets", [])

        exchange_cache = state.get("coinalyze_exchange_cache", {})
        if not exchange_cache.get("fetched_at") or now - int(exchange_cache["fetched_at"]) >= 21600:
            exchange_rows = coinalyze_request("/exchanges")
            exchange_names = {
                str(item.get("code", "")).upper(): str(item.get("name", item.get("code", "unknown")))
                for item in exchange_rows if item.get("code")
            }
            state["coinalyze_exchange_cache"] = {"fetched_at": now, "names": exchange_names}
        else:
            exchange_names = exchange_cache.get("names", {})

        def exchange_name(row):
            raw = str(row.get("exchange", "unknown"))
            return exchange_names.get(raw.upper(), raw)

        base = contract[:-5].upper() if contract.endswith("_USDT") else contract.upper()
        eligible = [
            row for row in markets
            if str(row.get("base_asset", "")).upper() == base
            and str(row.get("quote_asset", "")).upper() == "USDT"
            and row.get("is_perpetual") is True
            and str(row.get("margined", "")).upper() == "STABLE"
            and row.get("symbol")
        ]
        priority = {"BINANCE": 0, "BYBIT": 1, "OKX": 2, "GATEIO": 3, "GATE.IO": 3}
        eligible.sort(key=lambda row: (priority.get(exchange_name(row).upper().replace(" ", ""), 10),
                                       exchange_name(row).upper()))
        selected = []
        seen = set()
        for row in eligible:
            symbol = str(row["symbol"])
            if symbol not in seen:
                selected.append(row)
                seen.add(symbol)
            if len(selected) >= 3:
                break
        if len(selected) < 2:
            return {"configured": True, "valid": False, "reason": "Coinalyze کمتر از دو بازار پرپچوال USDT برای این ارز دارد"}

        symbols = ",".join(str(row["symbol"]) for row in selected)
        oi_rows = coinalyze_request(
            "/open-interest-history",
            {"symbols": symbols, "interval": "5min", "from": now - 1800, "to": now, "convert_to_usd": "true"},
        )
        funding_rows = coinalyze_request("/funding-rate", {"symbols": symbols})

        oi_by_symbol = {str(row.get("symbol", "")): row for row in oi_rows}
        funding_by_symbol = {str(row.get("symbol", "")): row for row in funding_rows}
        old_total = 0.0
        new_total = 0.0
        oi_exchanges = []
        funding_values = []
        funding_exchanges = []
        funding_raw_diagnostics = []
        exchange_by_symbol = {str(row["symbol"]): exchange_name(row) for row in selected}
        for symbol in seen:
            oi_row = oi_by_symbol.get(symbol, {})
            history = oi_row.get("history", [])
            history = sorted(history, key=lambda point: int(point.get("t", 0)))
            if len(history) >= 2:
                old_value = float(history[-2].get("c", 0) or 0)
                new_value = float(history[-1].get("c", 0) or 0)
                if old_value > 0 and new_value > 0:
                    old_total += old_value
                    new_total += new_value
                    oi_exchanges.append(exchange_by_symbol.get(symbol, symbol))
            funding_row = funding_by_symbol.get(symbol)
            if funding_row and funding_row.get("value") not in (None, ""):
                raw_rate = float(funding_row["value"])
                funding_values.append(raw_rate)
                exchange = exchange_by_symbol.get(symbol, symbol)
                funding_exchanges.append(exchange)
                # Diagnostic only: record the exact API numeric scale per exchange.
                funding_raw_diagnostics.append(f"{exchange}={raw_rate:+.8f}")

        if len(oi_exchanges) < 2 or len(funding_values) < 2 or old_total <= 0:
            return {"configured": True, "valid": False, "reason": "داده هم‌زمان OI/Funding از حداقل دو صرافی Coinalyze کامل نیست"}

        oi_change = (new_total / old_total - 1.0) * 100.0
        # Coinalyze returns funding values in percentage units (e.g. -0.0243 means
        # -0.0243%, not -2.43%). Normalize to a decimal fraction internally so the
        # rest of this bot can keep using fraction-based rates (0.0001 = 0.01%).
        extreme_funding = [
            f"{exchange_by_symbol.get(symbol, symbol)}={float(row['value']):+.8f}"
            for symbol, row in funding_by_symbol.items()
            if row.get("value") not in (None, "") and abs(float(row["value"])) > 1.0
        ]
        if extreme_funding:
            print(
                f"{contract}: Coinalyze funding rejected as implausible (>1% per interval); "
                f"raw_percent_values=[{','.join(extreme_funding)}]"
            )
            return {
                "configured": True, "valid": False,
                "reason": "Coinalyze funding magnitude failed sanity check",
            }
        funding_avg = sum(funding_values) / len(funding_values) / 100.0
        # Safe operational diagnostics: report data coverage and exchange names,
        # never the API key or request headers.
        print(
            f"{contract}: Coinalyze data OK; selected={len(selected)} "
            f"OI_history={len(oi_exchanges)}[{','.join(oi_exchanges)}] "
            f"funding={len(funding_values)}[{','.join(funding_exchanges)}] "
            f"aggregate_OI_change={oi_change:+.3f}% "
            f"aggregate_OI_USD={new_total:.0f} average_funding={funding_avg*100:+.4f}% "
            f"funding_raw_values=[{','.join(funding_raw_diagnostics)}]"
        )
        return {
            "configured": True, "valid": True,
            "oi_change": oi_change, "funding_rate": funding_avg,
            "oi_usd": new_total,
            "oi_exchanges": oi_exchanges, "funding_exchanges": funding_exchanges,
            "symbol": base + "USDT",
        }
    except Exception as error:
        print(f"{contract}: Coinalyze cross-check failed: {type(error).__name__}: {error}")
        return {"configured": True, "valid": False, "reason": "داده Coinalyze در دسترس نیست"}

OKX_BASE = "https://www.okx.com"


def okx_ratio_to_long_pct(value):
    ratio = float(value)
    if ratio < 0:
        raise RuntimeError("Negative OKX long/short ratio")
    return ratio / (1.0 + ratio) * 100.0


def okx_contract_long_ratio(contract, period):
    """Read per-contract OKX long/short account ratio and convert to long share."""
    base_asset = contract[:-5] if contract.endswith("_USDT") else contract
    inst_id = f"{base_asset}-USDT-SWAP"
    response = SESSION.get(
        OKX_BASE + "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract",
        params={"instId": inst_id, "period": period, "limit": 1},
        timeout=12,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        rows = payload
    else:
        if payload.get("code") != "0":
            raise RuntimeError(f"OKX API error: {payload.get('msg', payload)}")
        rows = payload.get("data", [])
    if not rows:
        raise RuntimeError(f"No OKX long/short data for {inst_id}/{period}")
    row = rows[-1]
    raw_ratio = row[-1] if isinstance(row, list) and row else row.get("longShortRatio")
    if raw_ratio in (None, ""):
        raise RuntimeError(f"Missing ratio for {inst_id}/{period}")
    return okx_ratio_to_long_pct(raw_ratio)


def okx_currency_daily_long_ratio(contract):
    """Daily account ratio by currency, as OKX's contract endpoint lacks daily granularity."""
    base_asset = contract[:-5] if contract.endswith("_USDT") else contract
    response = SESSION.get(
        OKX_BASE + "/api/v5/rubik/stat/contracts/long-short-account-ratio",
        params={"ccy": base_asset, "period": "1D", "limit": 1},
        timeout=12,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        rows = payload
    else:
        if payload.get("code") != "0":
            raise RuntimeError(f"OKX API error: {payload.get('msg', payload)}")
        rows = payload.get("data", [])
    if not rows:
        raise RuntimeError(f"No OKX daily long/short data for {base_asset}")
    row = rows[-1]
    raw_ratio = row[-1] if isinstance(row, list) and row else row.get("longShortRatio")
    if raw_ratio in (None, ""):
        raise RuntimeError(f"Missing daily ratio for {base_asset}")
    return okx_ratio_to_long_pct(raw_ratio)


def coinalyze_spike_metrics(contract, ticker, market_cap, market_volume, state):
    """Fetch Coinalyze derivatives metrics for confirmed LEG SPIKE alerts."""
    result = {"coin": contract.replace("_", "/"), "price": ticker.get("last"),
              "change24": ticker.get("change_percentage"), "market_cap": market_cap,
              "market_volume": market_volume, "oi_usd": None, "oi_change24": None,
              "oi_change4h": None, "oi_volume24": None, "funding_avg": None,
              "liquidations24": None, "long1h": None, "short1h": None,
              "long1d": None, "short1d": None}
    if not COINALYZE_API_KEY:
        return result
    try:
        now = int(time.time())
        cache = state.get("coinalyze_market_cache", {})
        if not cache.get("fetched_at") or now - int(cache["fetched_at"]) >= 21600:
            markets = coinalyze_request("/future-markets")
            state["coinalyze_market_cache"] = {"fetched_at": now, "markets": markets}
        else:
            markets = cache.get("markets", [])
        base = contract[:-5].upper() if contract.endswith("_USDT") else contract.upper()
        eligible = [row for row in markets
                    if str(row.get("base_asset", "")).upper() == base
                    and str(row.get("quote_asset", "")).upper() == "USDT"
                    and row.get("is_perpetual") is True
                    and str(row.get("margined", "")).upper() == "STABLE" and row.get("symbol")]
        names = state.get("coinalyze_exchange_cache", {}).get("names", {})
        def exname(row):
            raw = str(row.get("exchange", "unknown"))
            return str(names.get(raw.upper(), raw)).upper()
        priority = {"BINANCE": 0, "BYBIT": 1, "OKX": 2, "GATEIO": 3, "GATE.IO": 3}
        eligible.sort(key=lambda row: (priority.get(exname(row).replace(" ", ""), 10), exname(row)))
        selected, seen = [], set()
        for row in eligible:
            symbol = str(row["symbol"])
            if symbol not in seen:
                selected.append(row)
                seen.add(symbol)
            if len(selected) == 3:
                break
        if not selected:
            return result
        symbols = ",".join(str(row["symbol"]) for row in selected)
        current = coinalyze_request("/open-interest", {"symbols": symbols, "convert_to_usd": "true"})
        values = [float(row["value"]) for row in current
                  if row.get("value") not in (None, "") and float(row["value"]) > 0]
        if values:
            result["oi_usd"] = sum(values)
        funding_rows = coinalyze_request("/funding-rate", {"symbols": symbols})
        funding_values = [float(row["value"]) for row in funding_rows if row.get("value") not in (None, "")]
        if funding_values:
            # Coinalyze funding values are in percentage units; normalize to fraction.
            result["funding_avg"] = (sum(funding_values) / len(funding_values)) / 100.0

        oi_rows = coinalyze_request("/open-interest-history", {
            "symbols": symbols, "interval": "1hour", "from": now - 86400, "to": now,
            "convert_to_usd": "true"})
        first_total = last_total = old4_total = new4_total = 0.0
        nfirst = nlast = n4 = 0
        for row in oi_rows:
            hist = sorted(row.get("history", []), key=lambda p: int(p.get("t", 0)))
            if len(hist) < 2:
                continue
            first, last = float(hist[0].get("c", 0) or 0), float(hist[-1].get("c", 0) or 0)
            if first > 0 and last > 0:
                first_total += first; last_total += last; nfirst += 1; nlast += 1
            recent = [p for p in hist if int(p.get("t", 0)) >= now - 4 * 3600]
            if len(recent) >= 2:
                old4, new4 = float(recent[0].get("c", 0) or 0), float(recent[-1].get("c", 0) or 0)
                if old4 > 0 and new4 > 0:
                    old4_total += old4; new4_total += new4; n4 += 1
        if nfirst and nlast and first_total > 0:
            result["oi_change24"] = (last_total / first_total - 1) * 100
        if n4 and old4_total > 0:
            result["oi_change4h"] = (new4_total / old4_total - 1) * 100

        liq_rows = coinalyze_request("/liquidation-history", {
            "symbols": symbols, "interval": "1hour", "from": now - 86400, "to": now,
            "convert_to_usd": "true"})
        liq_sum, liq_found = 0.0, False
        for row in liq_rows:
            for point in row.get("history", []):
                for key in ("l", "s"):
                    if point.get(key) not in (None, ""):
                        liq_sum += float(point[key]); liq_found = True
        if liq_found:
            result["liquidations24"] = liq_sum

        for label, interval, since in (("1h", "1hour", now - 2 * 3600),
                                       ("1d", "daily", now - 2 * 86400)):
            try:
                ratio_rows = coinalyze_request("/long-short-ratio-history", {
                    "symbols": symbols, "interval": interval, "from": since, "to": now})
                longs, shorts = [], []
                for row in ratio_rows:
                    hist = sorted(row.get("history", []), key=lambda p: int(p.get("t", 0)))
                    if not hist:
                        continue
                    point = hist[-1]
                    if point.get("l") not in (None, "") and point.get("s") not in (None, ""):
                        lv, sv = float(point["l"]), float(point["s"])
                        if lv <= 1 and sv <= 1:
                            lv *= 100; sv *= 100
                        longs.append(lv); shorts.append(sv)
                    elif point.get("r") not in (None, ""):
                        ratio = float(point["r"])
                        if ratio >= 0:
                            longs.append(ratio / (1 + ratio) * 100)
                            shorts.append(100 / (1 + ratio))
                if longs and shorts:
                    result["long" + label] = sum(longs) / len(longs)
                    result["short" + label] = sum(shorts) / len(shorts)
            except Exception as error:
                print(f"{contract}: Coinalyze long/short {label} unavailable: {type(error).__name__}")

        # Futures OHLCV volume is converted from base units to USD using each candle close.
        try:
            vol_rows = coinalyze_request("/ohlcv-history", {
                "symbols": symbols, "interval": "1hour", "from": now - 86400, "to": now})
            total_usd, found = 0.0, False
            for row in vol_rows:
                for point in row.get("history", []):
                    vol, close = float(point.get("v", 0) or 0), float(point.get("c", 0) or 0)
                    if vol > 0 and close > 0:
                        total_usd += vol * close; found = True
            if found:
                result["oi_volume24"] = total_usd
        except Exception as error:
            print(f"{contract}: Coinalyze futures volume unavailable: {type(error).__name__}")
    except Exception as error:
        print(f"{contract}: Coinalyze spike metrics unavailable: {type(error).__name__}: {error}")
    return result


def spike_metric_text(metrics):
    def money(value):
        if value in (None, ""): return "ناموجود"
        value = float(value)
        if abs(value) >= 1_000_000_000: return "$" + format(value / 1_000_000_000, ".2f") + "B"
        if abs(value) >= 1_000_000: return "$" + format(value / 1_000_000, ".2f") + "M"
        if abs(value) >= 1_000: return "$" + format(value / 1_000, ".2f") + "K"
        return "$" + format(value, ".2f")
    def pct(value, decimals=2):
        return "ناموجود" if value in (None, "") else format(float(value), "+." + str(decimals) + "f") + "%"
    def ratio(numerator, denominator):
        if numerator in (None, "") or denominator in (None, "") or float(denominator) <= 0:
            return "ناموجود"
        return format(float(numerator) / float(denominator) * 100, ".1f") + "%"
    return (
        f"⚪️ Coin: {metrics.get('coin', 'نامشخص')}\n"
        f"⚪️ Price: {metrics.get('price') if metrics.get('price') not in (None, '') else 'ناموجود'}\n"
        f"⚪️ Chg 24h: {pct(metrics.get('change24'))}\n"
        f"⚪️ Mkt Cap: {money(metrics.get('market_cap'))}\n"
        f"⚪️ Vol 24h: {money(metrics.get('market_volume'))}\n"
        f"⚪️ Open Interest: {money(metrics.get('oi_usd'))}\n"
        f"⚪️ OI Chg 24h %: {pct(metrics.get('oi_change24'))}\n"
        f"⚪️ OI Chg 4h %: {pct(metrics.get('oi_change4h'))}\n"
        f"⚪️ OI / Mkt Cap: {ratio(metrics.get('oi_usd'), metrics.get('market_cap'))}\n"
        f"⚪️ OI Vol 24h: {money(metrics.get('oi_volume24'))}\n"
        f"⚪️ Vol 24h / Mkt Cap: {ratio(metrics.get('market_volume'), metrics.get('market_cap'))}\n"
        f"⚪️ FR Avg: {pct(None if metrics.get('funding_avg') is None else float(metrics['funding_avg']) * 100, 4)}\n"
        f"⚪️ Liqs 24h: {money(metrics.get('liquidations24'))}\n"
        f"⚪️ Long 1h: {pct(metrics.get('long1h'))} | Short 1h: {pct(metrics.get('short1h'))}\n"
        f"⚪️ Long 1d: {pct(metrics.get('long1d'))} | Short 1d: {pct(metrics.get('short1d'))}"
    )


def leg_spike_long_short_filter(contract):
    """
    LEG SPIKE crowding filter using public OKX account-ratio data:
    15m long share >50%, 1h >50%, daily <=70%.
    Fail closed if required data is unavailable.
    """
    try:
        ratio_15m = okx_contract_long_ratio(contract, "15m")
        ratio_1h = okx_contract_long_ratio(contract, "1H")
        ratio_1d = okx_currency_daily_long_ratio(contract)
        passed = ratio_15m > 50.0 and ratio_1h >= 60.0 and ratio_1d <= 70.0
        return {
            "valid": True, "passed": passed,
            "ratio_15m": ratio_15m, "ratio_1h": ratio_1h, "ratio_1d": ratio_1d,
            "symbol": (contract[:-5] if contract.endswith("_USDT") else contract) + "USDT",
        }
    except Exception as error:
        print(f"{contract}: OKX long/short ratio unavailable: {type(error).__name__}: {error}")
        return {"valid": False, "passed": False, "reason": "داده نسبت لانگ/شورت OKX در دسترس نیست"}


# Fibonacci pullback strategy configuration.
# "Several times market cap" is set to >=2x by default and can be tuned here.
FIB_MIN_VOLUME_TO_MARKET_CAP = 0.50
FIB_MIN_OI_TO_MARKET_CAP = 0.20
FIB_MIN_BODY_PCT = 0.8
FIB_MIN_TOTAL_PUMP_PCT = 4.0
COINGECKO_BASE = "https://api.coingecko.com/api/v3"


def get_market_caps(state):
    """Cache CoinGecko market caps for 15 minutes; exclude ambiguous duplicate symbols."""
    cache = state.get("market_cap_cache", {})
    now = int(time.time())
    if (cache.get("fetched_at") and now - int(cache["fetched_at"]) < 900
            and isinstance(cache.get("volumes"), dict)):
        return cache.get("caps", {}), cache.get("ambiguous", [])
    all_rows = []
    try:
        # Four pages cover the first 1,000 assets by market cap; avoid a request per coin.
        for page in range(1, 5):
            response = SESSION.get(
                f"{COINGECKO_BASE}/coins/markets",
                params={"vs_currency": "usd", "order": "market_cap_desc",
                        "per_page": 250, "page": page, "sparkline": "false"},
                timeout=20,
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise RuntimeError("CoinGecko market-cap response was not a list")
            all_rows.extend(rows)
            time.sleep(0.25)
        by_symbol = {}
        volume_by_symbol = {}
        ambiguous = set()
        for row in all_rows:
            symbol = str(row.get("symbol", "")).upper()
            cap = row.get("market_cap")
            total_volume = row.get("total_volume")
            if not symbol or not cap or float(cap) <= 0:
                continue
            if symbol in by_symbol:
                ambiguous.add(symbol)
            else:
                by_symbol[symbol] = float(cap)
                if total_volume not in (None, "") and float(total_volume) > 0:
                    volume_by_symbol[symbol] = float(total_volume)
        for symbol in ambiguous:
            by_symbol.pop(symbol, None)
            volume_by_symbol.pop(symbol, None)
        state["market_cap_cache"] = {
            "fetched_at": now, "caps": by_symbol, "volumes": volume_by_symbol,
            "ambiguous": sorted(ambiguous)
        }
        print(
            f"CoinGecko market data refreshed: {len(by_symbol)} unique market caps, "
            f"{len(volume_by_symbol)} 24h volumes; {len(ambiguous)} ambiguous symbols"
        )
        return by_symbol, sorted(ambiguous)
    except Exception as error:
        print(f"CoinGecko market-cap lookup unavailable: {type(error).__name__}: {error}")
        # Use a still-existing cache if available; otherwise fail closed for this filter.
        return cache.get("caps", {}), cache.get("ambiguous", [])


def detect_fib_pump(candles):
    """Find a 2- or 3-candle bullish impulse with large bodies among recent closed 5m candles."""
    if len(candles) < 10:
        return None
    # Prefer the strongest recent sequence, but only start a setup if it is still recent.
    start_min = max(0, len(candles) - 7)
    best = None
    for end in range(len(candles) - 1, start_min + 1, -1):
        for count in (3, 2):
            start = end - count + 1
            if start < start_min:
                continue
            seq = candles[start:end + 1]
            bodies = []
            valid = True
            for candle in seq:
                op = float(candle["o"])
                cl = float(candle["c"])
                body_pct = (cl / op - 1) * 100 if op else 0
                if cl <= op or body_pct < FIB_MIN_BODY_PCT:
                    valid = False
                    break
                bodies.append(body_pct)
            if not valid:
                continue
            low = min(float(x["l"]) for x in seq)
            high = max(float(x["h"]) for x in seq)
            total_pct = (high / low - 1) * 100 if low else 0
            if total_pct < FIB_MIN_TOTAL_PUMP_PCT or high <= low:
                continue
            # Candidate must have its latest impulse candle among the last 3 closed candles.
            if end < len(candles) - 3:
                continue
            best = {
                "start_time": str(seq[0].get("t", start)),
                "end_time": str(seq[-1].get("t", end)),
                "low": low, "high": high, "pump_pct": total_pct,
                "body_count": count,
                "fib50": high - (high - low) * 0.50,
                "fib66": high - (high - low) * 0.66,
            }
            return best
    return best


def scan_fib_pullbacks(tickers, state, confirmed_entries=True):
    """Scan LEG SPIKE with staged diagnostics and avoid ratio API calls before a real pump exists."""
    caps, ambiguous = get_market_caps(state)
    market_volumes = state.get("market_cap_cache", {}).get("volumes", {})
    candidates = state["fib_candidates"]
    sent = state["fib_alerts"]
    counts = {
        "tickers": 0, "negative_funding": 0, "cap_available": 0,
        "early_scan_checked": 0, "early_patterns_seen": 0, "early_warnings_sent": 0,
        "early_candle_errors": 0, "volume_pass": 0, "oi_pass": 0,
        "pump_detected": 0, "ratio_pass": 0, "ratio_rejected": 0,
        "ratio_unavailable": 0, "fib_reactions": 0,
        "missing_cap": 0, "missing_market_volume": 0,
    }
    volume_samples = []
    early_candle_cache = {}
    early_pattern_samples = []
    early_pattern_details = {}
    early_filter_status = {}

    # Early heads-up scan is deliberately independent of funding, market-cap,
    # volume/MC, OI and long/short filters. Those remain mandatory for confirmed entries.
    # Limit candle requests to the 100 most liquid USDT contracts each run.
    liquid_for_early = []
    for item in tickers:
        symbol = item.get("contract", "")
        if not is_crypto_contract(symbol):
            continue
        try:
            turnover = float(item.get("volume_24h_quote") or item.get("volume_24h_usd") or 0)
        except (TypeError, ValueError):
            continue
        liquid_for_early.append((turnover, item))
    liquid_for_early.sort(key=lambda pair: pair[0], reverse=True)

    for _turnover, item in liquid_for_early[:100]:
        contract = item.get("contract", "")
        try:
            candles = closed_candles(contract, "5m", 20)
            early_candle_cache[contract] = candles
            counts["early_scan_checked"] += 1
            early_candidate = detect_fib_pump(candles)
            if not early_candidate:
                continue
            counts["early_patterns_seen"] += 1
            early_pattern_details[contract] = early_candidate
            if len(early_pattern_samples) < 8:
                early_pattern_samples.append(
                    f"{contract}:{early_candidate['pump_pct']:.2f}%/{early_candidate['body_count']}c"
                )
            early_key = str(early_candidate["end_time"])
            early_sent = state["fib_early_alerts"]
            if early_sent.get(contract) == early_key:
                continue
            # Avoid duplicate early messages across LEG SPIKE and the regular strategy.
            recent_early = state.setdefault("early_any", {})
            try:
                last_early_time = int(recent_early.get(contract, 0))
            except (TypeError, ValueError):
                last_early_time = 0
            if int(time.time()) - last_early_time < 3600:
                early_sent[contract] = early_key
                print(f"{contract}: LEG SPIKE early alert suppressed; another strategy alerted within 1h")
                continue
            raw_funding = item.get("funding_rate")
            try:
                funding_text = f"{float(raw_funding) * 100:+.4f}%" if raw_funding not in (None, "") else "ناموجود"
            except (TypeError, ValueError):
                funding_text = "نامعتبر"
            symbol = contract[:-5].upper()
            cap = caps.get(symbol)
            total_market_volume = market_volumes.get(symbol)
            vol_mc_text = (
                f"{float(total_market_volume) / float(cap):.3f}x"
                if cap and total_market_volume and symbol not in ambiguous else "ناموجود"
            )
            early_message = (
                f"👀 هشدار اولیه LEG SPIKE؛ هنوز تأیید نشده: {contract.replace('_', '/')}\\n"
                f"حرکت شناسایی‌شده: {float(early_candidate['pump_pct']):.2f}% "
                f"در {int(early_candidate['body_count'])} کندل صعودی ۵دقیقه‌ای\\n"
                f"سقف: {float(early_candidate['high']):.10g} | کف: {float(early_candidate['low']):.10g}\\n"
                f"فیبو 0.50: {float(early_candidate['fib50']):.10g} | "
                f"فیبو 0.66: {float(early_candidate['fib66']):.10g}\\n"
                f"Funding: {funding_text} | حجم/مارکت‌کپ: {vol_mc_text}\\n"
                "این فقط هشدار حرکت اولیه است، نه ورود. برای سیگنال نهایی تمام فیلترهای Funding، حجم/مارکت‌کپ، OI، نسبت لانگ/شورت و واکنش کندل بسته‌شده لازم‌اند."
            )
            send_message(early_message)
            early_sent[contract] = early_key
            recent_early[contract] = int(time.time())
            counts["early_warnings_sent"] += 1
            print(f"LEG SPIKE early warning sent: {contract}; pump={early_candidate['pump_pct']:.2f}%")
        except Exception as error:
            counts["early_candle_errors"] += 1
            print(f"{contract}: LEG SPIKE early scan failed: {type(error).__name__}: {error}")

    if not confirmed_entries:
        print("LEG SPIKE: confirmed-entry checks paused by BTC filter; independent early scan completed.")
        print(
            "LEG SPIKE diagnostics: " +
            ", ".join(f"{key}={value}" for key, value in counts.items()) +
            f", active={len(candidates)}, caps_cached={len(caps)}, ambiguous_symbols={len(ambiguous)}"
        )
        print("LEG SPIKE early-pattern samples: " + ("; ".join(early_pattern_samples) or "none"))
        return

    for ticker in tickers:
        contract = ticker.get("contract", "")
        if not is_crypto_contract(contract):
            continue
        counts["tickers"] += 1
        try:
            # Gate documents volume_24h_quote as quote-currency turnover; USDT contracts quote in USDT.
            raw_volume = ticker.get("volume_24h_quote") or ticker.get("volume_24h_usd") or 0
            volume24 = float(raw_volume)
            raw_funding = ticker.get("funding_rate")
            if raw_funding in (None, ""):
                if contract in early_pattern_details: early_filter_status[contract] = "Funding missing"
                continue
            funding = float(raw_funding)
            if funding >= 0:
                if contract in early_pattern_details: early_filter_status[contract] = f"Funding non-negative ({funding*100:+.4f}%)"
                continue
            if contract in early_pattern_details: early_filter_status[contract] = "Funding passed; checking market cap"
            counts["negative_funding"] += 1

            symbol = contract[:-5].upper()
            market_cap = caps.get(symbol)
            if not market_cap or symbol in ambiguous:
                counts["missing_cap"] += 1
                if contract in early_pattern_details: early_filter_status[contract] = "Market cap missing/ambiguous"
                continue
            counts["cap_available"] += 1
            total_market_volume = market_volumes.get(symbol)
            if not total_market_volume or float(total_market_volume) <= 0:
                counts["missing_market_volume"] += 1
                if contract in early_pattern_details: early_filter_status[contract] = "CoinGecko 24h volume missing"
                continue
            # Use CoinGecko aggregate 24h market volume, not Gate.io-only turnover.
            total_market_volume = float(total_market_volume)
            volume_mc = total_market_volume / market_cap
            volume_samples.append((volume_mc, contract, total_market_volume, market_cap, funding))

            if volume_mc < FIB_MIN_VOLUME_TO_MARKET_CAP:
                if contract in early_pattern_details: early_filter_status[contract] = f"Volume/MC rejected ({volume_mc:.3f}x)"
                continue
            counts["volume_pass"] += 1
            if contract in early_pattern_details: early_filter_status[contract] = "Volume/MC passed; checking OI"

            # Coinalyze is the primary OI/funding source when configured. Do not
            # reject a candidate using Gate.io OI before checking the requested aggregate.
            coinalyze_check = coinalyze_derivatives_check(contract, state)
            if coinalyze_check.get("configured"):
                if not coinalyze_check.get("valid"):
                    if contract in early_pattern_details:
                        early_filter_status[contract] = "Coinalyze data unavailable"
                    continue
                aggregate_oi = float(coinalyze_check.get("oi_usd") or 0)
                aggregate_funding = float(coinalyze_check["funding_rate"])
                if aggregate_oi <= 0 or aggregate_oi / market_cap < FIB_MIN_OI_TO_MARKET_CAP:
                    if contract in early_pattern_details:
                        early_filter_status[contract] = f"Coinalyze OI/MC rejected ({aggregate_oi/market_cap:.1%})"
                    continue
                if aggregate_funding >= 0:
                    if contract in early_pattern_details:
                        early_filter_status[contract] = f"Coinalyze funding not negative ({aggregate_funding*100:+.4f}%)"
                    continue
                oi_usd = aggregate_oi
                funding = aggregate_funding
                if contract in early_pattern_details:
                    early_filter_status[contract] = "Coinalyze aggregate OI/funding passed"
            else:
                # Safe fallback only when no Coinalyze API key is configured.
                stats = get_json(
                    "/futures/usdt/contract_stats",
                    {"contract": contract, "from": int(time.time()) - 600,
                     "interval": "5m", "limit": 3},
                )
                stats = sorted(stats, key=lambda row: int(row.get("time", 0)))
                oi_usd = None
                for row in reversed(stats):
                    value = row.get("open_interest_usd")
                    if value not in (None, ""):
                        oi_usd = float(value)
                        break
                if oi_usd is None or oi_usd / market_cap < FIB_MIN_OI_TO_MARKET_CAP:
                    if contract in early_pattern_details:
                        early_filter_status[contract] = "Gate.io OI missing" if oi_usd is None else f"Gate.io OI/MC rejected ({oi_usd/market_cap:.1%})"
                    continue
            counts["oi_pass"] += 1
            if contract in early_pattern_details: early_filter_status[contract] = "OI/funding passed; checking pump"

            candles = early_candle_cache.get(contract)
            if candles is None:
                candles = closed_candles(contract, "5m", 20)
            if len(candles) < 10:
                continue
            candidate = candidates.get(contract)
            if candidate:
                try:
                    end_time = int(candidate["end_time"])
                    if int(candles[-1]["t"]) - end_time > 3600:
                        candidates.pop(contract, None)
                        candidate = None
                except (TypeError, ValueError, KeyError):
                    candidates.pop(contract, None)
                    candidate = None
            if candidate is None:
                candidate = detect_fib_pump(candles)
                if not candidate:
                    if contract in early_pattern_details: early_filter_status[contract] = "Pump pattern no longer valid in final scan"
                    continue
                candidate.update({
                    "market_cap": market_cap, "volume24": total_market_volume,
                    "oi_usd": oi_usd, "funding": funding,
                })
                candidates[contract] = candidate
                print(
                    f"{contract}: LEG SPIKE pump found: {candidate['pump_pct']:.2f}% "
                    f"in {candidate['body_count']} candles; waiting for Fib retracement"
                )
            counts["pump_detected"] += 1

            # Apply the long/short crowding filter only after the coin has passed
            # market-cap/volume/OI checks and an actual impulse exists.
            ls = leg_spike_long_short_filter(contract)
            if not ls.get("valid") or not ls.get("passed"):
                if contract in early_pattern_details:
                    early_filter_status[contract] = ("Long/short ratio rejected" if ls.get("valid") else "OKX ratio data unavailable")
                if ls.get("valid"):
                    counts["ratio_rejected"] += 1
                    print(
                        f"{contract}: LEG SPIKE ratio rejected; "
                        f"15m={ls['ratio_15m']:.1f}%, 1h={ls['ratio_1h']:.1f}%, "
                        f"1d={ls['ratio_1d']:.1f}%"
                    )
                else:
                    counts["ratio_unavailable"] += 1
                continue
            counts["ratio_pass"] += 1
            if contract in early_pattern_details: early_filter_status[contract] = "Data filters passed; waiting for closed-candle Fib reaction"

            price = float(candles[-1]["c"])
            low = float(candidate["low"])
            high = float(candidate["high"])
            if price <= low:
                candidates.pop(contract, None)
                print(f"{contract}: LEG SPIKE invalidated; impulse low breached")
                continue
            levels = (("0.50", float(candidate["fib50"])), ("0.66", float(candidate["fib66"])))
            for level_name, level in levels:
                alert_key = f'{candidate["end_time"]}:{level_name}'
                if sent.get(contract) == alert_key:
                    continue
                previous_close = float(candles[-2]["c"])
                # Require a downward retracement through the level on a closed candle.
                if previous_close > level and price <= level and price > low:
                    counts["fib_reactions"] += 1
                    risk = level - low
                    reward = high - level
                    if risk <= 0:
                        continue
                    metrics = coinalyze_spike_metrics(contract, ticker, market_cap, total_market_volume, state)
                    if metrics.get("change24") not in (None, "") and float(metrics["change24"]) <= 0:
                        print(f"{contract}: LEG SPIKE rejected; 24h price change is not positive")
                        continue
                    if metrics.get("oi_change4h") is None or metrics["oi_change4h"] <= 0:
                        print(f"{contract}: LEG SPIKE rejected; Coinalyze OI 4h change is missing or not positive")
                        continue
                    if metrics.get("funding_avg") is not None and metrics["funding_avg"] >= 0:
                        print(f"{contract}: LEG SPIKE rejected; Coinalyze average funding is non-negative")
                        continue
                    if metrics.get("long1h") is not None and metrics["long1h"] < 60:
                        print(f"{contract}: LEG SPIKE rejected; Coinalyze 1h long share is below 60%")
                        continue
                    if metrics.get("long1d") is not None and metrics["long1d"] > 70:
                        print(f"{contract}: LEG SPIKE rejected; Coinalyze daily long share exceeds 70%")
                        continue
                    alert_oi = metrics.get("oi_usd") or oi_usd
                    alert_funding = metrics.get("funding_avg")
                    if alert_funding is None:
                        alert_funding = funding
                    if alert_oi / market_cap < FIB_MIN_OI_TO_MARKET_CAP:
                        print(f"{contract}: LEG SPIKE rejected; OI/market cap below minimum")
                        continue
                    daily_long = metrics.get("long1d")
                    daily_warning = ("⚠️ هشدار شلوغی لانگ روزانه؛ ریسک ازدحام بالا"
                                     if daily_long is not None and daily_long > 60
                                     else "لانگ روزانه زیر آستانه هشدار")
                    message = (
                        f"🎯 🚀 LEG SPIKE — CoinAlayze + Fibonacci\n"
                        f"{contract.replace('_', '/')} (Gate.io Perpetual)\n"
                        f"واکنش به فیبو {level_name}؛ ورود پیشنهادی: {level:.10g}\n"
                        f"ورود ۱ (فیبو 0.50): {float(candidate['fib50']):.10g}\n"
                        f"ورود ۲ (فیبو 0.66): {float(candidate['fib66']):.10g}\n"
                        f"سقف پامپ/هدف: {high:.10g} | کف پامپ/حد ضرر: {low:.10g}\n"
                        f"حرکت پامپ: {float(candidate['pump_pct']):.2f}% با {int(candidate['body_count'])} کندل قوی ۵دقیقه‌ای\n"
                        f"نسبت لانگ OKX: ۱۵دقیقه {ls['ratio_15m']:.1f}% | ۱ساعت {ls['ratio_1h']:.1f}% | روزانه {ls['ratio_1d']:.1f}%\n"
                        f"{daily_warning}\n"
                        f"{spike_metric_text(metrics)}\n"
                        f"Funding مؤثر برای فیلتر: {alert_funding * 100:+.4f}% | OI مؤثر: {alert_oi / market_cap:.2%} از مارکت‌کپ\n"
                        f"نسبت سود به ریسک تا سقف: {reward / risk:.2f}:1\n"
                        "⚠️ داده ناموجود از منبع در دسترس نبود؛ هشدار الگوریتمی است و ورود تضمینی نیست."
                    )
                    send_message(message)
                    sent[contract] = alert_key
                    print(f"LEG SPIKE alert sent: {contract} fib={level_name}")
                    break
        except Exception as error:
            print(f"{contract}: LEG SPIKE skipped: {type(error).__name__}: {error}")
    volume_samples.sort(reverse=True)
    top_volume = "; ".join(
        f"{contract}:vol/MC={ratio:.3f}x,vol=${volume:,.0f},MC=${cap:,.0f},funding={funding*100:+.4f}%"
        for ratio, contract, volume, cap, funding in volume_samples[:5]
    ) or "none"
    print(
        "LEG SPIKE diagnostics: " +
        ", ".join(f"{key}={value}" for key, value in counts.items()) +
        f", active={len(candidates)}, caps_cached={len(caps)}, ambiguous_symbols={len(ambiguous)}"
    )
    print("LEG SPIKE early-pattern samples: " + ("; ".join(early_pattern_samples) or "none"))
    early_filter_snapshot = []
    ticker_by_contract = {item.get("contract", ""): item for item in tickers}
    for contract, impulse in early_pattern_details.items():
        item = ticker_by_contract.get(contract, {})
        raw_funding = item.get("funding_rate")
        symbol = contract[:-5].upper()
        cap = caps.get(symbol)
        volume = market_volumes.get(symbol)
        funding_label = "missing" if raw_funding in (None, "") else f"{float(raw_funding) * 100:+.4f}%"
        ratio_label = f"{float(volume) / float(cap):.3f}x" if cap and volume and symbol not in ambiguous else "missing/ambiguous"
        early_filter_snapshot.append(f"{contract}: funding={funding_label}, vol/MC={ratio_label}")
    print("LEG SPIKE early-pattern filter snapshot: " + ("; ".join(early_filter_snapshot) or "none"))
    print("LEG SPIKE early-pattern filter results: " + ("; ".join(f"{k}={v}" for k, v in early_filter_status.items()) or "none"))
    print(f"LEG SPIKE highest volume/market-cap candidates: {top_volume}")

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
        if is_crypto_contract(contract) and turnover24 >= 1_000_000:
            liquid.append((item, turnover24))
    coins = sorted(liquid, key=lambda pair: pair[1], reverse=True)[:100]
    print(f"Gate.io liquid contracts checked: {len(coins)}")

    state = load_alert_state()
    alerts_state = state["alerts"]
    candidates = state["candidates"]
    early_alerts_state = state["early_alerts"]

    btc_ok, btc_change = btc_market_filter()

    # Run LEG SPIKE's independent early-warning scan even when BTC pauses confirmed entries.
    # The BTC filter still blocks final entries for both strategies.
    # LEG SPIKE scans the full Gate.io USDT perpetual universe; regular strategy stays top-100 liquid only.
    scan_fib_pullbacks(tickers, state, confirmed_entries=btc_ok)
    if not btc_ok:
        print("BTC filter: paused confirmed entry alerts; independent early warnings were still scanned.")
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
                recent_early = state.setdefault("early_any", {})
                try:
                    last_early_time = int(recent_early.get(contract, 0))
                except (TypeError, ValueError):
                    last_early_time = 0
                if int(time.time()) - last_early_time < 3600:
                    early_alerts_state[contract] = candidate["impulse_time"]
                    print(f"{contract}: regular early alert suppressed; another strategy alerted within 1h")
                else:
                    early_message = (
                    f"👀 هشدار زودهنگام؛ هنوز تأیید نشده: {contract.replace('_', '/')} (Gate.io Perpetual)\n"
                    f"حرکت اولیه: {float(candidate['impulse_change']):+.2f}%؛ حجم {float(candidate['impulse_ratio']):.1f} برابر میانگین ۲۰ کندل قبلی\n"
                    f"قیمت فعلیِ کندل بسته‌شده ۵دقیقه: {latest_close}\n"
                    f"کف حرکت اولیه: {float(candidate['impulse_low'])}\n\n"
                    "هنوز سیگنال ورود نیست. ربات منتظر شکست ساختار ۵دقیقه، تأیید ۱۵دقیقه و روند ۱ساعته، سپس بررسی OI و Funding می‌ماند."
                )
                    send_message(early_message)
                    early_alerts_state[contract] = candidate["impulse_time"]
                    recent_early[contract] = int(time.time())
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
            if candidate.get("bos_level") is None and len(previous_three) == 3 and latest_close > latest_open:
                short_term_high = max(float(item["h"]) for item in previous_three)
                if latest_close > short_term_high:
                    candidate["bos_level"] = short_term_high
                    candidate["bos_time"] = str(latest.get("t", ""))
                    setup_alerts = state.setdefault("setup_alerts", {})
                    if setup_alerts.get(contract) != candidate["impulse_time"]:
                        setup_message = (
                            f"🟡 شکست ساختار ۵دقیقه‌ای: {contract.replace('_', '/')} (Gate.io Perpetual)\n"
                            f"سطح شکست: {short_term_high}\n"
                            f"قیمت کندل بسته‌شده: {latest_close}\n\n"
                            "این مرحله از هشدار اولیه قوی‌تر است، اما هنوز تأیید ورود نیست. ربات منتظر کندل ۱۵دقیقه‌ای، روند ۱ساعته و بررسی OI/Funding می‌ماند."
                        )
                        send_message(setup_message)
                        setup_alerts[contract] = candidate["impulse_time"]
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

            derivatives_text = (
                f"OI گیت: {oi_change:+.2f}% | Funding گیت: {funding_rate * 100:+.4f}%"
            )

            # Coinalyze is an additional multi-exchange confirmation when its API key is configured.
            coinalyze_check = coinalyze_derivatives_check(contract, state)
            if coinalyze_check.get("configured"):
                public_derivatives_checks += 1
                if not coinalyze_check.get("valid"):
                    print(f"{contract}: skipped; Coinalyze confirmation unavailable ({coinalyze_check.get('reason', 'unknown')})")
                    continue
                if float(coinalyze_check["oi_change"]) < 0.2:
                    print(f"{contract}: skipped; Coinalyze aggregate OI change {float(coinalyze_check['oi_change']):+.3f}% below +0.2%")
                    continue
                if float(coinalyze_check["funding_rate"]) >= 0.0005:
                    print(f"{contract}: skipped; Coinalyze average funding {float(coinalyze_check['funding_rate'])*100:+.4f}% is at/above +0.05%")
                    continue
                derivatives_text += (
                    f"\nتأیید Coinalyze ({', '.join(coinalyze_check['oi_exchanges'])}): "
                    f"OI تجمیعی {float(coinalyze_check['oi_change']):+.2f}% | "
                    f"Funding میانگین {float(coinalyze_check['funding_rate'])*100:+.4f}%"
                )
            else:
                print(f"{contract}: Coinalyze skipped; COINALYZE_API_KEY secret is not configured")

            # Keep Bybit as a best-effort diagnostic cross-check; it must not override the Gate.io/Coinalyze gates.
            public_derivatives_checks += 1
            cross_check = public_derivatives_check(contract)
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
    print("Coinalyze API key configured: " + ("yes" if COINALYZE_API_KEY else "no"))
    if COINALYZE_API_KEY:
        try:
            _coinalyze_markets = coinalyze_request("/future-markets")
            print(f"Coinalyze API connectivity: OK; markets={len(_coinalyze_markets)}")
        except Exception as _coinalyze_error:
            print(f"Coinalyze API connectivity: FAILED ({type(_coinalyze_error).__name__}: {_coinalyze_error})")
    if not TOKEN or not CHAT_ID:
        raise SystemExit("BOT_TOKEN یا CHAT_ID در GitHub Secrets تنظیم نشده است.")
    scan()
