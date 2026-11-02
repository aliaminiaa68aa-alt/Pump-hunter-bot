"""Standalone 15m Open Interest signal engine.

This module is intentionally independent of the legacy Pump Hunter strategies.
It emits only confirmed LONG/SHORT candidates; it does not send Telegram messages.
Public Binance USD-M Futures endpoints are used for market data.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

import requests

BASE_URL = "https://fapi.binance.com"
INTERVAL = "15m"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "OI-Signal-Engine/1.0"})


class MarketDataError(RuntimeError):
    pass


@dataclass
class Signal:
    symbol: str
    side: str
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    oi_change_pct: float
    price_change_pct: float
    taker_buy_ratio: float
    volume_ratio: float
    funding_rate: float
    confidence: int
    candle_close_time: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _get(path: str, params: Optional[dict] = None) -> Any:
    response = SESSION.get(BASE_URL + path, params=params, timeout=12)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("code") not in (None, 0):
        raise MarketDataError(str(payload))
    return payload


def _float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise MarketDataError(f"Invalid {field}")
    if not math.isfinite(number):
        raise MarketDataError(f"Non-finite {field}")
    return number


def get_active_symbols() -> List[str]:
    """Return liquid active USDT perpetuals, ranked by 24h quote turnover."""
    info = _get("/fapi/v1/exchangeInfo")
    excluded = {
        "USDCUSDT", "BTCDOMUSDT", "DEFIUSDT",
        "1000SHIBUSDT", "1000PEPEUSDT", "1000BONKUSDT",
    }
    active = set()
    for item in info.get("symbols", []):
        symbol = item.get("symbol", "")
        if (
            item.get("status") == "TRADING"
            and item.get("contractType") == "PERPETUAL"
            and item.get("quoteAsset") == "USDT"
            and symbol.endswith("USDT")
            and symbol not in excluded
        ):
            active.add(symbol)

    tickers = _get("/fapi/v1/ticker/24hr")
    ranked = []
    for ticker in tickers:
        symbol = ticker.get("symbol")
        if symbol not in active:
            continue
        try:
            turnover = _float(ticker.get("quoteVolume"), "24h quote volume")
        except MarketDataError:
            continue
        if turnover > 0:
            ranked.append((turnover, symbol))
    ranked.sort(reverse=True)
    return [symbol for _, symbol in ranked]


def validate_oi_cadence(rows: List[dict]) -> None:
    """Reject missing or irregular 15m OI samples."""
    if len(rows) < 3:
        raise MarketDataError("Not enough OI history")
    timestamps = [int(row.get("timestamp", 0)) for row in rows[-3:]]
    if any(current - previous != 15 * 60 * 1000
           for previous, current in zip(timestamps, timestamps[1:])):
        raise MarketDataError("OI samples do not follow exact 15m cadence")


def fetch_snapshot(symbol: str) -> Dict[str, Any]:
    """Fetch closed 15m candles, OI history and current funding."""
    candles = _get("/fapi/v1/klines", {
        "symbol": symbol, "interval": INTERVAL, "limit": 40
    })
    # Binance returns the currently-forming candle last; exclude it.
    candles = candles[:-1]
    if len(candles) < 22:
        raise MarketDataError("Not enough closed candles")

    oi_rows = _get("/futures/data/openInterestHist", {
        "symbol": symbol, "period": INTERVAL, "limit": 6
    })
    if not isinstance(oi_rows, list) or len(oi_rows) < 3:
        raise MarketDataError("Not enough OI history")

    premium = _get("/fapi/v1/premiumIndex", {"symbol": symbol})
    if isinstance(premium, list):
        premium = next((x for x in premium if x.get("symbol") == symbol), None)
    if not premium:
        raise MarketDataError("Funding data unavailable")

    parsed = []
    for row in candles:
        parsed.append({
            "open_time": int(row[0]),
            "open": _float(row[1], "open"),
            "high": _float(row[2], "high"),
            "low": _float(row[3], "low"),
            "close": _float(row[4], "close"),
            "volume": _float(row[5], "volume"),
            "quote_volume": _float(row[7], "quote volume"),
            "taker_buy_quote": _float(row[10], "taker buy quote"),
            "close_time": int(row[6]),
        })

    # Align OI samples to the last CLOSED candle; never mix a newer, still-forming
    # OI interval with a price candle that has already closed.
    oi_rows.sort(key=lambda x: int(x.get("timestamp", 0)))
    last_close_time = parsed[-1]["close_time"]
    aligned_rows = [
        row for row in oi_rows
        if int(row.get("timestamp", 0)) <= last_close_time
    ]
    if len(aligned_rows) < 3:
        raise MarketDataError("Not enough OI samples aligned to closed candle")
    latest_oi_timestamp = int(aligned_rows[-1].get("timestamp", 0))
    if last_close_time - latest_oi_timestamp > 15 * 60 * 1000:
        raise MarketDataError("OI sample is stale relative to closed candle")
    aligned_rows = aligned_rows[-3:]
    validate_oi_cadence(aligned_rows)
    oi_values = [
        _float(x.get("sumOpenInterestValue"), "OI value")
        for x in aligned_rows
    ]
    if any(x <= 0 for x in oi_values):
        raise MarketDataError("Invalid OI values")

    return {
        "symbol": symbol,
        "candles": parsed,
        "oi_values": oi_values,
        "oi_timestamp": latest_oi_timestamp,
        "funding_rate": _float(premium.get("lastFundingRate"), "funding"),
    }


def _atr(candles: List[dict], period: int = 14) -> float:
    recent = candles[-(period + 1):]
    true_ranges = []
    for previous, current in zip(recent, recent[1:]):
        true_ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        ))
    if not true_ranges:
        return 0.0
    return sum(true_ranges) / len(true_ranges)


def evaluate_snapshot(snapshot: Dict[str, Any]) -> Optional[Signal]:
    """Return a final signal only after closed-candle, OI, flow and breakout checks."""
    candles = snapshot["candles"]
    oi_values = snapshot["oi_values"]
    if len(candles) < 22 or len(oi_values) < 3:
        return None

    last = candles[-1]
    oi_timestamp = snapshot.get("oi_timestamp")
    if oi_timestamp is None:
        return None
    if oi_timestamp > last["close_time"]:
        return None
    if last["close_time"] - oi_timestamp > 15 * 60 * 1000:
        return None
    previous = candles[-2]
    baseline = candles[-21:-1]
    avg_quote_volume = sum(x["quote_volume"] for x in baseline) / len(baseline)
    if avg_quote_volume <= 0:
        return None

    volume_ratio = last["quote_volume"] / avg_quote_volume
    taker_ratio = (
        last["taker_buy_quote"] / last["quote_volume"]
        if last["quote_volume"] > 0 else 0.5
    )
    oi_change = (oi_values[-1] / oi_values[-3] - 1.0) * 100
    price_change = (last["close"] / previous["close"] - 1.0) * 100
    atr = _atr(candles)
    if atr <= 0 or last["close"] <= 0:
        return None

    prior = candles[-6:-1]
    prior_high = max(x["high"] for x in prior)
    prior_low = min(x["low"] for x in prior)
    funding = snapshot["funding_rate"]

    # Require fresh positioning, directional aggression and a confirmed 5-candle break.
    long_checks = [
        oi_change >= 0.35,
        price_change >= 0.15,
        taker_ratio >= 0.53,
        volume_ratio >= 1.15,
        last["close"] > prior_high and last["close"] > last["open"],
    ]
    short_checks = [
        oi_change >= 0.35,
        price_change <= -0.15,
        taker_ratio <= 0.47,
        volume_ratio >= 1.15,
        last["close"] < prior_low and last["close"] < last["open"],
    ]

    side = None
    checks = None
    if sum(long_checks) == 5:
        side, checks = "LONG", long_checks
    elif sum(short_checks) == 5:
        side, checks = "SHORT", short_checks
    else:
        return None

    entry = last["close"]
    risk = max(atr * 1.25, entry * 0.002)
    if side == "LONG":
        stop = entry - risk
        tp1, tp2 = entry + risk, entry + risk * 2
    else:
        stop = entry + risk
        tp1, tp2 = entry - risk, entry - risk * 2

    # Funding crowding is reported, not used as a blind directional veto.
    confidence = min(95, 70 + int(max(0, volume_ratio - 1.15) * 8)
                     + int(min(abs(oi_change), 2.0) * 3))
    return Signal(
        symbol=snapshot["symbol"],
        side=side,
        entry=entry,
        stop_loss=stop,
        take_profit_1=tp1,
        take_profit_2=tp2,
        oi_change_pct=oi_change,
        price_change_pct=price_change,
        taker_buy_ratio=taker_ratio,
        volume_ratio=volume_ratio,
        funding_rate=funding,
        confidence=confidence,
        candle_close_time=last["close_time"],
    )


def scan(symbols: Optional[List[str]] = None, max_symbols: int = 120) -> List[Signal]:
    """Scan a bounded symbol list and fail visibly if no market data is usable."""
    universe = symbols if symbols is not None else get_active_symbols()
    selected = universe[:max_symbols]
    results: List[Signal] = []
    successful_snapshots = 0
    skipped = 0
    error_samples: List[str] = []
    for symbol in selected:
        try:
            snapshot = fetch_snapshot(symbol)
            successful_snapshots += 1
            signal = evaluate_snapshot(snapshot)
            if signal is not None:
                results.append(signal)
        except (requests.RequestException, MarketDataError, KeyError, TypeError, ValueError) as exc:
            skipped += 1
            if len(error_samples) < 3:
                error_samples.append(f"{symbol}: {type(exc).__name__}: {exc}")
        except Exception as exc:
            # Keep a malformed symbol from stopping the scan, but retain diagnostics.
            skipped += 1
            if len(error_samples) < 3:
                error_samples.append(f"{symbol}: unexpected {type(exc).__name__}: {exc}")
        time.sleep(0.03)

    print(
        f"OI market-data scan: attempted={len(selected)}, "
        f"usable_snapshots={successful_snapshots}, skipped={skipped}, "
        f"confirmed_signals={len(results)}"
    )
    for detail in error_samples:
        print(f"OI scan diagnostic: {detail}")

    if selected and successful_snapshots == 0:
        details = "; ".join(error_samples) or "No usable market-data response"
        raise MarketDataError(
            f"All {len(selected)} market-data requests failed; refusing to report "
            f"an empty signal scan as a valid no-signal result. Details: {details}"
        )
    return sorted(results, key=lambda item: item.confidence, reverse=True)


def format_signal(signal: Signal) -> str:
    direction = "خرید LONG" if signal.side == "LONG" else "فروش SHORT"
    return (
        f"🚨 سیگنال نهایی {direction}\n"
        f"ارز: {signal.symbol}\n"
        f"ورود: {signal.entry:.8g}\n"
        f"حد ضرر: {signal.stop_loss:.8g}\n"
        f"هدف ۱: {signal.take_profit_1:.8g}\n"
        f"هدف ۲: {signal.take_profit_2:.8g}\n"
        f"تغییر OI: {signal.oi_change_pct:+.2f}%\n"
        f"تغییر قیمت ۱۵دقیقه: {signal.price_change_pct:+.2f}%\n"
        f"نسبت خرید تهاجمی: {signal.taker_buy_ratio:.1%}\n"
        f"حجم نسبت به میانگین: {signal.volume_ratio:.2f}x\n"
        f"Funding: {signal.funding_rate:.5%}\n"
        f"امتیاز شرایط: {signal.confidence}/100"
    )
