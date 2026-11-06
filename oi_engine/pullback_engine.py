"""Stateful 15m breakout -> retest -> confirmation entry engine."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from oi_engine.signal_engine import Signal, _atr, evaluate_snapshot, score_signal_components


PULLBACK_TOLERANCE_ATR = 0.35
INVALIDATION_ATR = 0.85
SETUP_LIFETIME_CANDLES = 8
STOP_BUFFER_ATR = 0.35
MAX_STOP_ATR = 2.00
MIN_STOP_PCT = 0.25
MAX_ENTRY_EXTENSION_ATR = 0.95
ENTRY_LIMIT_OFFSET_ATR = 0.02
MIN_ENTRY_QUALITY = 50
MIN_REJECTION_BODY_RATIO = 0.18
MIN_REJECTION_CLOSE_LOCATION = 0.55
EMA_TOLERANCE_ATR = 0.10
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
    # A symbol may not be selected on every 15m candle because the scanner
    # processes a bounded batch. Recover every closed candle since breakout,
    # rather than looking only at the newest candle and missing an intervening
    # retest/swing extreme while the symbol was outside the batch.
    post_breakout = [
        candle for candle in candles
        if int(candle["close_time"]) > int(pending.get("breakout_close_time", 0))
    ]
    if not post_breakout:
        return None, pending, "منتظر بسته‌شدن کندل بعد از شکست هستیم"
    pending = dict(pending)
    if side == "LONG":
        pullback_index = min(
            range(len(post_breakout)),
            key=lambda index: float(post_breakout[index]["low"]),
        )
        observed_low = float(post_breakout[pullback_index]["low"])
        pullback_extreme = min(
            float(pending.get("pullback_extreme", observed_low)),
            min(float(candle["low"]) for candle in post_breakout),
        )
        pending["pullback_extreme"] = pullback_extreme
        touched = pullback_extreme <= level + tolerance
        valid_depth = pullback_extreme >= level - invalidation
        confirmation_candle = None
        for candidate in post_breakout[pullback_index:]:
            candle_range = max(float(candidate["high"]) - float(candidate["low"]), 1e-12)
            candidate_body = abs(float(candidate["close"]) - float(candidate["open"])) / candle_range
            candidate_close_location = (
                float(candidate["close"]) - float(candidate["low"])
            ) / candle_range
            if (
                float(candidate["close"]) > level
                and float(candidate["close"]) > float(candidate["open"])
                and (
                    (
                        candidate_body >= MIN_REJECTION_BODY_RATIO
                        and candidate_close_location >= MIN_REJECTION_CLOSE_LOCATION
                    )
                    # A small-bodied candle can still reject the level when it
                    # closes decisively at the top of its range. Keep the
                    # directional-flow/OI/volume gates below as confirmation.
                    or (candidate_body >= 0.05 and candidate_close_location >= 0.85)
                )
            ):
                confirmation_candle = candidate
        confirmed = confirmation_candle is not None
        if confirmation_candle is not None:
            confirmation_range = max(
                float(confirmation_candle["high"]) - float(confirmation_candle["low"]), 1e-12
            )
            body_ratio = abs(
                float(confirmation_candle["close"]) - float(confirmation_candle["open"])
            ) / confirmation_range
            close_location = (
                float(confirmation_candle["close"]) - float(confirmation_candle["low"])
            ) / confirmation_range
        else:
            candle_range = max(float(last["high"]) - float(last["low"]), 1e-12)
            body_ratio = abs(float(last["close"]) - float(last["open"])) / candle_range
            close_location = (float(last["close"]) - float(last["low"])) / candle_range
        aggregate_flow, directional_candle_share = _recent_order_flow(candles, side)
        closes = [float(c["close"]) for c in candles]
        ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
        directional_flow = (
            float(last["taker_buy_quote"]) / max(float(last["quote_volume"]), 1e-12) >= 0.505
            and aggregate_flow >= 0.47
            and directional_candle_share >= 0.20
            and float(last["close"]) >= ema20 - atr * EMA_TOLERANCE_ATR
        )
        stop = pullback_extreme - atr * STOP_BUFFER_ATR
    else:
        pullback_index = max(
            range(len(post_breakout)),
            key=lambda index: float(post_breakout[index]["high"]),
        )
        observed_high = float(post_breakout[pullback_index]["high"])
        pullback_extreme = max(
            float(pending.get("pullback_extreme", observed_high)),
            max(float(candle["high"]) for candle in post_breakout),
        )
        pending["pullback_extreme"] = pullback_extreme
        touched = pullback_extreme >= level - tolerance
        valid_depth = pullback_extreme <= level + invalidation
        confirmation_candle = None
        for candidate in post_breakout[pullback_index:]:
            candle_range = max(float(candidate["high"]) - float(candidate["low"]), 1e-12)
            candidate_body = abs(float(candidate["close"]) - float(candidate["open"])) / candle_range
            candidate_close_location = (
                float(candidate["high"]) - float(candidate["close"])
            ) / candle_range
            if (
                float(candidate["close"]) < level
                and float(candidate["close"]) < float(candidate["open"])
                and (
                    (
                        candidate_body >= MIN_REJECTION_BODY_RATIO
                        and candidate_close_location >= MIN_REJECTION_CLOSE_LOCATION
                    )
                    # Symmetric strong-close rejection for SHORT setups.
                    or (candidate_body >= 0.05 and candidate_close_location >= 0.85)
                )
            ):
                confirmation_candle = candidate
        confirmed = confirmation_candle is not None
        if confirmation_candle is not None:
            confirmation_range = max(
                float(confirmation_candle["high"]) - float(confirmation_candle["low"]), 1e-12
            )
            body_ratio = abs(
                float(confirmation_candle["close"]) - float(confirmation_candle["open"])
            ) / confirmation_range
            close_location = (
                float(confirmation_candle["high"]) - float(confirmation_candle["close"])
            ) / confirmation_range
        else:
            candle_range = max(float(last["high"]) - float(last["low"]), 1e-12)
            body_ratio = abs(float(last["close"]) - float(last["open"])) / candle_range
            close_location = (float(last["high"]) - float(last["close"])) / candle_range
        aggregate_flow, directional_candle_share = _recent_order_flow(candles, side)
        closes = [float(c["close"]) for c in candles]
        ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
        directional_flow = (
            float(last["taker_buy_quote"]) / max(float(last["quote_volume"]), 1e-12) <= 0.495
            and aggregate_flow <= 0.53
            and directional_candle_share >= 0.20
            and float(last["close"]) <= ema20 + atr * EMA_TOLERANCE_ATR
        )
        stop = pullback_extreme + atr * STOP_BUFFER_ATR

    if not valid_depth:
        return None, None, "پولبک بیش از حد عمیق شد؛ setup باطل شد"
    if not touched:
        distance = (
            (pullback_extreme - level) / atr if side == "LONG"
            else (level - pullback_extreme) / atr
        )
        return None, pending, (
            f"قیمت هنوز محدوده پولبک را لمس نکرده؛ فاصله تا سطح شکست "
            f"{distance:.2f} ATR است"
        )
    if not confirmed:
        return None, pending, (
            f"پولبک انجام شده اما کندل برگشتی معتبر بعد از لمس سطح پیدا نشد "
            f"(بدنه {body_ratio:.0%}؛ حداقل {MIN_REJECTION_BODY_RATIO:.0%}، "
            f"محل بسته‌شدن {close_location:.0%}؛ حداقل {MIN_REJECTION_CLOSE_LOCATION:.0%})"
        )
    # Do not chase a confirmation candle that has already travelled too far
    # from the broken structure. Keep the setup alive for a fresh retest.
    entry_extension = (
        (float(last["close"]) - level) / atr if side == "LONG"
        else (level - float(last["close"])) / atr
    )
    if ((side == "LONG" and float(last["close"]) <= level)
            or (side == "SHORT" and float(last["close"]) >= level)):
        return None, pending, "قیمت دوباره سطح شکست را از دست داده؛ منتظر تثبیت مجدد می‌مانیم"
    if entry_extension > MAX_ENTRY_EXTENSION_ATR:
        return None, pending, (
            f"کندل تأیید از نقطه ورود دور شده ({entry_extension:.2f} ATR)؛ "
            "ورود تعقیبی ممنوع، منتظر پولبک تازه می‌مانیم"
        )
    if not directional_flow:
        return None, pending, (
            f"پولبک رخ داد اما تأیید چندکندلی/EMA کامل نیست "
            f"(جریان آخرین کندل {float(last['taker_buy_quote']) / max(float(last['quote_volume']), 1e-12):.1%}، "
            f"جریان تجمیعی {aggregate_flow:.1%}، سهم کندل‌های هم‌جهت {directional_candle_share:.1%}، "
            f"فاصله EMA20 {(float(last['close']) - ema20) / atr:+.2f} ATR)؛ "
            "setup تا پایان مهلت حفظ می‌شود"
        )

    oi_change = (float(oi_values[-1]) / float(oi_values[-2]) - 1.0) * 100.0
    if oi_change < 0.05:
        return None, pending, f"پولبک رخ داد اما رشد OI این کندل کافی نیست ({oi_change:+.2f}%)؛ setup تا پایان مهلت حفظ شد"
    quote_volume = float(last["quote_volume"])
    baseline = candles[-21:-1]
    average_volume = sum(float(c["quote_volume"]) for c in baseline) / len(baseline)
    if quote_volume <= 0 or average_volume <= 0 or quote_volume / average_volume < 0.90:
        return None, pending, "پولبک رخ داد اما حجم کندل برگشتی کافی نیست؛ setup تا پایان مهلت حفظ شد"

    # Place a resting limit near the broken structure, not at the confirmation
    # candle close. The signal is sent only after confirmation, then waits for
    # price to revisit this level; this avoids turning every signal into a
    # next-candle market entry after an extended rejection candle.
    entry = (
        level + atr * ENTRY_LIMIT_OFFSET_ATR if side == "LONG"
        else level - atr * ENTRY_LIMIT_OFFSET_ATR
    )
    # A resting limit must be on the passive side of the current close:
    # below market for LONG, above market for SHORT. Otherwise it would be
    # marketable at signal time and defeat the wait-for-retest execution model.
    confirmation_close = float(last["close"])
    if ((side == "LONG" and entry >= confirmation_close)
            or (side == "SHORT" and entry <= confirmation_close)):
        return None, pending, "قیمت هنوز از ورود محدود عبور نکرده؛ برای جلوگیری از ورود فوری، منتظر حرکت/پولبک معتبر می‌مانیم"
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
