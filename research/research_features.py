"""Research-only 15m indicators. Not imported by live signal or Telegram code."""
from __future__ import annotations

import math
from typing import Any


def ema_series(values: list[float], period: int = 20) -> list[float | None]:
    """Return an EMA aligned to values; seed with the first-period SMA."""
    if period < 1:
        raise ValueError("period must be >= 1")
    numbers = [float(value) for value in values]
    if any(not math.isfinite(value) for value in numbers):
        raise ValueError("values must be finite")
    result: list[float | None] = [None] * len(numbers)
    if len(numbers) < period:
        return result
    seed = sum(numbers[:period]) / period
    result[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    current = seed
    for index in range(period, len(numbers)):
        current = alpha * numbers[index] + (1.0 - alpha) * current
        result[index] = current
    return result


def candle_volume_pressure(candles: list[dict[str, Any]], lookback: int = 8) -> dict[str, float | int | str | None]:
    """Compare green/red quote turnover and taker buy/sell flow over closed candles.

    A doji is excluded from green/red candle totals. Missing taker data is
    tolerated, but malformed or impossible values are rejected.
    """
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    sample = candles[-lookback:]
    green_quote = red_quote = doji_quote = 0.0
    taker_buy = taker_sell = 0.0
    green_count = red_count = 0

    for candle in sample:
        opening = float(candle["open"])
        closing = float(candle["close"])
        quote = float(candle["quote_volume"])
        buy = float(candle.get("taker_buy_quote", 0.0))
        if not all(math.isfinite(v) for v in (opening, closing, quote, buy)):
            raise ValueError("candle values must be finite")
        if quote < 0 or buy < 0 or buy > quote:
            raise ValueError("invalid quote or taker-buy volume")
        taker_buy += buy
        taker_sell += quote - buy
        if closing > opening:
            green_quote += quote
            green_count += 1
        elif closing < opening:
            red_quote += quote
            red_count += 1
        else:
            doji_quote += quote

    total_directional = green_quote + red_quote
    red_green_ratio = red_quote / green_quote if green_quote > 0 else (math.inf if red_quote else 0.0)
    sell_buy_ratio = taker_sell / taker_buy if taker_buy > 0 else (math.inf if taker_sell else 0.0)

    if red_quote > green_quote * 1.25 and taker_sell > taker_buy * 1.10:
        pressure = "SELL"
    elif green_quote > red_quote * 1.25 and taker_buy > taker_sell * 1.10:
        pressure = "BUY"
    else:
        pressure = "MIXED"

    return {
        "candles": len(sample),
        "green_candles": green_count,
        "red_candles": red_count,
        "green_quote_volume": green_quote,
        "red_quote_volume": red_quote,
        "doji_quote_volume": doji_quote,
        "red_green_volume_ratio": red_green_ratio,
        "taker_buy_quote": taker_buy,
        "taker_sell_quote": taker_sell,
        "taker_sell_buy_ratio": sell_buy_ratio,
        "directional_quote_volume": total_directional,
        "pressure": pressure,
    }


def ema20_stop_candidate(
    side: str,
    entry: float,
    ema20: float,
    atr: float,
    swing_low: float,
    swing_high: float,
    buffer_atr: float = 0.15,
) -> float:
    """Research candidate only: combine EMA, ATR buffer and pullback structure."""
    values = (entry, ema20, atr, swing_low, swing_high, buffer_atr)
    if any(not math.isfinite(float(value)) for value in values):
        raise ValueError("stop inputs must be finite")
    if entry <= 0 or atr <= 0 or buffer_atr < 0 or swing_low > swing_high:
        raise ValueError("invalid stop inputs")
    if side == "LONG":
        stop = min(ema20, swing_low) - atr * buffer_atr
        if stop >= entry:
            raise ValueError("LONG stop must be below entry")
        return stop
    if side == "SHORT":
        stop = max(ema20, swing_high) + atr * buffer_atr
        if stop <= entry:
            raise ValueError("SHORT stop must be above entry")
        return stop
    raise ValueError("side must be LONG or SHORT")
