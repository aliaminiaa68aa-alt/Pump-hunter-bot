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
    # Leveraged/inverse equity and sector ETFs sometimes listed as USDT perpetuals.
    "SOXS", "SOXL", "SQQQ", "TQQQ", "SPXS", "SPXL", "UVXY", "LABU", "LABD",
    "FNGU", "FNGD", "NVDL", "NVD", "TSLL", "TSLQ", "CONL", "CONY", "MSTU",
    "MSTX", "BITX", "ETHU", "YINN", "YANG", "KOLD", "BOIL", "TZA", "TNA",
    # Equity-linked contracts observed in Gate.io futures scans (not crypto assets).
    "SNXX", "INTW", "SNDK", "SKHY", "CXMT", "DRAM", "CRWV",
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
    """Only pause final entries during an unusually sharp BTC 15m sell-off."""
    candles = closed_candles("BTC_USDT", "15m", 4)
    if len(candles) < 2:
        print("BTC risk filter: insufficient 15m candle data; allowing scan")
        return True, None
    candle = candles[-1]
    open_price = float(candle["o"])
    close_price = float(candle["c"])
    change = (close_price / open_price - 1) * 100 if open_price else 0.0
    print(f"BTC 15m closed-candle change: {change:+.2f}%")
    # A routine BTC pullback must not veto an independently confirmed altcoin setup.
    # A closed 15m BTC candle at -2.0% or lower pauses final-entry alerts.
    if change <= -2.0:
        print("BTC risk filter: paused final entries only; BTC 15m sell-off is severe (<= -3.0%)")
        return False, change
    print("BTC risk filter: no severe 15m sell-off; altcoin setup checks may continue")
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
    data.setdefault("early_any", {})  # Regular BOS strategy cooldowns
    data.setdefault("fib_early_any", {})  # LEG SPIKE cooldowns are independent
    data.setdefault("setup_alerts", {})  # Backward-compatible legacy state
    data.setdefault("regular_setup_alerts", {})
    data.setdefault("fib_setup_alerts", {})
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
COINMARKETCAP_API_KEY = os.getenv("COINMARKETCAP_API_KEY", "").strip()


def coinalyze_request(path, params=None):
    """Authenticated Coinalyze request with a bounded retry for temporary rate limits."""
    if not COINALYZE_API_KEY:
        raise RuntimeError("COINALYZE_API_KEY secret is not configured")
    response = None
    for attempt in range(2):
        response = SESSION.get(
            COINALYZE_BASE + path,
            params=params,
            headers={"api_key": COINALYZE_API_KEY},
            timeout=15,
        )
        if response.status_code != 429 or attempt == 1:
            break
        retry_after = response.headers.get("Retry-After", "")
        try:
            delay = min(max(float(retry_after), 1.0), 3.0)
        except (TypeError, ValueError):
            delay = 1.5
        print(f"Coinalyze rate limit on {path}; retrying once after {delay:.1f}s")
        time.sleep(delay)
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
        # Two independent exchanges are enough for the cross-check and keep
        # weighted symbol usage inside Coinalyze's 40-calls/minute quota.
        for row in eligible:
            symbol = str(row["symbol"])
            if symbol not in seen:
                selected.append(row)
                seen.add(symbol)
            if len(selected) >= 2:
                break
        if len(selected) < 2:
            return {"configured": True, "valid": False, "reason": "Coinalyze کمتر از دو بازار پرپچوال USDT برای این ارز دارد"}

        symbols = ",".join(str(row["symbol"]) for row in selected)
        oi_rows = coinalyze_request(
            "/open-interest-history",
            {"symbols": symbols, "interval": "5min", "from": now - 1800, "to": now, "convert_to_usd": "true"},
        )
        oi4_rows = coinalyze_request(
            "/open-interest-history",
            {"symbols": symbols, "interval": "1hour", "from": now - 5 * 3600, "to": now, "convert_to_usd": "true"},
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
        oi4_old_total = oi4_new_total = 0.0
        oi4_exchanges = 0
        oi4_by_symbol = {str(row.get("symbol", "")): row for row in oi4_rows}
        for symbol in seen:
            history4 = sorted(oi4_by_symbol.get(symbol, {}).get("history", []), key=lambda point: int(point.get("t", 0)))
            recent4 = [point for point in history4 if int(point.get("t", 0)) >= now - 4 * 3600]
            if len(recent4) < 2:
                continue
            old4 = float(recent4[0].get("c", 0) or 0)
            new4 = float(recent4[-1].get("c", 0) or 0)
            if old4 > 0 and new4 > 0:
                oi4_old_total += old4
                oi4_new_total += new4
                oi4_exchanges += 1
        oi_change4h = ((oi4_new_total / oi4_old_total - 1.0) * 100.0) if oi4_exchanges >= 2 and oi4_old_total > 0 else None
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
            f"aggregate_OI_change_4h={(f'{oi_change4h:+.3f}%' if oi_change4h is not None else 'unavailable')} "
            f"OI_4h_exchanges={oi4_exchanges} "
            f"aggregate_OI_USD={new_total:.0f} average_funding={funding_avg*100:+.4f}% "
            f"funding_raw_values=[{','.join(funding_raw_diagnostics)}]"
        )
        return {
            "configured": True, "valid": True,
            "oi_change": oi_change, "oi_change4h": oi_change4h, "funding_rate": funding_avg,
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
              "liquidations24": None, "long15m": None, "long1h": None, "short1h": None,
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
            if len(selected) == 2:
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

        for label, interval, since in (("15m", "15min", now - 3600),
                                       ("1h", "1hour", now - 2 * 3600),
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
        f"⚪️ Long 15m: {pct(metrics.get('long15m'))}\n"
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
# Stage-2 impulse detection: slightly more inclusive so valid developing
# setups are tracked; Stage-3 entry confirmation remains fully gated by Fib,
# OI, funding, and long/short checks.
FIB_MIN_BODY_PCT = 0.7
FIB_MIN_TOTAL_PUMP_PCT = 3.5
COINGECKO_BASE = "https://api.coingecko.com/api/v3"


def get_market_caps(state, requested_symbols=None):
    """CoinGecko first; use CoinMarketCap for missing/ambiguous candidate symbols."""
    cache = state.get("market_cap_cache", {})
    now = int(time.time())
    requested = list(dict.fromkeys(str(symbol).upper() for symbol in (requested_symbols or []) if symbol))
    fresh = (cache.get("fetched_at") and now - int(cache["fetched_at"]) < 900
             and isinstance(cache.get("volumes"), dict))
    if fresh:
        caps = dict(cache.get("caps", {}))
        volumes = dict(cache.get("volumes", {}))
        ambiguous = set(cache.get("ambiguous", []))
        checked = set(cache.get("cmc_checked_symbols", []))
    else:
        all_rows, caps, volumes, ambiguous, checked = [], {}, {}, set(), set()
        try:
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
            counts = {}
            for row in all_rows:
                symbol = str(row.get("symbol", "")).upper()
                cap = row.get("market_cap")
                if not symbol or cap in (None, "") or float(cap) <= 0:
                    continue
                counts[symbol] = counts.get(symbol, 0) + 1
                if counts[symbol] == 1:
                    caps[symbol] = float(cap)
                    volume = row.get("total_volume")
                    if volume not in (None, "") and float(volume) > 0:
                        volumes[symbol] = float(volume)
            ambiguous = {symbol for symbol, count in counts.items() if count > 1}
            for symbol in ambiguous:
                caps.pop(symbol, None)
                volumes.pop(symbol, None)
            print(f"CoinGecko market data refreshed: {len(caps)} unique market caps, {len(volumes)} 24h volumes; {len(ambiguous)} ambiguous symbols")
        except Exception as error:
            print(f"CoinGecko market-cap lookup unavailable: {type(error).__name__}: {error}")

    # One CMC request per scan, limited to high-priority missing symbols, to avoid HTTP 429.
    missing = [symbol for symbol in requested if symbol not in caps or symbol in ambiguous]
    missing = [symbol for symbol in missing if symbol not in checked][:50]
    cmc_resolved, cmc_ambiguous = 0, set()
    if missing:
        try:
            if COINMARKETCAP_API_KEY:
                cmc_url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
                cmc_headers = {"X-CMC_PRO_API_KEY": COINMARKETCAP_API_KEY, "Accept": "application/json"}
            else:
                cmc_url = "https://pro-api.coinmarketcap.com/public-api/v3/cryptocurrency/quotes/latest"
                cmc_headers = {"Accept": "application/json"}
            response = SESSION.get(cmc_url, params={"symbol": ",".join(missing), "convert": "USD"}, headers=cmc_headers, timeout=20)
            response.raise_for_status()
            data = response.json().get("data", {})
            grouped = {}
            if isinstance(data, dict):
                for key, value in data.items():
                    rows = value if isinstance(value, list) else [value]
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        symbol = str(row.get("symbol", key)).upper()
                        quote = (row.get("quote") or {}).get("USD") or {}
                        cap, volume = quote.get("market_cap"), quote.get("volume_24h")
                        if symbol in missing and cap not in (None, "") and float(cap) > 0:
                            grouped.setdefault(symbol, []).append((float(cap), volume))
            for symbol in missing:
                checked.add(symbol)
                matches = grouped.get(symbol, [])
                if len(matches) == 1:
                    caps[symbol] = matches[0][0]
                    if matches[0][1] not in (None, "") and float(matches[0][1]) > 0:
                        volumes[symbol] = float(matches[0][1])
                    ambiguous.discard(symbol)
                    cmc_resolved += 1
                elif len(matches) > 1:
                    caps.pop(symbol, None)
                    volumes.pop(symbol, None)
                    ambiguous.add(symbol)
                    cmc_ambiguous.add(symbol)
            print(f"CoinMarketCap fallback: requested={len(missing)}, resolved={cmc_resolved}, still_ambiguous={len(cmc_ambiguous)}, api_key_configured={bool(COINMARKETCAP_API_KEY)}")
        except Exception as error:
            print(f"CoinMarketCap fallback unavailable for {len(missing)} candidate symbols: {type(error).__name__}: {error}")

    # Independent free fallback: CoinPaprika, used only for symbols still missing/ambiguous.
    # It avoids relying on CoinMarketCap's keyless endpoint, which may rate-limit requests.
    # Let CoinPaprika cover all requested high-priority symbols still missing after CoinGecko/CMC, not only the first 50 CMC query symbols.
    paprika_missing = list(dict.fromkeys(symbol for symbol in requested if symbol not in caps or symbol in ambiguous))
    if paprika_missing:
        try:
            response = SESSION.get("https://api.coinpaprika.com/v1/tickers",
                                   params={"quotes": "USD"}, timeout=25)
            response.raise_for_status()
            rows = response.json()
            grouped = {}
            if isinstance(rows, list):
                for row in rows:
                    symbol = str(row.get("symbol", "")).upper()
                    if symbol not in paprika_missing:
                        continue
                    quote = (row.get("quotes") or {}).get("USD") or {}
                    cap, volume = quote.get("market_cap"), quote.get("volume_24h")
                    if cap not in (None, "") and float(cap) > 0:
                        grouped.setdefault(symbol, []).append((float(cap), volume))
            paprika_resolved = 0
            paprika_ambiguous = 0
            for symbol in paprika_missing:
                matches = grouped.get(symbol, [])
                if len(matches) == 1:
                    caps[symbol] = matches[0][0]
                    if matches[0][1] not in (None, "") and float(matches[0][1]) > 0:
                        volumes[symbol] = float(matches[0][1])
                    ambiguous.discard(symbol)
                    paprika_resolved += 1
                elif len(matches) > 1:
                    caps.pop(symbol, None)
                    volumes.pop(symbol, None)
                    ambiguous.add(symbol)
                    paprika_ambiguous += 1
            print(f"CoinPaprika fallback: requested={len(paprika_missing)}, resolved={paprika_resolved}, ambiguous={paprika_ambiguous}")
        except Exception as error:
            print(f"CoinPaprika fallback unavailable for {len(paprika_missing)} candidate symbols: {type(error).__name__}: {error}")

    state["market_cap_cache"] = {
        "fetched_at": now if not fresh else cache.get("fetched_at", now),
        "caps": caps, "volumes": volumes, "ambiguous": sorted(ambiguous),
        "cmc_checked_symbols": sorted(checked),
    }
    return caps, sorted(ambiguous)

def detect_fib_pump(candles, min_body_pct=FIB_MIN_BODY_PCT, min_total_pump_pct=FIB_MIN_TOTAL_PUMP_PCT):
    """Find a recent bullish impulse with a closed bullish FVG and at least two strong bodies."""
    if len(candles) < 10:
        return None
    start_min = max(0, len(candles) - 7)
    # A classic bullish FVG is confirmed when candle C's low is above candle A's high.
    for end_idx in range(len(candles) - 1, max(start_min + 1, 1), -1):
        first_idx = end_idx - 2
        if first_idx < start_min:
            continue
        try:
            a, b, c = candles[first_idx], candles[first_idx + 1], candles[end_idx]
            if float(c["l"]) <= float(a["h"]) or float(c["c"]) <= float(c["o"]):
                continue
            options = []
            # Fib starts at the OPEN/body-start, not the wick low.
            for anchor_idx in (first_idx, first_idx + 1):
                seq = candles[anchor_idx:end_idx + 1]
                strong = 0
                for item in seq:
                    op, cl = float(item["o"]), float(item["c"])
                    if op > 0 and cl > op and (cl / op - 1) * 100 >= min_body_pct:
                        strong += 1
                if strong < 2:
                    continue
                body_start = float(seq[0]["o"])
                high = max(float(item["h"]) for item in seq)
                pump_pct = (high / body_start - 1) * 100 if body_start > 0 else 0
                if pump_pct < min_total_pump_pct or high <= body_start:
                    continue
                options.append({
                    "start_time": str(seq[0].get("t", anchor_idx)),
                    "end_time": str(seq[-1].get("t", end_idx)),
                    "low": body_start,
                    "high": high,
                    "pump_pct": pump_pct,
                    "body_count": strong,
                    "candle_count": len(seq),
                    "fvg_low": float(a["h"]),
                    "fvg_high": float(c["l"]),
                    "fib50": high - (high - body_start) * 0.50,
                    "fib66": high - (high - body_start) * 0.66,
                })
            if options:
                options.sort(key=lambda x: (x["candle_count"], x["pump_pct"]), reverse=True)
                return options[0]
        except (TypeError, ValueError, KeyError, ZeroDivisionError):
            continue
    return None


def scan_fib_pullbacks(tickers, state, confirmed_entries=True):
    """Scan LEG SPIKE with staged diagnostics and avoid ratio API calls before a real pump exists."""
    # Include a wider liquid-symbol universe so fallback providers can cover missing caps.
    cmc_priority = sorted(
        (item for item in tickers if is_crypto_contract(item.get("contract", ""))),
        key=lambda item: float(item.get("volume_24h_quote") or item.get("volume_24h_usd") or 0),
        reverse=True,
    )[:300]
    # Always request market data for persisted LEG SPIKE candidates too, even if
    # they have temporarily fallen outside the top-300 turnover list. Otherwise
    # tracked setups can never reach Stage 2/3 simply because their symbol was
    # omitted from the current market-cap lookup universe.
    candidates = state.setdefault("fib_candidates", {})
    cap_symbols = [item.get("contract", "").split("_")[0] for item in cmc_priority]
    live_contracts = {item.get("contract", "") for item in tickers}
    tracked_symbols = [
        contract[:-5] for contract in candidates
        if contract in live_contracts and is_crypto_contract(contract)
    ]
    cap_symbols = list(dict.fromkeys(cap_symbols + tracked_symbols))
    caps, ambiguous = get_market_caps(state, cap_symbols)
    market_volumes = state.get("market_cap_cache", {}).get("volumes", {})
    sent = state["fib_alerts"]
    counts = {
        "tickers": 0, "negative_funding": 0, "cap_available": 0,
        "early_scan_checked": 0, "early_patterns_seen": 0, "early_warnings_sent": 0,
        "early_duplicate_suppressed": 0, "early_recent_suppressed": 0, "early_candle_errors": 0, "volume_pass": 0, "oi_pass": 0,
        "pump_detected": 0, "pump_pattern_rejected": 0,
        "coinalyze_unavailable": 0, "oi_rejected": 0, "quota_skipped": 0,
        "ratio_pass": 0, "ratio_rejected": 0,
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
    # Scan more liquid contracts for early alerts; final-entry filters remain strict.
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

    for _turnover, item in liquid_for_early[:150]:
        contract = item.get("contract", "")
        try:
            candles = closed_candles(contract, "15m", 20)
            early_candle_cache[contract] = candles
            counts["early_scan_checked"] += 1
            early_candidate = detect_fib_pump(candles, min_body_pct=0.6, min_total_pump_pct=3.0)
            is_new_early_candidate = early_candidate is not None
            if not early_candidate:
                # Keep monitoring an already tracked impulse even when this scan
                # does not rediscover a fresh impulse. Otherwise top-break alerts
                # can be silently skipped between scans.
                early_candidate = candidates.get(contract)
                if not early_candidate:
                    continue
                print(f"{contract}: monitoring previously tracked LEG SPIKE impulse")
            else:
                counts["early_patterns_seen"] += 1
                early_pattern_details[contract] = early_candidate

            # Persist the exact coin and impulse immediately after Stage 1.
            # This lets later scans monitor this same setup even if it temporarily
            # falls outside the early-scan top 150 or fails a filter this cycle.
            tracked_candidate = candidates.get(contract)
            replace_tracked = tracked_candidate is None
            if tracked_candidate is not None:
                try:
                    tracked_age = int(early_candidate["end_time"]) - int(tracked_candidate["end_time"])
                    tracked_expired = int(time.time()) - int(tracked_candidate["end_time"]) > 3600
                    replace_tracked = tracked_expired or tracked_age > 0
                except (TypeError, ValueError, KeyError):
                    replace_tracked = True
            if replace_tracked:
                candidates[contract] = dict(early_candidate)
                print(
                    f"{contract}: Stage-1 impulse saved for continued monitoring; "
                    f"pump={float(early_candidate['pump_pct']):.2f}%"
                )

            # Monitor the saved Stage-1 impulse on every scan, before derivatives
            # gates. The user wants a top-touch/break alert, not a filtered entry call.
            tracked_for_top = candidates.get(contract) or early_candidate
            try:
                top_low = float(tracked_for_top["low"])
                top_high = float(tracked_for_top["high"])
                top_end = int(tracked_for_top["end_time"])
                top_price = float(candles[-1]["c"])
                top_state = state.setdefault("fib_top_alerts", {})
                top_key_stage1 = str(top_end)
                if top_price <= top_low:
                    if contract in candidates:
                        candidates.pop(contract, None)
                    print(f"{contract}: LEG SPIKE invalidated at Stage 1; impulse low breached")
                elif top_state.get(contract) != top_key_stage1:
                    top_break_candle = None
                    for candle_idx in range(1, len(candles)):
                        prev_candle, closed_candle = candles[candle_idx - 1], candles[candle_idx]
                        try:
                            if int(closed_candle["t"]) <= top_end:
                                continue
                            if float(prev_candle["c"]) < top_high and float(closed_candle["h"]) >= top_high:
                                top_break_candle = closed_candle
                        except (TypeError, ValueError, KeyError):
                            continue
                    if top_break_candle is not None:
                        confirmed_top = float(top_break_candle["h"])
                        top_message = (
                            "🚀 LEG SPIKE | هشدار رسیدن به سقف پامپ\n"
                            "🟢━━━━━━━━━━━━━━━━━━🟢\n"
                            "🔔 قیمت به سقف حرکت شناسایی‌شده رسید/آن را شکست\n"
                            f"🪙 ارز: {contract.replace('_', '/')}\n"
                            f"🎯 سقف حرکت: {top_high:.10g}\n"
                            f"📍 بیشترین قیمت کندل تأیید: {confirmed_top:.10g}\n"
                            f"💰 آخرین قیمت بسته‌شده: {top_price:.10g}\n"
                            f"📈 اندازه حرکت: {float(tracked_for_top['pump_pct']):.2f}% در {int(tracked_for_top['body_count'])} کندل ۱۵دقیقه‌ای\n"
                            "⚠️ این هشدار فقط رسیدن/شکست سقف را اعلام می‌کند؛ نقطه ورود، حدضرر و تارگت را خودت تعیین کن.\n"
                            "🟢━━━━━━━━━━━━━━━━━━🟢"
                        )
                        send_message(top_message)
                        top_state[contract] = top_key_stage1
                        print(f"{contract}: Stage-1 top-break alert sent at {confirmed_top:.10g}")
            except (TypeError, ValueError, KeyError, IndexError) as error:
                print(f"{contract}: Stage-1 top monitor skipped: {error}")

            if is_new_early_candidate and len(early_pattern_samples) < 8:
                early_pattern_samples.append(
                    f"{contract}:{early_candidate['pump_pct']:.2f}%/{early_candidate['body_count']}c"
                )
            early_key = str(early_candidate["end_time"])
            early_sent = state["fib_early_alerts"]
            if early_sent.get(contract) == early_key:
                counts["early_duplicate_suppressed"] += 1
                print(f"{contract}: LEG SPIKE early alert suppressed; same candle already alerted")
                continue
            # LEG SPIKE has its own cooldown map; it never reads or writes regular-strategy cooldowns.
            recent_early = state.setdefault("fib_early_any", {})
            try:
                last_early_time = int(recent_early.get(contract, 0))
            except (TypeError, ValueError):
                last_early_time = 0
            if int(time.time()) - last_early_time < 3600:
                early_sent[contract] = early_key
                counts["early_recent_suppressed"] += 1
                print(f"{contract}: LEG SPIKE early alert suppressed by its own 1h cooldown")
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
                "🚀 LEG SPIKE | شکار پامپ\n"
                "🟡━━━━━━━━━━━━━━━━━━🟡\n"
                f"👀 مرحله ۱ از ۳ | هشدار اولیه؛ هنوز تأیید نشده: {contract.replace('_', '/')}\n"
                f"حرکت شناسایی‌شده: {float(early_candidate['pump_pct']):.2f}% "
                f"در {int(early_candidate['body_count'])} کندل صعودی ۱۵دقیقه‌ای\n"
                f"سقف: {float(early_candidate['high']):.10g} | کف: {float(early_candidate['low']):.10g}\n"
                f"فیبو 0.50: {float(early_candidate['fib50']):.10g} | "
                f"فیبو 0.66: {float(early_candidate['fib66']):.10g}\n"
                f"Funding: {funding_text} | حجم/مارکت‌کپ: {vol_mc_text}\n"
                "⚠️ این فقط هشدار حرکت اولیه است، نه نقطه ورود. برای سیگنال نهایی تمام فیلترهای Funding، حجم/مارکت‌کپ، OI، نسبت لانگ/شورت و واکنش کندل بسته‌شده لازم‌اند."
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
        print("BTC filter: keeping LEG SPIKE Stage-2 monitoring active; only final-entry alerts are paused.")

    coinalyze_checks_this_scan = 0
    # Coinalyze's practical endpoint quota was hit when checking 8 symbols
    # in one scan (HTTP 429 on OI history). Keep the per-scan budget conservative;
    # tracked Stage-1 candidates are already ranked ahead of general symbols.
    max_coinalyze_checks_per_scan = 5
    def _turnover_rank(item):
        try:
            return float(item.get("volume_24h_quote") or item.get("volume_24h_usd") or 0)
        except (TypeError, ValueError):
            return 0.0

    # Rank qualifying candidates by actual Coinalyze OI change over 4h.
    prequalified = []
    priority_contracts = set(early_pattern_details) | set(candidates)
    for item in tickers:
        contract0 = item.get("contract", "")
        if not is_crypto_contract(contract0):
            continue
        try:
            raw_funding0 = item.get("funding_rate")
            funding0 = float(raw_funding0) if raw_funding0 not in (None, "") else None
            # Tracked LEG SPIKE setups must reach Coinalyze even when Gate.io's
            # single-venue funding is zero, positive, or missing. Aggregate
            # funding is validated later by coinalyze_derivatives_check.
            if (funding0 is None or funding0 >= 0) and not (COINALYZE_API_KEY and contract0 in priority_contracts):
                continue
            symbol0 = contract0[:-5].upper()
            cap0 = caps.get(symbol0)
            vol0 = market_volumes.get(symbol0)
            if not cap0 or symbol0 in ambiguous or not vol0:
                continue
            # Allow tracked early impulses into the bounded OI sample; the strict
            # volume/market-cap gate still applies before setup confirmation.
            if float(vol0) / float(cap0) < FIB_MIN_VOLUME_TO_MARKET_CAP and contract0 not in priority_contracts:
                continue
            prequalified.append(item)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    # Diversify the small API-budget sample so it is not selected by turnover alone:
    # take candidates from turnover, volume/market-cap intensity, and 24h price change.
    def _volume_cap_rank(item):
        try:
            symbol = item.get("contract", "")[:-5].upper()
            return float(market_volumes.get(symbol) or 0) / float(caps.get(symbol) or 1)
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0

    def _change24_rank(item):
        try:
            return float(item.get("change_percentage") or 0)
        except (TypeError, ValueError):
            return 0.0

    selected_for_oi = []
    selected_contracts = set()
    # Prioritize both fresh Stage-1 detections and persisted candidates from earlier scans.
    # Otherwise a tracked coin can be starved by the five-call Coinalyze quota after its first scan.
    early_contracts = set(early_pattern_details) | set(candidates)
    early_prequalified = [item for item in prequalified if item.get("contract", "") in early_contracts]
    other_prequalified = [item for item in prequalified if item.get("contract", "") not in early_contracts]
    ranked_pools = [
        # Give an actual early pump candidate first chance at Coinalyze checks.
        sorted(early_prequalified, key=_turnover_rank, reverse=True),
        sorted(early_prequalified, key=_volume_cap_rank, reverse=True),
        sorted(early_prequalified, key=_change24_rank, reverse=True),
        sorted(other_prequalified, key=_turnover_rank, reverse=True),
        sorted(other_prequalified, key=_volume_cap_rank, reverse=True),
        sorted(other_prequalified, key=_change24_rank, reverse=True),
    ]
    while len(selected_for_oi) < max_coinalyze_checks_per_scan:
        added = False
        for pool in ranked_pools:
            for candidate in pool:
                candidate_contract = candidate.get("contract", "")
                if candidate_contract not in selected_contracts:
                    selected_for_oi.append(candidate)
                    selected_contracts.add(candidate_contract)
                    added = True
                    break
            if len(selected_for_oi) >= max_coinalyze_checks_per_scan:
                break
        if not added:
            break
    print("OI4h precheck sample: " + (", ".join(selected_contracts) if selected_contracts else "none"))
    ranked_checks = {}
    if COINALYZE_API_KEY:
        for item in selected_for_oi:
            contract0 = item.get("contract", "")
            ranked_checks[contract0] = coinalyze_derivatives_check(contract0, state)
            coinalyze_checks_this_scan += 1
        oi4_ranked = sorted(
            [
                (contract, check.get("oi_change4h"))
                for contract, check in ranked_checks.items()
                if check.get("valid") and check.get("oi_change4h") is not None
            ],
            key=lambda pair: pair[1],
            reverse=True,
        )
        print(
            "OI4h ranked candidates: " +
            (", ".join(f"{contract}={change:+.3f}%" for contract, change in oi4_ranked) or "no valid 4h OI data")
        )
        scan_tickers = sorted(
            tickers,
            key=lambda item: (
                0 if item.get("contract", "") in ranked_checks
                   and ranked_checks[item.get("contract", "")].get("valid")
                   and ranked_checks[item.get("contract", "")].get("oi_change4h") is not None else 1,
                -(float(ranked_checks[item.get("contract", "")].get("oi_change4h"))
                  if item.get("contract", "") in ranked_checks
                  and ranked_checks[item.get("contract", "")].get("oi_change4h") is not None else -1e9),
                -_turnover_rank(item),
            ),
        )
    else:
        scan_tickers = sorted(tickers, key=_turnover_rank, reverse=True)
    for ticker in scan_tickers:
        contract = ticker.get("contract", "")
        if not is_crypto_contract(contract):
            continue
        counts["tickers"] += 1
        try:
            # Gate documents volume_24h_quote as quote-currency turnover; USDT contracts quote in USDT.
            raw_volume = ticker.get("volume_24h_quote") or ticker.get("volume_24h_usd") or 0
            volume24 = float(raw_volume)
            raw_funding = ticker.get("funding_rate")
            # When Coinalyze is configured, validate funding from the aggregated
            # derivatives venues below; do not discard a candidate just because
            # the ticker venue's funding is zero, missing, or disagrees.
            if raw_funding in (None, ""):
                if not COINALYZE_API_KEY:
                    if contract in early_pattern_details: early_filter_status[contract] = "Funding missing"
                    continue
                funding = 0.0
            else:
                funding = float(raw_funding)
                if funding >= 0 and not COINALYZE_API_KEY:
                    if contract in early_pattern_details: early_filter_status[contract] = f"Funding non-negative ({funding*100:+.4f}%)"
                    continue
            if contract in early_pattern_details: early_filter_status[contract] = "Checking aggregate funding and market cap"
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
            # Bound weighted API usage per scan; prioritize earlier (more liquid) ticker rows.
            if contract in ranked_checks:
                coinalyze_check = ranked_checks[contract]
            else:
                if COINALYZE_API_KEY and coinalyze_checks_this_scan >= max_coinalyze_checks_per_scan:
                    counts["quota_skipped"] += 1
                    if contract in early_pattern_details:
                        early_filter_status[contract] = "Coinalyze scan quota reached; skipped this cycle"
                    continue
                coinalyze_checks_this_scan += 1
                coinalyze_check = coinalyze_derivatives_check(contract, state)
            if coinalyze_check.get("configured"):
                if not coinalyze_check.get("valid"):
                    counts["coinalyze_unavailable"] += 1
                    if contract in early_pattern_details:
                        early_filter_status[contract] = "Coinalyze data unavailable"
                    continue
                aggregate_oi = float(coinalyze_check.get("oi_usd") or 0)
                aggregate_funding = float(coinalyze_check["funding_rate"])
                if aggregate_oi <= 0 or aggregate_oi / market_cap < FIB_MIN_OI_TO_MARKET_CAP:
                    counts["oi_rejected"] += 1
                    if contract in early_pattern_details:
                        early_filter_status[contract] = f"Coinalyze OI/MC rejected ({aggregate_oi/market_cap:.1%})"
                    continue
                if aggregate_funding >= 0:
                    if contract in early_pattern_details:
                        early_filter_status[contract] = f"Coinalyze funding not negative ({aggregate_funding*100:+.4f}%)"
                    continue
                oi_usd = aggregate_oi
                funding = aggregate_funding
                oi_change4h = coinalyze_check.get("oi_change4h")
                if contract in early_pattern_details:
                    early_filter_status[contract] = "Coinalyze aggregate OI/funding passed"
            else:
                # Safe fallback only when no Coinalyze API key is configured.
                oi_change4h = None
                stats = get_json(
                    "/futures/usdt/contract_stats",
                    {"contract": contract, "from": int(time.time()) - 600,
                     "interval": "15m", "limit": 3},
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
                candles = closed_candles(contract, "15m", 20)
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
                # Stage 2 uses early-impulse thresholds; Stage 3 still requires closed-candle Fib reaction and confirmations.
                candidate = detect_fib_pump(candles, min_body_pct=0.6, min_total_pump_pct=3.0)
                if not candidate:
                    counts["pump_pattern_rejected"] += 1
                    print(
                        f"{contract}: LEG SPIKE passed volume/OI/funding but no recent "
                        "2-3 candle 15m impulse met body>=0.6% and total-move>=3.0%"
                    )
                    if contract in early_pattern_details: early_filter_status[contract] = "Volume/OI/funding passed; impulse below Stage-2 threshold or too old"
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

            # Refresh filter values every scan, including candidates first stored
            # at Stage 1, so persisted candidates have complete current metadata.
            candidate.update({
                "market_cap": market_cap,
                "volume24": total_market_volume,
                "oi_usd": oi_usd,
                "funding": funding,
            })
            counts["pump_detected"] += 1

            # Use Coinalyze long/short data at the actual closed-candle Fib reaction
            # instead of blocking symbols solely because OKX does not list them.
            if contract in early_pattern_details:
                early_filter_status[contract] = "OI/funding passed; waiting for closed-candle Fib reaction"

            # Stage 2: one stronger watch alert after volume/MC, OI/MC and funding pass.
            # Stage 3 remains the final entry alert after Fib reaction and all confirmations.
            setup_sent = state.setdefault("fib_setup_alerts", {})
            setup_key = str(candidate["end_time"])
            oi4_message = (
                f"{float(oi_change4h):+.2f}%"
                if oi_change4h is not None else "داده در دسترس نیست"
            )
            if setup_sent.get(contract) != setup_key:
                raw_change = ticker.get("change_percentage")
                try:
                    change24_text = f"{float(raw_change):+.2f}%"
                except (TypeError, ValueError):
                    change24_text = "نامشخص"
                watch_message = (
                    "🚀 LEG SPIKE | شکار پامپ\n"
                    "🟠━━━━━━━━━━━━━━━━━━🟠\n"
                    "🔎 مرحله ۲ از ۳ | ستاپ قوی‌تر شد\n"
                    f"🪙 ارز: {contract.replace('_', '/')}\n"
                    f"📈 حرکت ۱۵دقیقه‌ای: {float(candidate['pump_pct']):.2f}% در {int(candidate['body_count'])} کندل\n"
                    f"💵 تغییر ۲۴ساعته: {change24_text} | 💧 حجم/مارکت‌کپ: {volume_mc:.2f}x\n"
                    f"📊 OI/مارکت‌کپ: {oi_usd / market_cap:.1%} | OI چهار‌ساعته: {oi4_message}\n"
                    f"💸 Funding: {funding * 100:+.4f}%\n"
                    f"🎯 سقف حرکت شناسایی‌شده: {float(candidate['high']):.10g}\n"
                    "⏳ این هشدار تأیید قدرت ستاپ است؛ منتظر هشدار رسیدن/شکست سقف باشید و نقطه ورود را خودتان تعیین کنید.\n"
                    "🟠━━━━━━━━━━━━━━━━━━🟠"
                )
                send_message(watch_message)
                setup_sent[contract] = setup_key
                print(f"{contract}: stage-2 setup alert sent")
            # Do not wait for Fibonacci or emit an algorithmic Fib entry. The user will
            # determine the actual entry manually after the top-break alert.
            continue

            # Legacy Fib reaction block intentionally disabled by the continue above.
            # is not mislabeled as a shallower 0.50 entry on the same candle.
            levels = (("0.66", float(candidate["fib66"])), ("0.50", float(candidate["fib50"])))
            for level_name, level in levels:
                alert_key = f'{candidate["end_time"]}:{level_name}'
                # Only one final entry alert per impulse, even if price later
                # reacts at the other Fib level on a subsequent scan.
                previous_fib_alert = str(sent.get(contract, ""))
                if previous_fib_alert.startswith(f'{candidate["end_time"]}:'):
                    continue
                # Check the latest three CLOSED candles after the impulse. A valid
                # Fib rejection can occur while OI/ratio checks are being rate-limited;
                # checking only candles[-1] could permanently miss that confirmation.
                reaction_candle = None
                for closed_candle in reversed(candles[-3:]):
                    try:
                        if int(closed_candle["t"]) <= int(candidate["end_time"]):
                            continue
                        candle_low = float(closed_candle["l"])
                        candle_close = float(closed_candle["c"])
                        if candle_low <= level and candle_close > level and candle_close > low:
                            reaction_candle = closed_candle
                            break
                    except (TypeError, ValueError, KeyError):
                        continue
                if reaction_candle is not None:
                    reaction_close = float(reaction_candle["c"])
                    counts["fib_reactions"] += 1
                    risk = level - low
                    reward = high - level
                    if risk <= 0:
                        continue
                    metrics = coinalyze_spike_metrics(contract, ticker, market_cap, total_market_volume, state)
                    if metrics.get("change24") in (None, "") or float(metrics["change24"]) <= 0:
                        print(f"{contract}: LEG SPIKE rejected; 24h price change is missing or not positive")
                        continue
                    if metrics.get("oi_change4h") is None or metrics["oi_change4h"] <= 0:
                        print(f"{contract}: LEG SPIKE rejected; Coinalyze OI 4h change is missing or not positive")
                        continue
                    if metrics.get("funding_avg") is None or metrics["funding_avg"] >= 0:
                        print(f"{contract}: LEG SPIKE rejected; Coinalyze average funding is missing or non-negative")
                        continue
                    if metrics.get("long15m") is None or metrics["long15m"] <= 50:
                        print(f"{contract}: LEG SPIKE rejected; Coinalyze 15m long share is missing or <=50%")
                        continue
                    if metrics.get("long1h") is None or metrics["long1h"] < 60:
                        print(f"{contract}: LEG SPIKE rejected; Coinalyze 1h long share is missing or below 60%")
                        continue
                    if metrics.get("long1d") is None or metrics["long1d"] > 70:
                        print(f"{contract}: LEG SPIKE rejected; Coinalyze daily long share is missing or exceeds 70%")
                        continue
                    alert_oi = metrics.get("oi_usd")
                    alert_funding = metrics.get("funding_avg")
                    if alert_oi is None or alert_oi <= 0 or alert_oi / market_cap < FIB_MIN_OI_TO_MARKET_CAP:
                        print(f"{contract}: LEG SPIKE rejected; OI/market cap below minimum")
                        continue
                    counts["ratio_pass"] += 1
                    daily_long = metrics.get("long1d")
                    daily_warning = ("⚠️ هشدار شلوغی لانگ روزانه؛ ریسک ازدحام بالا"
                                     if daily_long is not None and daily_long > 60
                                     else "لانگ روزانه زیر آستانه هشدار")
                    message = (
                        "🚀 LEG SPIKE | شکار پامپ\n"
                        "🟢━━━━━━━━━━━━━━━━━━🟢\n"
                        "✅ مرحله ۳ از ۳ | نقطه ورود تأیید شد\n"
                        "🟢━━━━━━━━━━━━━━━━━━🟢\n"
                        f"🎯 🚀 LEG SPIKE — CoinAlayze + Fibonacci\n"
                        f"🪙 {contract.replace('_', '/')} (Gate.io Perpetual)\n"
                        f"واکنش کندل بسته‌شده به فیبو {level_name}؛ ورود پیشنهادی: {level:.10g}\n"
                        f"ورود ۱ (فیبو 0.50): {float(candidate['fib50']):.10g}\n"
                        f"ورود ۲ (فیبو 0.66): {float(candidate['fib66']):.10g}\n"
                        f"سقف پامپ/هدف: {high:.10g} | کف پامپ/حد ضرر: {low:.10g}\n"
                        f"حرکت پامپ: {float(candidate['pump_pct']):.2f}% با {int(candidate['body_count'])} کندل قوی ۵دقیقه‌ای\n"
                        f"نسبت لانگ CoinAlayze: ۱۵دقیقه {metrics['long15m']:.1f}% | ۱ساعت {metrics['long1h']:.1f}% | روزانه {metrics['long1d']:.1f}%\n"
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

    # Both strategies scan independently even when BTC pauses confirmed entries.
    # LEG SPIKE scans the full Gate.io USDT perpetual universe; HYPE/BOS stays top-100 liquid only.
    # The BTC filter gates only final-entry confirmation, not either strategy's early/BOS alerts.
    scan_fib_pullbacks(tickers, state, confirmed_entries=btc_ok)
    if not btc_ok:
        print("BTC filter: final-entry alerts paused; both strategies' early/BOS alerts remain enabled.")

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
                    print(f"{contract}: HYPE/BOS early alert suppressed by its own 1h cooldown")
                else:
                    early_message = (
                    "🟦 HYPE/BOS | استراتژی اصلی\n"
                    f"👀 هشدار زودهنگام؛ هنوز تأیید نشده: {contract.replace('_', '/')} (Gate.io Perpetual)\n"
                    f"حرکت اولیه: {float(candidate['impulse_change']):+.2f}%؛ حجم {float(candidate['impulse_ratio']):.1f} برابر میانگین ۲۰ کندل قبلی\n"
                    f"قیمت فعلیِ کندل بسته‌شده ۵دقیقه: {latest_close}\n"
                    f"کف حرکت اولیه: {float(candidate['impulse_low'])}\n\n"
                    "هنوز سیگنال ورود نیست. ربات منتظر شکست ساختار ۵دقیقه، تأیید روند ۱۵دقیقه و روند ۱ساعته، سپس بررسی OI و Funding می‌ماند."
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
            if latest_time - impulse_time > 14400:
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
                    setup_alerts = state.setdefault("regular_setup_alerts", {})
                    if setup_alerts.get(contract) != candidate["impulse_time"]:
                        setup_message = (
                            "🟦 HYPE/BOS | استراتژی اصلی\n"
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
            # BTC market filter must not suppress this strategy's early warning or BOS alert.
            # It only blocks progression into 15m/1h/derivatives checks and final-entry alerts.
            if not btc_ok:
                print(f"{contract}: BOS recorded; final-entry confirmation paused by BTC filter")
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
            # Short-window OI can move only a few hundredths of a percent.
            # Use a modest positive-growth floor rather than demanding +0.2% in
            # roughly 10 minutes; retain the separate 15m/1h price confirmations.
            if oi_change < 0.03:
                print(f"{contract}: skipped; OI change {oi_change:+.3f}% is below +0.03%")
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
                "🟦 HYPE/BOS | استراتژی اصلی\n"
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
