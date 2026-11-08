"""Independent 4H/1D scanner. Does not import or modify the existing 15m engine."""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BINANCE = "https://data-api.binance.vision"
COINALYZE = "https://api.coinalyze.net/v1"
TIMEFRAMES = {"4h": 4 * 60 * 60 * 1000, "1d": 24 * 60 * 60 * 1000}
MIN_TP1_PCT = 5.0
MIN_TP2_PCT = 8.0
MAX_RISK_PCT = 5.0
STATE_PATH = Path(os.getenv("HIGH_TF_STATE_PATH", ".state/high_tf_sent.json"))
MAX_SYMBOLS = max(10, min(200, int(os.getenv("HIGH_TF_MAX_SYMBOLS", "100"))))
LOG = logging.getLogger("high_timeframe_bot")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PumpHunter-HighTF/2.0"})
_retry = Retry(total=3, connect=3, read=2, backoff_factor=0.6,
               status_forcelist=(429, 500, 502, 503, 504),
               allowed_methods=frozenset(["GET", "POST"]))
_adapter = HTTPAdapter(max_retries=_retry, pool_connections=12, pool_maxsize=12)
SESSION.mount("https://", _adapter)


@dataclass
class Setup:
    symbol: str
    timeframe: str
    side: str
    entry: float
    stop: float
    tp1: float
    tp2: float
    risk_pct: float
    room_to_tp1_pct: float
    room_to_tp2_pct: float
    sweep_level: float
    sweep_time: int
    oi_change_pct: float
    funding_pct: float
    coinalyze_exchanges: str


def get_json(base: str, path: str, params: Optional[dict] = None,
             headers: Optional[dict] = None):
    response = SESSION.get(base + path, params=params, headers=headers, timeout=20)
    response.raise_for_status()
    payload = response.json()
    return payload


def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("sent", {}), dict):
            return data
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as exc:
        LOG.warning("State file unreadable; starting empty state: %s", exc)
    return {"sent": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    tmp.replace(STATE_PATH)


def universe() -> list[tuple[str, float]]:
    info = get_json(BINANCE, "/api/v3/exchangeInfo")
    if not isinstance(info, dict):
        raise RuntimeError("Binance exchangeInfo returned an unexpected payload")
    allowed = set()
    for item in info.get("symbols", []):
        symbol = str(item.get("symbol", "")).upper()
        base = str(item.get("baseAsset", "")).upper()
        if (item.get("status") == "TRADING"
                and item.get("isSpotTradingAllowed", True)
                and item.get("quoteAsset") == "USDT"
                and base not in {"USDC", "BUSD", "TUSD", "FDUSD", "USDP", "DAI"}
                and not base.endswith(("UP", "DOWN", "BULL", "BEAR"))):
            allowed.add(symbol)
    tickers = get_json(BINANCE, "/api/v3/ticker/24hr")
    if not isinstance(tickers, list):
        raise RuntimeError("Binance 24h ticker returned an unexpected payload")
    ranked = []
    for row in tickers:
        symbol = str(row.get("symbol", "")).upper()
        if symbol not in allowed:
            continue
        try:
            quote_volume = float(row.get("quoteVolume", 0))
            last_price = float(row.get("lastPrice", 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(quote_volume) and math.isfinite(last_price) and quote_volume >= 1_000_000 and last_price > 0:
            ranked.append((quote_volume, symbol, last_price))
    ranked.sort(reverse=True)
    return [(symbol, price) for _, symbol, price in ranked[:MAX_SYMBOLS]]


def candles(symbol: str, interval: str) -> list[dict]:
    if interval not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {interval}")
    rows = get_json(BINANCE, "/api/v3/klines",
                    {"symbol": symbol, "interval": interval, "limit": 180})
    if not isinstance(rows, list):
        raise RuntimeError(f"Binance klines payload invalid for {symbol} {interval}")
    now_ms = int(time.time() * 1000)
    duration = TIMEFRAMES[interval]
    result = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 8:
            continue
        try:
            open_time = int(row[0])
            if open_time + duration > now_ms:
                continue
            candle = {
                "open_time": open_time, "open": float(row[1]),
                "high": float(row[2]), "low": float(row[3]),
                "close": float(row[4]), "volume": float(row[5]),
                "close_time": int(row[6]), "quote_volume": float(row[7]),
            }
        except (TypeError, ValueError, OverflowError):
            continue
        if (all(math.isfinite(candle[k]) for k in ("open", "high", "low", "close"))
                and candle["low"] > 0 and candle["high"] >= candle["low"]):
            result.append(candle)
    result.sort(key=lambda c: c["open_time"])
    if len(result) < 65:
        raise ValueError(f"Not enough closed {interval} candles for {symbol}: {len(result)}")
    return result


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError("EMA input too short")
    alpha = 2.0 / (period + 1)
    value = sum(values[:period]) / period
    for value_in in values[period:]:
        value = alpha * value_in + (1 - alpha) * value
    return value


def atr(cs: list[dict], period: int = 14) -> float:
    if len(cs) < period + 1:
        raise ValueError("ATR input too short")
    ranges = []
    for previous, current in zip(cs[-period-1:-1], cs[-period:]):
        ranges.append(max(current["high"] - current["low"],
                          abs(current["high"] - previous["close"]),
                          abs(current["low"] - previous["close"])))
    value = sum(ranges) / len(ranges)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("ATR is invalid")
    return value


def pivot_levels(cs: list[dict], side: str, entry: float) -> list[float]:
    """Confirmed 2-left/2-right swing levels from prior closed candles only."""
    levels = []
    start = max(2, len(cs) - 100)
    end = len(cs) - 2
    for i in range(start, end):
        candle = cs[i]
        left, right = cs[i-2:i], cs[i+1:i+3]
        if side == "LONG":
            level = candle["high"]
            is_pivot = all(level > x["high"] for x in left + right)
            if is_pivot and level >= entry * (1 + MIN_TP2_PCT / 100):
                levels.append(level)
        else:
            level = candle["low"]
            is_pivot = all(level < x["low"] for x in left + right)
            if is_pivot and level <= entry * (1 - MIN_TP2_PCT / 100):
                levels.append(level)
    return sorted(set(levels)) if side == "LONG" else sorted(set(levels), reverse=True)


def analyze_candles(symbol: str, tf: str, cs: list[dict],
                    spot_price: Optional[float] = None) -> Optional[Setup]:
    """A fresh sweep/reclaim on the latest fully closed candle is required."""
    if len(cs) < 65:
        return None
    last = cs[-1]
    prior = cs[-13:-1]
    if len(prior) != 12:
        return None

    e20 = ema([c["close"] for c in cs], 20)
    e50 = ema([c["close"] for c in cs], 50)
    volatility = atr(cs)
    if spot_price and (not math.isfinite(spot_price) or spot_price <= 0):
        return None
    # Do not publish a setup if the market has already run too far from its signal candle.
    if spot_price and abs(spot_price / last["close"] - 1) > max(0.012, 0.5 * volatility / last["close"]):
        return None

    prior_low = min(c["low"] for c in prior)
    prior_high = max(c["high"] for c in prior)
    candle_range = max(last["high"] - last["low"], 1e-12)
    close_location = (last["close"] - last["low"]) / candle_range

    long_sweep = (last["low"] < prior_low and last["close"] > prior_low
                  and last["close"] > last["open"] and close_location >= 0.65
                  and e20 > e50 and last["close"] > e20)
    short_sweep = (last["high"] > prior_high and last["close"] < prior_high
                   and last["close"] < last["open"] and close_location <= 0.35
                   and e20 < e50 and last["close"] < e20)
    if long_sweep == short_sweep:
        return None
    side = "LONG" if long_sweep else "SHORT"
    entry = last["close"]
    stop = last["low"] - 0.20 * volatility if side == "LONG" else last["high"] + 0.20 * volatility
    if side == "LONG" and not 0 < stop < entry:
        return None
    if side == "SHORT" and not stop > entry:
        return None
    risk_pct = abs(entry - stop) / entry * 100
    if not math.isfinite(risk_pct) or risk_pct <= 0 or risk_pct > MAX_RISK_PCT:
        return None

    tp1 = entry * (1 + MIN_TP1_PCT / 100) if side == "LONG" else entry * (1 - MIN_TP1_PCT / 100)
    if (MIN_TP1_PCT / risk_pct) < 1.0:
        return None
    levels = pivot_levels(cs[:-1], side, entry)
    if not levels:
        return None
    tp2 = levels[0]
    room1 = (tp1 / entry - 1) * 100 if side == "LONG" else (1 - tp1 / entry) * 100
    room2 = (tp2 / entry - 1) * 100 if side == "LONG" else (1 - tp2 / entry) * 100
    return Setup(symbol, tf, side, entry, stop, tp1, tp2, risk_pct,
                 room1, room2, prior_low if side == "LONG" else prior_high,
                 last["open_time"], 0.0, 0.0, "")


_COI_CACHE = {"fetched_at": 0, "markets": [], "exchange_names": {}}


def coinalyze_request(path: str, params: Optional[dict] = None):
    key = os.getenv("COINALYZE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("COINALYZE_API_KEY secret is missing")
    response = SESSION.get(COINALYZE + path, params=params,
                           headers={"api_key": key}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected Coinalyze response: {path}")
    return payload


def coinalyze_metrics(symbol: str) -> tuple[float, float, str]:
    """Return cross-exchange OI change and mean funding; metrics are context, not direction filters."""
    now = int(time.time())
    if now - _COI_CACHE["fetched_at"] >= 6 * 3600 or not _COI_CACHE["markets"]:
        _COI_CACHE["markets"] = coinalyze_request("/future-markets")
        exchange_rows = coinalyze_request("/exchanges")
        _COI_CACHE["exchange_names"] = {
            str(row.get("code", "")).upper(): str(row.get("name", row.get("code", "")))
            for row in exchange_rows if row.get("code")
        }
        _COI_CACHE["fetched_at"] = now

    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    exchange_names = _COI_CACHE["exchange_names"]
    def exchange_name(row):
        code = str(row.get("exchange", ""))
        return exchange_names.get(code.upper(), code)
    eligible = [
        row for row in _COI_CACHE["markets"]
        if str(row.get("base_asset", "")).upper() == base
        and str(row.get("quote_asset", "")).upper() == "USDT"
        and row.get("is_perpetual") is True
        and str(row.get("margined", "")).upper() == "STABLE"
        and row.get("symbol")
    ]
    priority = {"BINANCE": 0, "BYBIT": 1, "OKX": 2, "GATEIO": 3, "GATE.IO": 3}
    eligible.sort(key=lambda row: priority.get(exchange_name(row).upper().replace(" ", ""), 10))
    selected, seen = [], set()
    for row in eligible:
        market_symbol = str(row["symbol"])
        if market_symbol not in seen:
            selected.append(row)
            seen.add(market_symbol)
        if len(selected) == 2:
            break
    if len(selected) < 2:
        raise RuntimeError(f"Coinalyze has fewer than two perpetual USDT markets for {symbol}")

    symbols = ",".join(str(row["symbol"]) for row in selected)
    oi_rows = coinalyze_request("/open-interest-history", {
        "symbols": symbols, "interval": "5min", "from": now - 1800,
        "to": now, "convert_to_usd": "true",
    })
    funding_rows = coinalyze_request("/funding-rate", {"symbols": symbols})
    oi_by = {str(row.get("symbol", "")): row for row in oi_rows}
    funding_by = {str(row.get("symbol", "")): row for row in funding_rows}
    old_total = new_total = 0.0
    oi_count = 0
    funding_values = []
    names = []
    for row in selected:
        market_symbol = str(row["symbol"])
        history = sorted(oi_by.get(market_symbol, {}).get("history", []),
                         key=lambda point: int(point.get("t", 0)))
        if len(history) >= 2:
            old_value = float(history[-2].get("c", 0) or 0)
            new_value = float(history[-1].get("c", 0) or 0)
            if old_value > 0 and new_value > 0:
                old_total += old_value
                new_total += new_value
                oi_count += 1
        funding = funding_by.get(market_symbol, {}).get("value")
        if funding not in (None, ""):
            raw = float(funding)
            if not math.isfinite(raw) or abs(raw) > 1.0:
                raise RuntimeError(f"Coinalyze funding sanity check failed for {market_symbol}")
            funding_values.append(raw / 100.0)
        names.append(exchange_name(row))
    if oi_count < 2 or len(funding_values) < 2 or old_total <= 0:
        raise RuntimeError(f"Incomplete cross-exchange Coinalyze OI/Funding data for {symbol}")
    return (new_total / old_total - 1) * 100, sum(funding_values) / len(funding_values) * 100, ",".join(names)


def make_message(s: Setup) -> str:
    action = "خرید اسپات" if s.side == "LONG" else "هشدار SHORT؛ فقط فیوچرز/مارجین"
    risk_reward = s.room_to_tp1_pct / s.risk_pct if s.risk_pct else 0
    return (
        f"📊 HIGH-TF {s.side} {s.symbol}\n"
        f"مرجع قیمت: Binance Spot | مرجع تحلیل: {s.timeframe}\n"
        f"نوع موقعیت: {action}\n"
        f"ورود: {s.entry:.10g}\n"
        f"حد ضرر: {s.stop:.10g} | ریسک: {s.risk_pct:.2f}%\n"
        f"TP1: {s.tp1:.10g} | فاصله: {s.room_to_tp1_pct:.2f}%\n"
        f"TP2 ساختاری: {s.tp2:.10g} | فاصله: {s.room_to_tp2_pct:.2f}%\n"
        f"R:R تا TP1: {risk_reward:.2f}\n"
        f"OI: {s.oi_change_pct:+.3f}% | Funding: {s.funding_pct:+.4f}%\n"
        f"Coinalyze: {s.coinalyze_exchanges}\n"
        f"Liquidity sweep: {s.sweep_level:.10g}\n"
        f"روش: Coinalyze + Price Action + Liquidity Sweep\n"
        f"تحلیل فقط با کندل بسته‌شده؛ سیگنال تضمین سود نیست."
    )


class TelegramDeliveryError(RuntimeError):
    pass


def send_telegram(text: str) -> None:
    token = os.getenv("HIGH_TF_BOT_TOKEN") or os.getenv("BOT_TOKEN")
    chat_id = os.getenv("HIGH_TF_CHAT_ID") or os.getenv("CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Telegram BOT_TOKEN/CHAT_ID secrets are missing")
    response = SESSION.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text}, timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram sendMessage rejected message: {payload.get('description', 'unknown error')}")


def signal_key(s: Setup) -> str:
    return f"{s.symbol}:{s.timeframe}:{s.side}:{s.sweep_time}"


def run_once() -> dict:
    # Validate all credentials before scanning; never silently skip Coinalyze checks.
    if not (os.getenv("COINALYZE_API_KEY") or "").strip():
        raise RuntimeError("COINALYZE_API_KEY secret is missing")
    if not ((os.getenv("HIGH_TF_BOT_TOKEN") or os.getenv("BOT_TOKEN"))
            and (os.getenv("HIGH_TF_CHAT_ID") or os.getenv("CHAT_ID"))):
        raise RuntimeError("Telegram bot token/chat id secrets are missing")

    state = load_state()
    pairs = universe()
    if not pairs:
        raise RuntimeError("Binance returned no eligible liquid USDT Spot symbols")
    stats = {"symbols": len(pairs), "timeframes_checked": 0,
             "technical_setups": 0, "sent": [], "duplicates": 0,
             "analysis_errors": 0, "coinalyze_errors": 0}
    for symbol, current_price in pairs:
        for tf in TIMEFRAMES:
            stats["timeframes_checked"] += 1
            try:
                cs = candles(symbol, tf)
                setup = analyze_candles(symbol, tf, cs, current_price)
                if setup is None:
                    continue
                stats["technical_setups"] += 1
                key = signal_key(setup)
                if key in state.setdefault("sent", {}):
                    stats["duplicates"] += 1
                    continue
                try:
                    oi_change, funding_pct, exchanges = coinalyze_metrics(symbol)
                except Exception as exc:
                    stats["coinalyze_errors"] += 1
                    LOG.warning("%s %s Coinalyze validation failed: %s", symbol, tf, exc)
                    continue
                setup.oi_change_pct = oi_change
                setup.funding_pct = funding_pct
                setup.coinalyze_exchanges = exchanges
                # Persist only after Telegram confirms delivery.
                try:
                    send_telegram(make_message(setup))
                except Exception as exc:
                    raise TelegramDeliveryError(f"Telegram delivery failed for {key}: {exc}") from exc
                state["sent"][key] = {"sent_at": int(time.time()), "setup": asdict(setup)}
                save_state(state)
                stats["sent"].append(key)
                LOG.info("Telegram signal delivered: %s", key)
            except TelegramDeliveryError:
                raise
            except Exception as exc:
                stats["analysis_errors"] += 1
                LOG.exception("%s %s scan error: %s", symbol, tf, exc)
    LOG.info("Scan summary: %s", stats)
    return stats


def main() -> None:
    interval = max(300, int(os.getenv("HIGH_TF_SCAN_INTERVAL_SECONDS", "1800")))
    LOG.info("Independent 4H/1D scanner starting; interval=%ss; symbols=%s",
             interval, MAX_SYMBOLS)
    while True:
        started = time.monotonic()
        try:
            run_once()
        except Exception:
            LOG.exception("High-timeframe scan failed")
        time.sleep(max(5, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    main()
