"""Higher-timeframe trend gate for Pump Hunter.

All OHLCV comes from Binance Spot. A direction is accepted only when the
1h EMA structure and confirmed swing sequence agree; 15m must independently
show the same swing direction before liquidity-sweep analysis is allowed.
"""
from __future__ import annotations

import math
import requests

BASES = ("https://data-api.binance.vision", "https://api.binance.com")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Pump-Hunter-Trend-Engine/1.0"})


class TrendDataError(RuntimeError):
    pass


def _fetch_timeframe(symbol: str, timeframe: str, limit: int = 240) -> list[dict]:
    errors = []
    for base in BASES:
        try:
            response = SESSION.get(
                base + "/api/v3/klines",
                params={"symbol": symbol, "interval": timeframe, "limit": limit},
                timeout=12,
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or len(rows) < 205:
                raise TrendDataError("Binance Spot higher-timeframe history is insufficient")
            # Exclude the still-forming final candle.
            closed = rows[:-1]
            result = []
            for row in closed:
                values = [float(row[i]) for i in (1, 2, 3, 4)]
                if not all(math.isfinite(v) and v > 0 for v in values):
                    raise TrendDataError("Invalid Binance Spot higher-timeframe candle")
                result.append({
                    "open": values[0], "high": values[1],
                    "low": values[2], "close": values[3],
                })
            return result
        except (requests.RequestException, ValueError, TypeError, TrendDataError) as exc:
            errors.append(f"{base}: {type(exc).__name__}: {exc}")
    raise TrendDataError("Binance Spot higher-timeframe candles unavailable: " + " | ".join(errors))


def _ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise TrendDataError(f"Need {period} closed candles for EMA")
    value = sum(values[:period]) / period
    alpha = 2.0 / (period + 1.0)
    for item in values[period:]:
        value = item * alpha + value * (1.0 - alpha)
    return value


def _swings(candles: list[dict]) -> tuple[list[float], list[float]]:
    highs, lows = [], []
    # One closed candle on each side confirms a pivot. This keeps the
    # structure testable on 15m history while excluding the live right edge.
    for i in range(1, len(candles) - 1):
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        if high > float(candles[i-1]["high"]) and high > float(candles[i+1]["high"]):
            highs.append(high)
        if low < float(candles[i-1]["low"]) and low < float(candles[i+1]["low"]):
            lows.append(low)
    return highs[-2:], lows[-2:]


def _structure_direction(candles: list[dict]) -> str:
    highs, lows = _swings(candles)
    if len(highs) < 2 or len(lows) < 2:
        return "NEUTRAL"
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "LONG"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "SHORT"
    return "NEUTRAL"


def analyze_trend_timeframe(symbol: str, timeframe: str) -> dict:
    if timeframe not in {"1h", "4h", "1d"}:
        raise ValueError(f"Unsupported trend timeframe: {timeframe}")
    candles = _fetch_timeframe(symbol, timeframe)
    closes = [row["close"] for row in candles]
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    ema50_previous = _ema(closes[:-3], 50)
    close = closes[-1]
    structure = _structure_direction(candles[-100:])
    if close > ema50 > ema200 and ema50 > ema50_previous and structure == "LONG":
        direction = "LONG"
    elif close < ema50 < ema200 and ema50 < ema200 and ema50 < ema50_previous and structure == "SHORT":
        direction = "SHORT"
    else:
        direction = "NEUTRAL"
    return {
        "timeframe": timeframe,
        "direction": direction,
        "close": close,
        "ema50": ema50,
        "ema200": ema200,
        "structure": structure,
    }


def analyze_trend_1h(symbol: str) -> dict:
    return analyze_trend_timeframe(symbol, "1h")


def analyze_trend_4h_1d(symbol: str) -> dict:
    """Fetch 4h and daily structure independently for multi-timeframe context."""
    return {
        "4h": analyze_trend_timeframe(symbol, "4h"),
        "1d": analyze_trend_timeframe(symbol, "1d"),
    }


def confirm_trend_15m(candles: list[dict], direction: str) -> bool:
    if direction not in {"LONG", "SHORT"} or len(candles) < 30:
        return False
    structure = _structure_direction(candles[-80:])
    closes = [float(row["close"]) for row in candles]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50) if len(closes) >= 50 else None
    last = closes[-1]
    if direction == "LONG":
        return structure == "LONG" and last > ema20 and (ema50 is None or ema20 > ema50)
    return structure == "SHORT" and last < ema20 and (ema50 is None or ema20 < ema50)
