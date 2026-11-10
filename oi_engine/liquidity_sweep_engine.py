"""New strategy: Coinalyze context + price action + liquidity sweep."""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

from oi_engine.signal_engine import Signal, _atr, score_signal_components

MIN_CONFIRMED_ENTRY_RR = 2.0


def evaluate_liquidity_sweep(
    snapshot: Dict[str, Any], metrics: Dict[str, Any]
) -> Tuple[Optional[Signal], str]:
    """Create an entry only after a closed 15m candle sweeps and reclaims liquidity.

    LONG: take the prior swing low, then close bullish back above it.
    SHORT: take the prior swing high, then close bearish back below it.
    Coinalyze OI expansion and taker flow must agree with the reversal.
    """
    candles = snapshot.get("candles") or []
    if len(candles) < 30:
        return None, "برای تشخیص ساختار و نقدینگی، حداقل ۳۰ کندل بسته‌شده لازم است"

    last = candles[-1]
    try:
        oi_change = float(metrics["oi_change_15m_window_pct"])
        taker_ratio = float(metrics["taker_buy_ratio"])
        funding = float(metrics["funding_rate"])
        entry = float(last["close"])
        open_price = float(last["open"])
        high = float(last["high"])
        low = float(last["low"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, "داده‌های Coinalyze یا OHLCV ناقص است"

    values = (oi_change, taker_ratio, funding, entry, open_price, high, low)
    if not all(math.isfinite(value) for value in values):
        return None, "داده‌های بازار معتبر نیست"
    if not (0.0 <= taker_ratio <= 1.0) or min(entry, open_price, high, low) <= 0:
        return None, "داده قیمت یا جریان سفارش نامعتبر است"

    atr = _atr(candles)
    if not math.isfinite(atr) or atr <= 0:
        return None, "ATR معتبر برای تعیین حد ضرر وجود ندارد"

    # Search the latest six CLOSED candles for a completed sweep/reclaim.
    # If the sweep occurred one or two candles earlier, require the newest
    # closed candle to continue away from the reclaimed level before entering.
    setup = None
    sweep_seen = False
    continuation_seen = False
    for sweep_index in range(len(candles) - 1, max(10, len(candles) - 7), -1):
        sweep = candles[sweep_index]
        previous = candles[sweep_index - 12:sweep_index]
        if len(previous) != 12:
            continue
        try:
            sweep_open = float(sweep["open"])
            sweep_high = float(sweep["high"])
            sweep_low = float(sweep["low"])
            sweep_close = float(sweep["close"])
            prior_low = min(float(row["low"]) for row in previous)
            prior_high = max(float(row["high"]) for row in previous)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue

        span = max(sweep_high - sweep_low, 1e-12)
        long_sweep = (
            sweep_low < prior_low and sweep_close > prior_low and sweep_close > sweep_open
            and (sweep_close - sweep_low) / span >= 0.60
        )
        short_sweep = (
            sweep_high > prior_high and sweep_close < prior_high and sweep_close < sweep_open
            and (sweep_high - sweep_close) / span >= 0.60
        )
        if long_sweep and not short_sweep:
            sweep_seen = True
            continued = (
                entry > prior_low and
                (sweep_index == len(candles) - 1 or (entry > sweep_close and entry > open_price))
            )
            if continued:
                continuation_seen = True
                setup = ("LONG", prior_low, sweep_low, len(candles) - 1 - sweep_index)
                break
        elif short_sweep and not long_sweep:
            sweep_seen = True
            continued = (
                entry < prior_high and
                (sweep_index == len(candles) - 1 or (entry < sweep_close and entry < open_price))
            )
            if continued:
                continuation_seen = True
                setup = ("SHORT", prior_high, sweep_high, len(candles) - 1 - sweep_index)
                break

    if setup is None:
        if sweep_seen and not continuation_seen:
            return None, (
                "شکار نقدینگی در ۶ کندل اخیر دیده شد، اما کندل بسته‌شده بعدی "
                "ادامه برگشت را تأیید نکرد؛ برای جلوگیری از ورود زودهنگام سیگنال صادر نشد"
            )
        return None, "در ۶ کندل بسته‌شده اخیر شکست معتبر سقف/کف ۱۲ کندل قبل و بازپس‌گیری سطح دیده نشد"

    side, swept_level, sweep_extreme, sweep_age = setup
    if side == "LONG":
        if oi_change <= 0 or taker_ratio <= 0.50:
            return None, "نقدینگی کف زده شده، اما OI و جریان خرید Coinalyze برگشت LONG را تأیید نمی‌کنند"
    else:
        if oi_change <= 0 or taker_ratio >= 0.50:
            return None, "نقدینگی سقف زده شده، اما OI و جریان فروش Coinalyze برگشت SHORT را تأیید نمی‌کنند"

    # Prepare a LIMIT pullback at the reclaimed liquidity level. The signal is
    # published before touch; the live monitor is responsible for confirming fill.
    entry = float(swept_level)
    if side == "LONG" and entry >= float(snapshot.get("live_price", entry + 1e-12)):
        return None, "قیمت ورود پولبک قبلاً لمس یا از دست رفته؛ سیگنال دیرهنگام صادر نشد"
    if side == "SHORT" and entry <= float(snapshot.get("live_price", entry - 1e-12)):
        return None, "قیمت ورود پولبک قبلاً لمس یا از دست رفته؛ سیگنال دیرهنگام صادر نشد"

    # Structural stop remains beyond the actual sweep extreme with an ATR buffer.
    buffer = max(atr * 0.10, entry * 0.0005)
    stop = sweep_extreme - buffer if side == "LONG" else sweep_extreme + buffer
    risk = entry - stop if side == "LONG" else stop - entry
    if risk <= 0 or risk / entry > 0.04:
        return None, "فاصله حد ضرر ساختاری نامعتبر یا بیش از ۴٪ است"

    # Find the nearest two confirmed opposing swing levels as structural targets.
    # Only closed candles are considered; no arbitrary fixed-R targets.
    levels = []
    history = candles[-51:-1]
    for index in range(1, len(history) - 1):
        row = history[index]
        try:
            level = float(row["high"] if side == "LONG" else row["low"])
            left = float(history[index - 1]["high"] if side == "LONG" else history[index - 1]["low"])
            right = float(history[index + 1]["high"] if side == "LONG" else history[index + 1]["low"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        pivot = level > left and level >= right if side == "LONG" else level < left and level <= right
        beyond_entry = level > entry + risk * 0.25 if side == "LONG" else level < entry - risk * 0.25
        if pivot and beyond_entry:
            levels.append(level)
    levels = sorted(set(levels), reverse=(side == "SHORT"))
    if len(levels) < 2:
        return None, "دو سطح نقدینگی/مقاومت معتبر برای تعیین اهداف ساختاری پیدا نشد"
    tp1, tp2 = levels[0], levels[1]
    if side == "LONG" and not stop < entry < tp1 < tp2:
        return None, "ترتیب سطوح ساختاری LONG معتبر نیست"
    if side == "SHORT" and not stop > entry > tp1 > tp2:
        return None, "ترتیب سطوح ساختاری SHORT معتبر نیست"
    risk_reward = (tp1 - entry) / risk if side == "LONG" else (entry - tp1) / risk
    if not math.isfinite(risk_reward) or risk_reward + 1e-9 < MIN_CONFIRMED_ENTRY_RR:
        return None, (
            f"نسبت سود به ریسک تا هدف اول {risk_reward:.2f}R است؛ "
            f"حداقل مجاز {MIN_CONFIRMED_ENTRY_RR:.2f}R است"
        )
    previous_close = float(candles[-2]["close"])
    price_change = (float(last["close"]) / previous_close - 1.0) * 100.0
    baseline = candles[-21:-1]
    avg_volume = sum(float(row["quote_volume"]) for row in baseline) / len(baseline)
    volume_ratio = float(last["quote_volume"]) / avg_volume if avg_volume > 0 else 0.0
    # A modest floor blocks very weak sweeps without requiring a volume spike.
    if not math.isfinite(volume_ratio) or volume_ratio < 0.50:
        return None, (
            f"شکار نقدینگی تأیید شد، اما حجم کندل فقط {volume_ratio:.2f}x میانگین است؛ "
            "حداقل قابل قبول 0.50x است"
        )
    movement_strength, entry_quality = score_signal_components(
        candles, side, oi_change, price_change, taker_ratio, volume_ratio, atr, swept_level
    )
    signal = Signal(
        symbol=str(snapshot.get("symbol", "")), side=side, entry=entry,
        stop_loss=stop, take_profit_1=tp1, take_profit_2=tp2,
        oi_change_pct=oi_change, price_change_pct=price_change,
        taker_buy_ratio=taker_ratio, volume_ratio=volume_ratio,
        funding_rate=funding, confidence=movement_strength,
        candle_close_time=int(last["close_time"]),
        movement_strength=movement_strength, entry_quality=entry_quality,
        setup_level=swept_level, atr_15m=atr,
        pullback_extreme=sweep_extreme, stop_distance_atr=risk / atr,
        execution_mode="LIMIT",
    )
    age_text = "همین کندل" if sweep_age == 0 else f"{sweep_age} کندل قبل"
    return signal, (
        f"Liquidity Sweep {side}: سطح {swept_level:.8g} در {age_text} شکسته و پس گرفته شد؛ "
        "کندل بسته‌شده فعلی ادامه برگشت را تأیید کرد"
    )
