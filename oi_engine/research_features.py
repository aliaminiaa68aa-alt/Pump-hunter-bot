"""Offline-only diagnostics for EMA20 stops and candle-volume pressure.

Nothing in this module is imported by the live signal or Telegram engines.
All inputs must be closed 15m candles.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def ema(values: List[float], period: int = 20) -> Optional[float]:
    """Return the latest recursive EMA; requires at least one finite value."""
    if period < 1 or not values:
        return None
    numbers = [float(value) for value in values]
    if any(value <= 0 for value in numbers):
        raise ValueError("EMA inputs must be positive")
    alpha = 2.0 / (period + 1.0)
    current = numbers[0]
    for value in numbers[1:]:
        current = alpha * value + (1.0 - alpha) * current
    return current


def candle_volume_pressure(
    candles: List[Dict[str, Any]], window: int = 8,
    dominance_threshold: float = 1.25,
) -> Dict[str, Any]:
    """Compare quote volume on green/red candles and taker buy/sell flow."""
    if window < 1 or dominance_threshold <= 1:
        raise ValueError("window must be positive and dominance_threshold must exceed 1")
    recent = candles[-window:]
    green_volume = red_volume = green_count = red_count = 0.0
    buy_quote = sell_quote = 0.0
    for candle in recent:
        opening = float(candle["open"])
        closing = float(candle["close"])
        quote_volume = float(candle["quote_volume"])
        taker_buy = float(candle["taker_buy_quote"])
        if quote_volume < 0 or taker_buy < 0 or taker_buy > quote_volume:
            raise ValueError("Invalid quote/taker-buy volume")
        buy_quote += taker_buy
        sell_quote += quote_volume - taker_buy
        if closing > opening:
            green_volume += quote_volume
            green_count += 1
        elif closing < opening:
            red_volume += quote_volume
            red_count += 1

    candle_ratio = (
        red_volume / green_volume if green_volume > 0
        else (float("inf") if red_volume > 0 else 1.0)
    )
    total_flow = buy_quote + sell_quote
    taker_buy_ratio = buy_quote / total_flow if total_flow > 0 else 0.5
    if candle_ratio >= dominance_threshold and taker_buy_ratio <= 0.47:
        pressure = "SELL"
    elif candle_ratio <= 1.0 / dominance_threshold and taker_buy_ratio >= 0.53:
        pressure = "BUY"
    else:
        pressure = "MIXED"
    return {
        "window": len(recent),
        "green_candles": int(green_count),
        "red_candles": int(red_count),
        "green_quote_volume": green_volume,
        "red_quote_volume": red_volume,
        "red_to_green_volume_ratio": candle_ratio,
        "taker_buy_ratio": taker_buy_ratio,
        "pressure": pressure,
    }


def stop_candidates(
    candles: List[Dict[str, Any]], side: str, entry: float,
    atr_value: float, buffer_atr: float = 0.15,
    max_risk_atr: float = 1.50,
) -> Dict[str, Any]:
    """Return baseline ATR and EMA20+structure stops for offline comparison."""
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    if len(candles) < 20 or entry <= 0 or atr_value <= 0:
        raise ValueError("Need 20 closed candles and positive entry/ATR")
    closes = [float(item["close"]) for item in candles]
    ema20 = ema(closes, 20)
    buffer = atr_value * buffer_atr
    baseline_distance = atr_value * 1.25
    if side == "LONG":
        swing = min(float(item["low"]) for item in candles[-5:])
        ema_stop = ema20 - buffer
        structure_stop = swing - buffer
        combined = min(ema_stop, structure_stop)
        baseline = entry - baseline_distance
        distance = entry - combined
    else:
        swing = max(float(item["high"]) for item in candles[-5:])
        ema_stop = ema20 + buffer
        structure_stop = swing + buffer
        combined = max(ema_stop, structure_stop)
        baseline = entry + baseline_distance
        distance = combined - entry
    valid = 0 < distance <= atr_value * max_risk_atr
    return {
        "ema20": ema20,
        "swing_extreme": swing,
        "baseline_atr_stop": baseline,
        "ema20_buffer_stop": ema_stop,
        "ema20_atr_structure_stop": combined if valid else None,
        "combined_stop_valid": valid,
        "combined_risk_atr": distance / atr_value if distance > 0 else None,
    }
