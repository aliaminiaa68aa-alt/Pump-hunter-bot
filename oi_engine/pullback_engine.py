"""Stateful 15m breakout -> retest -> confirmation entry engine."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from oi_engine.signal_engine import Signal, _atr, evaluate_snapshot


PULLBACK_TOLERANCE_ATR = 0.25
INVALIDATION_ATR = 0.60
SETUP_LIFETIME_CANDLES = 4
STOP_BUFFER_ATR = 0.15
MAX_STOP_ATR = 1.50
MIN_STOP_PCT = 0.20


def advance_pullback_setup(
    snapshot: Dict[str, Any],
    pending: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Signal], Optional[Dict[str, Any]], str]:
    """Create a pending setup on breakout; emit only after a later retest candle confirms."""
    candles = snapshot.get("candles") or []
    oi_values = snapshot.get("oi_values") or []
    if len(candles) < 22 or len(oi_values) < 3:
        return None, pending, "داده کافی برای پولبک موجود نیست"

    last = candles[-1]
    close_time = int(last["close_time"])
    atr = _atr(candles)
    if atr <= 0:
        return None, pending, "ATR معتبر نیست"

    if pending is None:
        breakout = evaluate_snapshot(snapshot)
        if breakout is None:
            return None, None, "شکست اولیه هنوز تأیید نشده"
        prior = candles[-6:-1]
        level = (max(float(c["high"]) for c in prior) if breakout.side == "LONG"
                 else min(float(c["low"]) for c in prior))
        setup = {
            "side": breakout.side,
            "level": level,
            "breakout_close_time": close_time,
            "expires_at_close_time": close_time + SETUP_LIFETIME_CANDLES * 15 * 60 * 1000,
            "breakout_atr": atr,
        }
        return None, setup, (
            f"شکست {breakout.side} ثبت شد؛ منتظر برگشت قیمت به سطح {level:.8g} "
            "و بسته‌شدن کندل تأییدی هستیم"
        )

    side = str(pending.get("side", ""))
    if side not in {"LONG", "SHORT"}:
        return None, None, "جهت setup نامعتبر بود؛ setup پاک شد"
    if close_time <= int(pending.get("breakout_close_time", 0)):
        return None, pending, "منتظر کندل بعد از شکست هستیم"
    if close_time > int(pending.get("expires_at_close_time", 0)):
        return None, None, "مهلت پولبک تمام شد؛ setup منقضی شد"

    level = float(pending["level"])
    tolerance = atr * PULLBACK_TOLERANCE_ATR
    invalidation = atr * INVALIDATION_ATR
    if side == "LONG":
        touched = float(last["low"]) <= level + tolerance
        valid_depth = float(last["low"]) >= level - invalidation
        confirmed = float(last["close"]) > level and float(last["close"]) > float(last["open"])
        directional_flow = float(last["taker_buy_quote"]) / max(float(last["quote_volume"]), 1e-12) >= 0.53
        stop = float(last["low"]) - atr * STOP_BUFFER_ATR
        risk = float(last["close"]) - stop
    else:
        touched = float(last["high"]) >= level - tolerance
        valid_depth = float(last["high"]) <= level + invalidation
        confirmed = float(last["close"]) < level and float(last["close"]) < float(last["open"])
        directional_flow = float(last["taker_buy_quote"]) / max(float(last["quote_volume"]), 1e-12) <= 0.47
        stop = float(last["high"]) + atr * STOP_BUFFER_ATR
        risk = stop - float(last["close"])

    if touched and not valid_depth:
        return None, None, "پولبک بیش از حد عمیق شد؛ setup باطل شد"
    if not touched or not confirmed:
        return None, pending, "هنوز پولبک و کندل برگشتی معتبر شکل نگرفته"
    if not directional_flow:
        return None, None, "پولبک رخ داد اما جریان سفارش هم‌جهت نبود؛ setup رد شد"

    oi_change = (float(oi_values[-1]) / float(oi_values[-2]) - 1.0) * 100.0
    if oi_change < 0.35:
        return None, None, f"پولبک تأیید شد اما رشد OI کافی نیست ({oi_change:+.2f}%)"
    quote_volume = float(last["quote_volume"])
    baseline = candles[-21:-1]
    average_volume = sum(float(c["quote_volume"]) for c in baseline) / len(baseline)
    if quote_volume <= 0 or average_volume <= 0 or quote_volume / average_volume < 1.15:
        return None, None, "پولبک تأیید شد اما حجم کندل برگشتی کافی نیست"

    entry = float(last["close"])
    min_risk = entry * MIN_STOP_PCT / 100.0
    if risk < min_risk:
        stop = entry - min_risk if side == "LONG" else entry + min_risk
        risk = min_risk
    if risk > atr * MAX_STOP_ATR:
        return None, None, "حد ضرر پشت ساختار بیش از حد دور است؛ setup رد شد"

    tp1 = entry + risk if side == "LONG" else entry - risk
    tp2 = entry + 2.0 * risk if side == "LONG" else entry - 2.0 * risk
    taker_ratio = float(last["taker_buy_quote"]) / quote_volume
    price_change = (entry / float(candles[-2]["close"]) - 1.0) * 100.0
    confidence = min(95, 75 + int(min(oi_change, 2.0) * 4)
                     + int(max(0.0, quote_volume / average_volume - 1.15) * 5))
    signal = Signal(
        symbol=snapshot["symbol"], side=side, entry=entry, stop_loss=stop,
        take_profit_1=tp1, take_profit_2=tp2, oi_change_pct=oi_change,
        price_change_pct=price_change, taker_buy_ratio=taker_ratio,
        volume_ratio=quote_volume / average_volume,
        funding_rate=float(snapshot.get("funding_rate", 0.0)),
        confidence=min(95, confidence), candle_close_time=close_time,
    )
    return signal, None, "پولبک و کندل برگشتی تأیید شدند"
