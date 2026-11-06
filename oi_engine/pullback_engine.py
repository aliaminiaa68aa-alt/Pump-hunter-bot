"""Stateful 15m breakout -> pre-positioned retest-limit entry engine."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
import time

from oi_engine.signal_engine import Signal, _atr, evaluate_snapshot, score_signal_components


PULLBACK_TOLERANCE_ATR = 0.45
INVALIDATION_ATR = 0.85
SETUP_LIFETIME_CANDLES = 8
STOP_BUFFER_ATR = 0.35
MAX_STOP_ATR = 2.00
MIN_STOP_PCT = 0.25
MAX_ENTRY_EXTENSION_ATR = 1.75
ENTRY_LIMIT_OFFSET_ATR = 0.02
MIN_ENTRY_QUALITY = 45
MIN_REJECTION_BODY_RATIO = 0.08
MIN_REJECTION_CLOSE_LOCATION = 0.35
EMA_TOLERANCE_ATR = 0.18
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
    """Emit a retest LIMIT as soon as a confirmed breakout is detected."""
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
        # Publish the retest LIMIT before price comes back to the broken
        # structure. The previous implementation waited for the retest and
        # rejection candle first, which routinely made the entry arrive late.
        entry = level
        risk = atr * INVALIDATION_ATR
        stop = entry - risk if breakout.side == "LONG" else entry + risk
        min_risk = entry * MIN_STOP_PCT / 100.0
        if risk < min_risk:
            risk = min_risk
            stop = entry - risk if breakout.side == "LONG" else entry + risk
        if risk > atr * MAX_STOP_ATR:
            return None, None, "فاصله ساختاری ورود تا ابطال برای این شکست بیش از حد است"
        tp1 = entry + risk if breakout.side == "LONG" else entry - risk
        tp2 = entry + 2.0 * risk if breakout.side == "LONG" else entry - 2.0 * risk
        limit_signal = Signal(
            symbol=breakout.symbol, side=breakout.side, entry=entry,
            stop_loss=stop, take_profit_1=tp1, take_profit_2=tp2,
            oi_change_pct=breakout.oi_change_pct,
            price_change_pct=breakout.price_change_pct,
            taker_buy_ratio=breakout.taker_buy_ratio,
            volume_ratio=breakout.volume_ratio,
            funding_rate=breakout.funding_rate,
            confidence=breakout.confidence,
            candle_close_time=breakout.candle_close_time,
            movement_strength=breakout.movement_strength,
            entry_quality=breakout.entry_quality,
            setup_level=level, atr_15m=atr, pullback_extreme=None,
            ema20=breakout.ema20, ema50=breakout.ema50,
            stop_distance_atr=(risk / atr if atr > 0 else 0.0),
            execution_mode="LIMIT",
        )
        setup = {
            "side": breakout.side,
            "level": level,
            "breakout_close_time": close_time,
            "expires_at_close_time": close_time + SETUP_LIFETIME_CANDLES * 15 * 60 * 1000,
            "breakout_atr": atr,
            "entry_signal_sent": True,
            "signal": limit_signal.to_dict(),
        }
        return limit_signal, setup, (
            f"شکست {breakout.side} تأیید شد؛ سیگنال LIMIT پیش از پولبک صادر شد. "
            f"ورود روی سقف/کف شکسته‌شده: {level:.8g}"
        )

    side = str(pending.get("side", ""))
    if side not in {"LONG", "SHORT"}:
        return None, None, "جهت setup نامعتبر بود؛ setup پاک شد"
    # Discard setups persisted by the previous delayed-entry implementation.
    # They have no pre-positioned signal and must not generate a stale entry
    # after a retest that already happened.
    if not pending.get("entry_signal_sent"):
        return None, None, "setup قدیمیِ قبل از اصلاح ورود پاک شد؛ منتظر شکست تازه می‌مانیم"
    if close_time <= int(pending.get("breakout_close_time", 0)):
        return None, pending, "منتظر کندل بعد از شکست هستیم"
    if close_time > int(pending.get("expires_at_close_time", 0)):
        return None, None, "مهلت سفارش LIMIT تمام شد؛ setup منقضی شد"

    level = float(pending["level"])
    if pending.get("entry_signal_sent") and isinstance(pending.get("signal"), dict):
        # Do not issue a delayed replacement after price has already tagged
        # the planned retest level. If the external OI/flow confirmation was
        # unavailable at breakout time, this opportunity is skipped rather
        # than sending a stale entry after the pullback.
        post_breakout_for_entry = [
            candle for candle in candles
            if int(candle["close_time"]) > int(pending.get("breakout_close_time", 0))
        ]
        invalidation_distance = atr * INVALIDATION_ATR
        if side == "LONG":
            invalidated = any(float(c["low"]) < level - invalidation_distance
                              for c in post_breakout_for_entry)
            retest_already_tagged = any(float(c["low"]) <= level
                                        for c in post_breakout_for_entry)
        else:
            invalidated = any(float(c["high"]) > level + invalidation_distance
                              for c in post_breakout_for_entry)
            retest_already_tagged = any(float(c["high"]) >= level
                                        for c in post_breakout_for_entry)
        if invalidated:
            return None, None, "سطح شکست تا محدوده ابطال از دست رفت؛ سفارش LIMIT لغو شد"
        if retest_already_tagged:
            return None, None, "قیمت پیش از تأیید مستقل به سطح ورود رسیده؛ سیگنال دیرهنگام صادر نمی‌شود"
        try:
            return Signal(**pending["signal"]), pending, "در انتظار رسیدن قیمت به سفارش LIMIT از پیش اعلام‌شده"
        except (TypeError, ValueError):
            return None, None, "اطلاعات سیگنال ذخیره‌شده معتبر نیست؛ setup پاک شد"


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
                if confirmation_candle is None:
                    confirmation_candle = candidate
                candidate_flow = float(candidate["taker_buy_quote"]) / max(float(candidate["quote_volume"]), 1e-12)
                if candidate_flow >= 0.48:
                    confirmation_candle = candidate
                    break
        # Also accept a two-candle rejection: the first candle reacts above
        # the retest level, and the next candle confirms by closing above its
        # high. This avoids discarding valid retests whose first candle has a
        # modest close location, without weakening the single-candle threshold.
        if confirmation_candle is None:
            post_touch = post_breakout[pullback_index:]
            for first, follow_through in zip(post_touch, post_touch[1:]):
                first_range = max(float(first["high"]) - float(first["low"]), 1e-12)
                first_body = abs(float(first["close"]) - float(first["open"])) / first_range
                follow_range = max(float(follow_through["high"]) - float(follow_through["low"]), 1e-12)
                follow_body = abs(float(follow_through["close"]) - float(follow_through["open"])) / follow_range
                follow_close_location = (
                    float(follow_through["close"]) - float(follow_through["low"])
                ) / follow_range
                if (
                    float(first["close"]) > level
                    and float(first["close"]) > float(first["open"])
                    and first_body >= 0.15
                    and float(follow_through["close"]) > float(first["high"])
                    and float(follow_through["close"]) > float(follow_through["open"])
                    and follow_body >= 0.08
                    and follow_close_location >= 0.52
                ):
                    if confirmation_candle is None:
                        confirmation_candle = follow_through
                    follow_flow = float(follow_through["taker_buy_quote"]) / max(float(follow_through["quote_volume"]), 1e-12)
                    if follow_flow >= 0.48:
                        confirmation_candle = follow_through
                        break
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
        flow_confirmation = confirmation_candle or last
        directional_flow = (
            float(flow_confirmation["taker_buy_quote"]) / max(float(flow_confirmation["quote_volume"]), 1e-12) >= 0.48
            and aggregate_flow >= 0.44
            and directional_candle_share >= 0.15
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
                if confirmation_candle is None:
                    confirmation_candle = candidate
                candidate_flow = float(candidate["taker_buy_quote"]) / max(float(candidate["quote_volume"]), 1e-12)
                if candidate_flow <= 0.52:
                    confirmation_candle = candidate
                    break
        # Symmetric two-candle bearish follow-through after the retest.
        if confirmation_candle is None:
            post_touch = post_breakout[pullback_index:]
            for first, follow_through in zip(post_touch, post_touch[1:]):
                first_range = max(float(first["high"]) - float(first["low"]), 1e-12)
                first_body = abs(float(first["close"]) - float(first["open"])) / first_range
                follow_range = max(float(follow_through["high"]) - float(follow_through["low"]), 1e-12)
                follow_body = abs(float(follow_through["close"]) - float(follow_through["open"])) / follow_range
                follow_close_location = (
                    float(follow_through["high"]) - float(follow_through["close"])
                ) / follow_range
                if (
                    float(first["close"]) < level
                    and float(first["close"]) < float(first["open"])
                    and first_body >= 0.15
                    and float(follow_through["close"]) < float(first["low"])
                    and float(follow_through["close"]) < float(follow_through["open"])
                    and follow_body >= 0.10
                    and follow_close_location >= 0.60
                ):
                    if confirmation_candle is None:
                        confirmation_candle = follow_through
                    follow_flow = float(follow_through["taker_buy_quote"]) / max(float(follow_through["quote_volume"]), 1e-12)
                    if follow_flow <= 0.52:
                        confirmation_candle = follow_through
                        break
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
        flow_confirmation = confirmation_candle or last
        directional_flow = (
            float(flow_confirmation["taker_buy_quote"]) / max(float(flow_confirmation["quote_volume"]), 1e-12) <= 0.52
            and aggregate_flow <= 0.56
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
    if quote_volume <= 0 or average_volume <= 0 or quote_volume / average_volume < 0.85:
        return None, pending, (f"پولبک رخ داد اما حجم کندل برگشتی کافی نیست ({quote_volume / average_volume:.2f}x؛ حداقل 0.85x)؛ setup تا پایان مهلت حفظ شد")

    # A MARKET entry is allowed only from a timestamped, fresh executable quote.
    # A closed 15m candle close is analysis data, not a live fill price.
    planned_entry = (
        level + atr * ENTRY_LIMIT_OFFSET_ATR if side == "LONG"
        else level - atr * ENTRY_LIMIT_OFFSET_ATR
    )
    confirmation_close = float(last["close"])
    quote_value = snapshot.get("current_price")
    price_timestamp = snapshot.get("price_timestamp")
    now_ms = int(time.time() * 1000)
    live_quote_fresh = False
    if quote_value is not None and price_timestamp is not None:
        quote_age_ms = now_ms - int(price_timestamp)
        live_quote_fresh = 0 <= quote_age_ms <= 60_000
    current_price = float(quote_value) if live_quote_fresh else confirmation_close
    execution_mode = "LIMIT"
    crossed_entry = (
        current_price > planned_entry if side == "LONG"
        else current_price < planned_entry
    )
    if crossed_entry:
        if not live_quote_fresh:
            return None, pending, (
                "قیمت ورود از سطح سفارش عبور کرده اما قیمت لحظه‌ای تازه در دسترس نیست؛ "
                "برای جلوگیری از ورود با قیمت قدیمی، منتظر کوتیشن معتبر می‌مانیم"
            )
        market_extension = (
            (current_price - level) / atr if side == "LONG"
            else (level - current_price) / atr
        )
        if market_extension > MAX_ENTRY_EXTENSION_ATR:
            return None, pending, (
                f"قیمت لحظه‌ای از سطح شکست بیش از حد دور شده ({market_extension:.2f} ATR)؛ "
                "ورود مارکت تعقیبی صادر نشد"
            )
        entry = current_price
        execution_mode = "MARKET"
    else:
        entry = planned_entry
        if ((side == "LONG" and entry >= current_price)
                or (side == "SHORT" and entry <= current_price)):
            return None, pending, "ورود محدود هنوز فعال نشده؛ قیمت به ناحیه سفارش نرسیده، منتظر پولبک معتبر می‌مانیم"
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
        execution_mode=execution_mode,
    )
    return signal, None, "پولبک و کندل برگشتی تأیید شدند"
