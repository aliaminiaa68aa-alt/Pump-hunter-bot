"""Stateful 15m breakout -> retest -> confirmation entry engine."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from oi_engine.signal_engine import Signal, _atr, evaluate_snapshot, score_signal_components


PULLBACK_TOLERANCE_ATR = 0.25
INVALIDATION_ATR = 0.60
SETUP_LIFETIME_CANDLES = 4
STOP_BUFFER_ATR = 0.22
MAX_STOP_ATR = 1.50
MIN_STOP_PCT = 0.20
MAX_ENTRY_EXTENSION_ATR = 0.45
ENTRY_LIMIT_OFFSET_ATR = 0.05
MIN_ENTRY_QUALITY = 65
MIN_REJECTION_BODY_RATIO = 0.30
MIN_REJECTION_CLOSE_LOCATION = 0.68
FLOW_LOOKBACK_CANDLES = 3


def _ema(values: list[float], period: int) -> float:
    """EMA over closed candles only; values are supplied oldest to newest."""
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = float(values[0])
    for value in values[1:]:
        result = alpha * float(value) + (1.0 - alpha) * result
    return result


def _recent_order_flow(candles: list[dict], side: str) -> tuple[float, float]:
    """Return aggregate taker-buy ratio and directional candle-volume share."""
    recent = candles[-FLOW_LOOKBACK_CANDLES:]
    quote_total = sum(float(c.get("quote_volume", 0.0)) for c in recent)
    taker_total = sum(float(c.get("taker_buy_quote", 0.0)) for c in recent)
    if quote_total <= 0 or taker_total < 0 or taker_total > quote_total:
        return 0.5, 0.0
    directional_volume = 0.0
    for candle in recent:
        quote = float(candle.get("quote_volume", 0.0))
        is_directional = (
            float(candle["close"]) > float(candle["open"])
            if side == "LONG" else
            float(candle["close"]) < float(candle["open"])
        )
        if is_directional:
            directional_volume += quote
    return taker_total / quote_total, directional_volume / quote_total


def advance_pullback_setup(
    snapshot: Dict[str, Any],
    pending: Optional[Dict[str, Any]] = None,
    required_side: Optional[str] = None,
) -> Tuple[Optional[Signal], Optional[Dict[str, Any]], str]:
    """Create a pending setup on breakout; emit only after a later retest candle confirms."""
    candles = snapshot.get("candles") or []
    oi_values = snapshot.get("oi_values") or []
    if len(candles) < 55 or len(oi_values) < 3:
        return None, pending, f"برای EMA50 حداقل 55 کندل بسته‌شده لازم است ({len(candles)}/55)"

    last = candles[-1]
    close_time = int(last["close_time"])
    atr = _atr(candles)
    if atr <= 0:
        return None, pending, "ATR معتبر نیست"

    if required_side not in {None, "LONG", "SHORT"}:
        required_side = None
    if pending is not None and required_side is not None and pending.get("side") != required_side:
        return None, None, "جهت هشدار جدید با setup قبلی متفاوت است؛ setup قبلی پاک شد"

    if pending is None:
        breakout = evaluate_snapshot(snapshot)
        if breakout is None:
            return None, None, "شکست اولیه هنوز تأیید نشده"
        if required_side is not None and breakout.side != required_side:
            return None, None, f"جهت موتور {breakout.side} با جهت کانال {required_side} هم‌خوان نیست؛ سیگنال رد شد"
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
    # Track the full post-breakout pullback structure, not just the final
    # confirmation candle. This prevents a later shallow candle from moving
    # the stop inside an earlier valid swing extreme.
    pending = dict(pending)
    if side == "LONG":
        pullback_extreme = min(
            float(pending.get("pullback_extreme", last["low"])),
            float(last["low"]),
        )
        pending["pullback_extreme"] = pullback_extreme
        touched = pullback_extreme <= level + tolerance
        valid_depth = pullback_extreme >= level - invalidation
        candle_range = max(float(last["high"]) - float(last["low"]), 1e-12)
        body_ratio = abs(float(last["close"]) - float(last["open"])) / candle_range
        close_location = (float(last["close"]) - float(last["low"])) / candle_range
        confirmed = (
            float(last["close"]) > level
            and float(last["close"]) > float(last["open"])
            and body_ratio >= MIN_REJECTION_BODY_RATIO
            and close_location >= MIN_REJECTION_CLOSE_LOCATION
        )
        aggregate_flow, directional_candle_share = _recent_order_flow(candles, side)
        closes = [float(c["close"]) for c in candles]
        ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
        directional_flow = (
            float(last["taker_buy_quote"]) / max(float(last["quote_volume"]), 1e-12) >= 0.53
            and aggregate_flow >= 0.52
            and directional_candle_share >= 0.55
            and float(last["close"]) > ema20 > ema50
        )
        stop = pullback_extreme - atr * STOP_BUFFER_ATR
    else:
        pullback_extreme = max(
            float(pending.get("pullback_extreme", last["high"])),
            float(last["high"]),
        )
        pending["pullback_extreme"] = pullback_extreme
        touched = pullback_extreme >= level - tolerance
        valid_depth = pullback_extreme <= level + invalidation
        candle_range = max(float(last["high"]) - float(last["low"]), 1e-12)
        body_ratio = abs(float(last["close"]) - float(last["open"])) / candle_range
        close_location = (float(last["high"]) - float(last["close"])) / candle_range
        confirmed = (
            float(last["close"]) < level
            and float(last["close"]) < float(last["open"])
            and body_ratio >= MIN_REJECTION_BODY_RATIO
            and close_location >= MIN_REJECTION_CLOSE_LOCATION
        )
        aggregate_flow, directional_candle_share = _recent_order_flow(candles, side)
        closes = [float(c["close"]) for c in candles]
        ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
        directional_flow = (
            float(last["taker_buy_quote"]) / max(float(last["quote_volume"]), 1e-12) <= 0.47
            and aggregate_flow <= 0.48
            and directional_candle_share >= 0.55
            and float(last["close"]) < ema20 < ema50
        )
        stop = pullback_extreme + atr * STOP_BUFFER_ATR

    if not valid_depth:
        return None, None, "پولبک بیش از حد عمیق شد؛ setup باطل شد"
    if not touched or not confirmed:
        return None, pending, "هنوز پولبک و کندل برگشتی معتبر شکل نگرفته"
    # Do not chase a confirmation candle that has already travelled too far
    # from the broken structure. Keep the setup alive for a fresh retest.
    entry_extension = (
        (float(last["close"]) - level) / atr if side == "LONG"
        else (level - float(last["close"])) / atr
    )
    if entry_extension > MAX_ENTRY_EXTENSION_ATR:
        return None, pending, (
            f"کندل تأیید از نقطه ورود دور شده ({entry_extension:.2f} ATR)؛ "
            "ورود تعقیبی ممنوع، منتظر پولبک تازه می‌مانیم"
        )
    if not directional_flow:
        return None, pending, (
            f"پولبک رخ داد اما تأیید چندکندلی/EMA کامل نیست "
            f"(جریان تجمیعی {aggregate_flow:.1%}، سهم کندل‌های هم‌جهت {directional_candle_share:.1%})؛ "
            "setup تا پایان مهلت حفظ می‌شود"
        )

    oi_change = (float(oi_values[-1]) / float(oi_values[-2]) - 1.0) * 100.0
    if oi_change < 0.35:
        return None, pending, f"پولبک رخ داد اما رشد OI این کندل کافی نیست ({oi_change:+.2f}%)؛ setup تا پایان مهلت حفظ شد"
    quote_volume = float(last["quote_volume"])
    baseline = candles[-21:-1]
    average_volume = sum(float(c["quote_volume"]) for c in baseline) / len(baseline)
    if quote_volume <= 0 or average_volume <= 0 or quote_volume / average_volume < 1.15:
        return None, pending, "پولبک رخ داد اما حجم کندل برگشتی کافی نیست؛ setup تا پایان مهلت حفظ شد"

    # Place a resting limit near the broken structure, not at the confirmation
    # candle close. The signal is sent only after confirmation, then waits for
    # price to revisit this level; this avoids turning every signal into a
    # next-candle market entry after an extended rejection candle.
    entry = (
        level + atr * ENTRY_LIMIT_OFFSET_ATR if side == "LONG"
        else level - atr * ENTRY_LIMIT_OFFSET_ATR
    )
    risk = entry - stop if side == "LONG" else stop - entry
    if risk <= 0:
        return None, None, "ساختار حد ضرر در سمت اشتباه ورود قرار گرفته؛ setup باطل شد"
    min_risk = entry * MIN_STOP_PCT / 100.0
    if risk < min_risk:
        stop = entry - min_risk if side == "LONG" else entry + min_risk
        risk = min_risk
    # Re-check the final stop after applying the minimum-distance floor.
    # A widened stop must never bypass the maximum ATR risk guard.
    if risk > atr * MAX_STOP_ATR:
        return None, pending, "ریسک این کندل برای ورود بیش از حد دور است؛ setup حفظ شد تا فرصت پولبک معتبر بعدی بررسی شود"

    tp1 = entry + risk if side == "LONG" else entry - risk
    tp2 = entry + 2.0 * risk if side == "LONG" else entry - 2.0 * risk
    taker_ratio = float(last["taker_buy_quote"]) / quote_volume
    price_change = (float(last["close"]) / float(candles[-2]["close"]) - 1.0) * 100.0
    movement_strength, entry_quality = score_signal_components(
        candles, side, oi_change, price_change, taker_ratio,
        quote_volume / average_volume, atr, level,
    )
    if entry_quality < MIN_ENTRY_QUALITY:
        return None, pending, (
            f"کیفیت محل ورود پایین است ({entry_quality}/100؛ حداقل "
            f"{MIN_ENTRY_QUALITY})؛ setup حفظ شد تا فرصت بهتر بررسی شود"
        )
    confidence = movement_strength
    signal = Signal(
        symbol=snapshot["symbol"], side=side, entry=entry, stop_loss=stop,
        take_profit_1=tp1, take_profit_2=tp2, oi_change_pct=oi_change,
        price_change_pct=price_change, taker_buy_ratio=taker_ratio,
        volume_ratio=quote_volume / average_volume,
        funding_rate=float(snapshot.get("funding_rate", 0.0)),
        confidence=confidence, candle_close_time=close_time,
        movement_strength=movement_strength, entry_quality=entry_quality,
        setup_level=level, atr_15m=atr, pullback_extreme=pullback_extreme,
        ema20=ema20, ema50=ema50, stop_distance_atr=(risk / atr if atr > 0 else 0.0),
    )
    return signal, None, "پولبک و کندل برگشتی تأیید شدند"
