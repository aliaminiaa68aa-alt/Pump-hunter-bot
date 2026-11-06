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
    movement_strength: int = 0
    entry_quality: int = 0
    # Entry/stop diagnostics captured at signal time; these do not affect execution.
    setup_level: Optional[float] = None
    atr_15m: Optional[float] = None
    # This engine enters on the confirmed breakout close; no pullback extreme
    # exists for this entry model, so keep the field explicitly unavailable.
    pullback_extreme: Optional[float] = None
    ema20: Optional[float] = None
    ema50: Optional[float] = None
    stop_distance_atr: Optional[float] = None
    execution_mode: str = "LIMIT"

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
        "symbol": symbol, "interval": INTERVAL, "limit": 80
    })
    # Binance returns the currently-forming candle last; exclude it.
    candles = candles[:-1]
    if len(candles) < 55:
        raise MarketDataError("Not enough closed candles for EMA50 confirmation")

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
    # Binance OI-statistics timestamps mark the END of the 15m period.
    # Kline close_time is inclusive (boundary - 1 ms), so include the exact
    # next boundary when aligning the just-closed candle.
    oi_cutoff_time = last_close_time + 1
    aligned_rows = [
        row for row in oi_rows
        if int(row.get("timestamp", 0)) <= oi_cutoff_time
    ]
    if len(aligned_rows) < 3:
        raise MarketDataError("Not enough OI samples aligned to closed candle")
    latest_oi_timestamp = int(aligned_rows[-1].get("timestamp", 0))
    if oi_cutoff_time - latest_oi_timestamp > 15 * 60 * 1000:
        raise MarketDataError("OI sample is stale relative to closed candle")
    aligned_rows = aligned_rows[-3:]
    validate_oi_cadence(aligned_rows)
    oi_values = [
        _float(x.get("sumOpenInterest"), "OI value")
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


def _ema(candles: List[dict], period: int) -> Optional[float]:
    """Calculate an EMA only when a full period of closed candles is available."""
    if period <= 0 or len(candles) < period:
        return None
    closes = [float(row["close"]) for row in candles]
    value = sum(closes[:period]) / period
    alpha = 2.0 / (period + 1.0)
    for close in closes[period:]:
        value = close * alpha + value * (1.0 - alpha)
    return value


def diagnose_snapshot(snapshot: Dict[str, Any]) -> List[str]:
    """Explain the exact live entry gates without changing signal rules."""
    candles = snapshot.get("candles") or []
    oi_values = snapshot.get("oi_values") or []
    if len(candles) < 22:
        return [f"کندل بسته‌شده کافی نیست ({len(candles)}/22)"]
    if len(oi_values) < 3:
        return [f"نمونه OI کافی نیست ({len(oi_values)}/3)"]
    last = candles[-1]
    oi_timestamp = snapshot.get("oi_timestamp")
    if oi_timestamp is None:
        return ["زمان نمونه OI موجود نیست"]
    cutoff = int(last["close_time"]) + 1
    if oi_timestamp > cutoff:
        return ["نمونه OI مربوط به آینده است"]
    if cutoff - int(oi_timestamp) > 15 * 60 * 1000:
        return ["نمونه OI قدیمی است"]
    try:
        previous = candles[-2]
        baseline = candles[-21:-1]
        avg_volume = sum(float(x["quote_volume"]) for x in baseline) / len(baseline)
        last_volume = float(last["quote_volume"])
        taker_buy_quote = float(last["taker_buy_quote"])
        close = float(last["close"])
        previous_close = float(previous["close"])
        open_price = float(last["open"])
        oi_previous = float(oi_values[-2])
        oi_latest = float(oi_values[-1])
        prior_high = max(float(x["high"]) for x in candles[-6:-1])
        prior_low = min(float(x["low"]) for x in candles[-6:-1])
        values = (avg_volume, last_volume, taker_buy_quote, close, previous_close,
                  open_price, oi_previous, oi_latest, prior_high, prior_low)
        if not all(math.isfinite(value) for value in values):
            return ["داده قیمت، حجم یا OI غیرمتناهی است"]
        if avg_volume <= 0:
            return ["حجم مبنا نامعتبر است"]
        if last_volume <= 0:
            return ["داده جریان خرید نامعتبر است؛ حجم کندل باید مثبت باشد"]
        if taker_buy_quote < 0 or taker_buy_quote > last_volume:
            return ["داده جریان خرید نامعتبر است؛ نسبت خرید تهاجمی باید بین 0 تا 100٪ باشد"]
        if close <= 0 or previous_close <= 0 or oi_previous <= 0 or oi_latest <= 0:
            return ["داده قیمت یا OI نامعتبر است"]
        volume_ratio = last_volume / avg_volume
        taker_ratio = taker_buy_quote / last_volume
        oi_change = (oi_latest / oi_previous - 1.0) * 100.0
        price_change = (close / previous_close - 1.0) * 100.0
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return ["ساختار داده بازار ناقص یا نامعتبر است"]

    reasons = []
    if oi_change < 0.15:
        reasons.append(f"رشد OI کم است ({oi_change:+.2f}%؛ حداقل 0.15%)")

    long_break = close > prior_high and close > open_price
    short_break = close < prior_low and close < open_price
    active_volume = volume_ratio >= 1.00

    long_fail = []
    short_fail = []
    if price_change < 0.05:
        long_fail.append(f"تغییر قیمت LONG کافی نیست ({price_change:+.2f}%؛ حداقل +0.05%)")
    if taker_ratio < 0.51:
        long_fail.append(f"خرید تهاجمی پایین است ({taker_ratio:.1%}؛ حداقل 51%)")
    if not (active_volume or long_break):
        long_fail.append(f"حجم پایین است ({volume_ratio:.2f}x؛ حداقل 1.00x) و شکست سقف هم تأیید نشده")

    if price_change > -0.05:
        short_fail.append(f"تغییر قیمت SHORT کافی نیست ({price_change:+.2f}%؛ حداکثر -0.05%)")
    if taker_ratio > 0.49:
        short_fail.append(f"فروش تهاجمی کافی نیست ({taker_ratio:.1%}؛ حداکثر 49%)")
    if not (active_volume or short_break):
        short_fail.append(f"حجم پایین است ({volume_ratio:.2f}x؛ حداقل 1.05x) و شکست کف هم تأیید نشده")

    if long_fail:
        reasons.append("LONG: " + "، ".join(long_fail))
    if short_fail:
        reasons.append("SHORT: " + "، ".join(short_fail))
    return reasons or ["شرایط ورود کامل نیست"]

def score_signal_components(
    candles: List[dict], side: str, oi_change: float, price_change: float,
    taker_ratio: float, volume_ratio: float, atr: float, breakout_level: float,
) -> tuple[int, int]:
    """Return separate heuristic movement-strength and entry-location scores.

    These are technical component scores, not calibrated win probabilities.
    """
    oi_score = min(25.0, max(0.0, oi_change) / 2.0 * 25.0)
    volume_score = min(25.0, max(0.0, volume_ratio - 1.0) / 3.0 * 25.0)
    directional_taker = taker_ratio if side == "LONG" else 1.0 - taker_ratio
    flow_score = min(25.0, max(0.0, directional_taker - 0.50) / 0.15 * 25.0)
    momentum_score = min(25.0, abs(price_change) / 2.0 * 25.0)
    movement_strength = round(oi_score + volume_score + flow_score + momentum_score)

    last = candles[-1]
    candle_range = max(float(last["high"]) - float(last["low"]), 1e-12)
    body_ratio = min(1.0, abs(float(last["close"]) - float(last["open"])) / candle_range)
    if side == "LONG":
        close_location = (float(last["close"]) - float(last["low"])) / candle_range
        extension = max(0.0, (float(last["close"]) - breakout_level) / max(atr, 1e-12))
    else:
        close_location = (float(last["high"]) - float(last["close"])) / candle_range
        extension = max(0.0, (breakout_level - float(last["close"])) / max(atr, 1e-12))
    body_score = min(30.0, max(0.0, body_ratio) * 30.0)
    close_score = min(30.0, max(0.0, close_location) * 30.0)
    extension_score = max(0.0, 40.0 * (1.0 - extension / 0.90))
    entry_quality = round(min(100.0, body_score + close_score + extension_score))
    return min(100, max(0, movement_strength)), min(100, max(0, entry_quality))

def evaluate_snapshot(snapshot: Dict[str, Any], required_side: Optional[str] = None) -> Optional[Signal]:
    """Return a directional 15m signal after closed-candle, OI and order-flow confirmation.

    The upstream OI alert supplies direction context, not permission to enter.
    A signal still requires independent price, OI and taker-flow alignment.
    """
    if required_side not in {None, "LONG", "SHORT"}:
        required_side = None
    candles = snapshot["candles"]
    oi_values = snapshot["oi_values"]
    if len(candles) < 22 or len(oi_values) < 3:
        return None

    last = candles[-1]
    oi_timestamp = snapshot.get("oi_timestamp")
    if oi_timestamp is None:
        return None
    oi_cutoff_time = last["close_time"] + 1
    if oi_timestamp > oi_cutoff_time:
        return None
    if oi_cutoff_time - oi_timestamp > 15 * 60 * 1000:
        return None
    previous = candles[-2]
    baseline = candles[-21:-1]
    avg_quote_volume = sum(x["quote_volume"] for x in baseline) / len(baseline)
    if avg_quote_volume <= 0:
        return None

    last_volume = last["quote_volume"]
    taker_buy_quote = last["taker_buy_quote"]
    if (not math.isfinite(last_volume) or not math.isfinite(taker_buy_quote)
            or last_volume <= 0 or taker_buy_quote < 0 or taker_buy_quote > last_volume):
        return None
    volume_ratio = last_volume / avg_quote_volume
    taker_ratio = taker_buy_quote / last_volume
    oi_change = (oi_values[-1] / oi_values[-2] - 1.0) * 100
    price_change = (last["close"] / previous["close"] - 1.0) * 100
    atr = _atr(candles)
    if atr <= 0 or last["close"] <= 0:
        return None

    prior = candles[-6:-1]
    prior_high = max(x["high"] for x in prior)
    prior_low = min(x["low"] for x in prior)
    funding = snapshot["funding_rate"]

    # The channel alert narrows the direction. Keep OI + price + taker flow
    # mandatory, while accepting either expanding volume or a local structure
    # break as the fourth confirmation. Requiring all five checks caused the
    # live scanner to watch valid alerts but almost never produce an entry.
    long_break = last["close"] > prior_high and last["close"] > last["open"]
    short_break = last["close"] < prior_low and last["close"] < last["open"]
    active_volume = volume_ratio >= 1.05

    long_valid = (
        oi_change >= 0.15
        and price_change >= 0.05
        and taker_ratio >= 0.51
        and (active_volume or long_break)
    )
    short_valid = (
        oi_change >= 0.15
        and price_change <= -0.05
        and taker_ratio <= 0.49
        and (active_volume or short_break)
    )

    if required_side == "LONG":
        side = "LONG" if long_valid else None
    elif required_side == "SHORT":
        side = "SHORT" if short_valid else None
    elif long_valid and not short_valid:
        side = "LONG"
    elif short_valid and not long_valid:
        side = "SHORT"
    else:
        side = None
    if side is None:
        return None

    entry = last["close"]
    risk = max(atr * 1.25, entry * 0.002)
    stop_distance_atr = risk / atr
    ema20 = _ema(candles, 20)
    ema50 = _ema(candles, 50)
    setup_level = prior_high if side == "LONG" else prior_low
    if side == "LONG":
        stop = entry - risk
        tp1, tp2 = entry + risk, entry + risk * 2
    else:
        stop = entry + risk
        tp1, tp2 = entry - risk, entry - risk * 2

    # Movement strength and entry location are independent diagnostics.
    movement_strength, entry_quality = score_signal_components(
        candles, side, oi_change, price_change, taker_ratio, volume_ratio,
        atr, prior_high if side == "LONG" else prior_low,
    )
    # Funding crowding is reported, not used as a blind directional veto.
    confidence = movement_strength
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
        movement_strength=movement_strength,
        entry_quality=entry_quality,
        setup_level=setup_level,
        atr_15m=atr,
        ema20=ema20,
        ema50=ema50,
        stop_distance_atr=stop_distance_atr,
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
        f"Funding (snapshot اولیه): {signal.funding_rate:.5%}\n"
        f"قدرت حرکت: {signal.movement_strength}/100 (امتیاز فنی، نه احتمال برد)\n"
        f"کیفیت نقطه ورود: {signal.entry_quality}/100 (امتیاز مستقل؛ نه احتمال برد)"
    )
