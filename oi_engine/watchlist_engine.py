"""Alert-driven watchlist scanner with independent Coinalyze confirmation."""
from __future__ import annotations
import time
import math
import requests
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from oi_engine.coinalyze_client import CoinalyzeClient, CoinalyzeError
from oi_engine.signal_engine import MarketDataError, Signal, _atr, fetch_snapshot
from oi_engine.liquidity_sweep_engine import evaluate_liquidity_sweep

DATA_ERROR_RETRY_SECONDS = 300


def coinalyze_rejection_reasons(signal: Signal, metrics: Dict[str, Any]) -> List[str]:
    """Return precise derivatives-gate failures for diagnostics; never loosen a gate."""
    try:
        oi_change = float(metrics["oi_change_15m_window_pct"])
        taker_ratio = float(metrics["taker_buy_ratio"])
        funding = float(metrics["funding_rate"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return ["داده اصلی OI، جریان خرید/فروش یا Funding ناقص است"]

    if not all(math.isfinite(value) for value in (oi_change, taker_ratio, funding)):
        return ["داده OI، جریان یا Funding معتبر نیست"]
    reasons = []
    if oi_change < 0.30:
        reasons.append(f"رشد OI کافی نیست ({oi_change:+.2f}٪؛ حداقل +0.30٪)")
    if not 0.0 <= taker_ratio <= 1.0:
        reasons.append("نسبت جریان خرید/فروش خارج از محدوده معتبر است")

    raw_lsr = metrics.get("long_short_ratio")
    long_share = None
    if raw_lsr is not None:
        try:
            ratio = float(raw_lsr)
        except (TypeError, ValueError, OverflowError):
            reasons.append("نسبت لانگ/شورت نامعتبر است")
            ratio = None
        if ratio is not None:
            if not math.isfinite(ratio) or ratio <= 0:
                reasons.append("نسبت لانگ/شورت نامعتبر است")
            else:
                long_share = ratio / (1.0 + ratio)

    if signal.side == "LONG":
        if taker_ratio < 0.55:
            reasons.append(f"جریان خرید کافی نیست ({taker_ratio:.1%}؛ حداقل 55٪)")
        if funding > 0.0005:
            reasons.append(f"Funding برای LONG بیش از حد مثبت است ({funding:+.5%})")
        if long_share is not None and long_share > 0.70:
            reasons.append(f"ازدحام لانگ بالاست ({long_share:.1%}؛ حداکثر 70٪)")
    elif signal.side == "SHORT":
        short_share = None if long_share is None else 1.0 - long_share
        if taker_ratio > 0.45:
            reasons.append(f"جریان فروش کافی نیست ({1.0-taker_ratio:.1%}؛ حداقل 55٪)")
        if funding < -0.0005:
            reasons.append(f"Funding برای SHORT بیش از حد منفی است ({funding:+.5%})")
        if short_share is not None and short_share > 0.70:
            reasons.append(f"ازدحام شورت بالاست ({short_share:.1%}؛ حداکثر 70٪)")
    else:
        reasons.append("جهت سیگنال نامعتبر است")
    return reasons


def coinalyze_confirms(signal: Signal, metrics: Dict[str, Any]) -> bool:
    """Require fresh core derivatives evidence; treat ancillary metrics as optional."""
    return not coinalyze_rejection_reasons(signal, metrics)


def source_oi_priority(source_text: str) -> Dict[str, Any]:
    """Extract labeled upstream OI changes for ranking only, never as an entry gate."""
    import re

    values = {}
    if not isinstance(source_text, str):
        source_text = ""
    for timeframe in ("15m", "1h"):
        # Prefer explicit timeframe + OI labels, e.g.
        # "15m: OI +5.30%, price +2.17%" or "OI (15m): +5.30%".
        patterns = (
            rf"(?:^|\b){timeframe}\s*[:：-]\s*OI\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)\s*%",
            rf"\bOI\s*(?:change\s*)?(?:\[\s*{timeframe}\s*\]|\(\s*{timeframe}\s*\)|{timeframe})\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)\s*%",
            rf"\bOI\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)\s*%\s*\(\s*{timeframe}\s*\)",
        )
        match = next((m for pattern in patterns if (m := re.search(pattern, source_text, re.I))), None)
        # Older compact feed: "🟩+5.30% 🟩+2.17% (15m)".
        # Its first percentage is OI; the second is price.
        if not match:
            match = re.search(
                rf"([+-]?\d+(?:\.\d+)?)%\s+[🟩🟥🟢🔴]?\s*[+-]?\d+(?:\.\d+)?%\s*\(\s*{timeframe}\s*\)",
                source_text,
                re.I,
            )
        if match:
            try:
                value = float(match.group(1))
                if math.isfinite(value):
                    values[timeframe] = value
            except (TypeError, ValueError):
                pass
    oi_15m = values.get("15m")
    oi_1h = values.get("1h")
    # Strong 15m expansion gets the greatest weight; 1h confirms persistence.
    score = 0.0
    if oi_15m is not None:
        score += min(10.0, max(0.0, oi_15m) * 1.2)
    if oi_1h is not None:
        score += min(10.0, max(0.0, oi_1h) * 0.6)
    tier = ("🔥 بسیار قوی" if score >= 10 else
            "🟢 قوی" if score >= 6 else
            "🟡 معمولی")
    return {
        "source_oi_15m_pct": oi_15m,
        "source_oi_1h_pct": oi_1h,
        "oi_priority_score": round(score, 2),
        "oi_priority_tier": tier,
    }


def signal_price_geometry_rejection(signal: Signal, min_tp1_distance_pct: float = 1.0) -> str | None:
    """Fail closed on malformed levels or a first target that is too close."""
    try:
        entry = float(signal.entry)
        stop = float(signal.stop_loss)
        tp1 = float(signal.take_profit_1)
        tp2 = float(signal.take_profit_2)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return "قیمت ورود، حد ضرر یا اهداف معتبر نیست"
    if not all(math.isfinite(value) and value > 0 for value in (entry, stop, tp1, tp2)):
        return "قیمت ورود، حد ضرر یا اهداف معتبر نیست"
    side = str(signal.side).upper()
    if side == "LONG":
        if not stop < entry < tp1 < tp2:
            return "چیدمان قیمت LONG نامعتبر است؛ باید حد ضرر < ورود < هدف ۱ < هدف ۲ باشد"
    elif side == "SHORT":
        if not stop > entry > tp1 > tp2:
            return "چیدمان قیمت SHORT نامعتبر است؛ باید حد ضرر > ورود > هدف ۱ > هدف ۲ باشد"
    else:
        return "جهت سیگنال نامعتبر است"
    tp1_pct = abs(tp1 - entry) / entry * 100.0
    if tp1_pct + 1e-9 < min_tp1_distance_pct:
        return (
            f"فاصله هدف اول فقط {tp1_pct:.2f}٪ است؛ "
            f"حداقل مجاز {min_tp1_distance_pct:.2f}٪ است"
        )
    return None


def format_confirmed_signal(signal: Signal, metrics: Dict[str, Any]) -> str:
    """Compact, colorful Telegram card; keep only actionable signal details."""
    import html

    symbol = html.escape(str(signal.symbol))
    if signal.side == "LONG":
        title = "🟢 <b>LONG</b>"
        side_label = "خرید"
    else:
        title = "🔴 <b>SHORT</b>"
        side_label = "فروش"

    engine_names = {"LIQUIDITY_SWEEP": "Coinalyze + Price Action + Liquidity Sweep"}
    engine = engine_names.get(str(metrics.get("engine", "")), "OI Signal")
    oi_change = metrics.get("oi_change_15m_window_pct")
    taker_ratio = metrics.get("taker_buy_ratio")
    funding_rate = metrics.get("funding_rate")
    oi_text = f"{float(oi_change):+.2f}%" if oi_change is not None else "—"
    taker_text = f"{float(taker_ratio):.0%}" if taker_ratio is not None else "—"
    funding_text = f"{float(funding_rate):+.8g}" if funding_rate is not None else "—"

    entry = float(signal.entry)
    try:
        current_price = float(metrics.get("current_price"))
        if not math.isfinite(current_price) or current_price <= 0:
            current_price = None
    except (TypeError, ValueError, OverflowError):
        current_price = None
    price_time_label = "نامشخص"
    try:
        price_time_ms = int(metrics.get("current_price_time_ms") or 0)
        if price_time_ms > 0:
            price_time_label = datetime.fromtimestamp(
                price_time_ms / 1000, tz=timezone.utc
            ).astimezone(timezone(timedelta(hours=3, minutes=30))).strftime("%H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    price_source_label = html.escape(str(metrics.get("price_source", "Binance Futures")))
    if current_price is None:
        current_price_line = f"💹 قیمت فعلی Binance Futures / {price_source_label}: <b>ناموجود</b>\\n"
    else:
        current_price_gap = (current_price - entry) / entry * 100.0
        current_price_line = (
            f"💹 قیمت فعلی Binance Futures / {price_source_label}: <code>{current_price:.8g}</code>"
            f" | فاصله تا ورود: <b>{current_price_gap:+.2f}%</b>"
            f" | ساعت ایران: <b>{price_time_label}</b>\n"
        )
    risk_pct = abs(entry - float(signal.stop_loss)) / entry * 100.0
    tp1_pct = abs(float(signal.take_profit_1) - entry) / entry * 100.0
    tp2_pct = abs(float(signal.take_profit_2) - entry) / entry * 100.0
    rr_tp1 = tp1_pct / risk_pct if risk_pct > 0 else 0.0
    entry_instruction = (
        "⚡ <b>ورود مارکت</b>؛ قیمت لحظه‌ای مبناست. به‌دلیل اسلیپیج، قبل از سفارش قیمت را بررسی کنید.\n"
        if getattr(signal, "execution_mode", "LIMIT") == "MARKET"
        else "⏳ ورود فقط بعد از لمس قیمت ورود فعال است؛ قبل از آن وارد نشوید.\n"
    )
    return (
        f"{title}  <b>{symbol}</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"💵 ورود: <code>{signal.entry:.8g}</code>\n"
        f"{current_price_line}"
        f"{entry_instruction}"
        f"🛡 حد ضرر: <code>{signal.stop_loss:.8g}</code>  |  ریسک: <b>{risk_pct:.2f}%</b>\n"
        f"🎯 هدف ۱: <code>{signal.take_profit_1:.8g}</code>  |  فاصله: <b>{tp1_pct:.2f}%</b>\n"
        f"🚀 هدف ۲: <code>{signal.take_profit_2:.8g}</code>  |  فاصله: <b>{tp2_pct:.2f}%</b>\n"
        f"⚖️ نسبت سود به ریسک تا هدف ۱: <b>{rr_tp1:.2f}R</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"📊 OI ۱۵دقیقه: <b>{oi_text}</b>  |  جریان {taker_text}\n"
        f"💸 Funding API خام: <b>{funding_text}</b> (واحد در مستندات API مشخص نشده)\n"
        f"⚙️ حجم: {signal.volume_ratio:.1f}x  |  روش: {engine}\n"
        f"🧭 تایم‌فریم: {html.escape(str(metrics.get('sweep_timeframe', '15m')))}\n"
        f"💧 شکار نقدینگی: {html.escape(str(metrics.get('sweep_reason', 'تأیید شد')))}"
    )


def scan_watchlist(watchlist: Dict[str, Dict[str, Any]], client: CoinalyzeClient | None = None,
                   now: int | None = None) -> Tuple[List[Tuple[Signal, Dict[str, Any]]], Dict[str, Dict[str, Any]]]:
    """Use Coinalyze as the primary source and emit only confirmed liquidity-sweep entries."""
    client = client or CoinalyzeClient()
    timestamp = int(now if now is not None else time.time())
    confirmed: List[Tuple[Signal, Dict[str, Any]]] = []
    updated = {symbol: dict(record) for symbol, record in watchlist.items()}
    for symbol, record in updated.items():
        symbol_started = time.monotonic()
        print(f"Scan symbol started: {symbol}")
        record["last_attempted_at"] = timestamp
        # Never leave a stale provider label from a previous scanner version or
        # an earlier successful attempt when the current price fetch fails.
        record.pop("data_source", None)
        try:
            # One exchange supplies both OHLCV and live price; Coinalyze supplies
            # derivatives/positioning metrics only. Never combine exchange prices.
            snapshot = fetch_snapshot(symbol)
            record["data_source"] = f"{snapshot.get('price_source', 'market feed')} prices + Coinalyze derivatives metrics"
            metrics = client.fetch_metrics(symbol, now=timestamp)
            # OI change and taker flow must describe the very same closed
            # 15m candle as the price-action liquidity sweep. Reject stale or
            # cross-candle derivatives data rather than mixing timestamps.
            candles = snapshot.get("candles") or []
            price_candle_time = int(candles[-1].get("open_time", 0)) if candles else 0
            derivatives_candle_time = metrics.get("derivatives_candle_open_time_ms")
            if not price_candle_time or derivatives_candle_time is None:
                raise CoinalyzeError("زمان کندل مشتقات Coinalyze یا قیمت موجود نیست")
            if abs(int(derivatives_candle_time) - price_candle_time) > 60_000:
                raise CoinalyzeError(
                    "زمان کندل OI/جریان Coinalyze با کندل قیمت یکسان نیست؛ "
                    f"price={price_candle_time}, derivatives={int(derivatives_candle_time)}"
                )
            # Discard any provider-side OHLCV snapshot: it must not affect entries.
            metrics.pop("market_snapshot", None)
            metrics.update(source_oi_priority(record.get("source_text", "")))
            record["oi_priority_score"] = metrics["oi_priority_score"]
            record["oi_priority_tier"] = metrics["oi_priority_tier"]
            record["coinalyze"] = dict(metrics)

            signal, reason = evaluate_liquidity_sweep(snapshot, metrics)
            if signal is not None:
                # The signal entry is the last closed Binance 15m candle close.
                # Do not publish it if Binance's live perpetual price has moved
                # materially away before the alert is generated.
                live_price = snapshot.get("live_price")
                atr_for_entry = _atr(snapshot.get("candles") or [])
                if live_price is None or not math.isfinite(float(live_price)) or float(live_price) <= 0:
                    geometry_rejection = "قیمت لحظه‌ای همان صرافیِ کندل موجود نیست؛ سیگنال با داده ناقص رد شد"
                elif abs(float(live_price) - float(signal.entry)) > max(atr_for_entry * 0.50, float(signal.entry) * 0.001):
                    deviation_pct = abs(float(live_price) - float(signal.entry)) / float(signal.entry) * 100.0
                    geometry_rejection = (
                        f"فاصله قیمت زنده {snapshot.get('price_source', 'بازار')} تا ورود کندل "
                        f"{deviation_pct:.2f}٪ است؛ ورود قدیمی/دور رد شد"
                    )
                else:
                    geometry_rejection = signal_price_geometry_rejection(
                        signal, min_tp1_distance_pct=0.0
                    )
                if geometry_rejection:
                    record["status"] = "رد سیگنال؛ قیمت ورود نامعتبر"
                    record["reason"] = geometry_rejection
                else:
                    signal_metrics = dict(metrics)
                    signal_metrics["engine"] = "LIQUIDITY_SWEEP"
                    signal_metrics["coinalyze_confirmed"] = True
                    signal_metrics["liquidity_sweep_confirmed"] = True
                    signal_metrics["sweep_timeframe"] = "15m"
                    signal_metrics["sweep_reason"] = reason
                    signal_metrics["current_price"] = float(live_price) if live_price is not None else None
                    signal_metrics["current_price_time_ms"] = int(
                        snapshot.get("live_price_time_ms") or time.time() * 1000
                    )
                    signal_metrics["price_source"] = snapshot.get("price_source", "unknown")
                    candles = snapshot.get("candles") or []
                    atr_value = _atr(candles) if candles else 0.0
                    stop_distance = abs(signal.entry - signal.stop_loss)
                    signal_metrics["atr_15m"] = atr_value if atr_value > 0 else None
                    signal_metrics["stop_distance_pct"] = stop_distance / signal.entry * 100.0
                    signal_metrics["stop_distance_atr"] = (
                        stop_distance / atr_value if atr_value > 0 else None
                    )
                    confirmed.append((signal, signal_metrics))
                    record.pop("pending_setup", None)
                    record["status"] = f"سیگنال {signal.side} آماده ارسال"
                    record["reason"] = reason
            else:
                record["status"] = "در حال رصد نقدینگی"
                record["reason"] = reason
                record.pop("pending_setup", None)

            record["last_checked"] = timestamp
            record.pop("retry_after", None)
        except (CoinalyzeError, MarketDataError, OSError, ValueError, KeyError) as exc:
            record["status"] = "خطای دریافت داده"
            record["reason"] = f"{type(exc).__name__}: {exc}"
            record["last_checked"] = timestamp
            record["retry_after"] = timestamp + DATA_ERROR_RETRY_SECONDS
        except Exception as exc:
            record["status"] = "خطای غیرمنتظره"
            record["reason"] = f"{type(exc).__name__}: {exc}"
            record["last_checked"] = timestamp
            record["retry_after"] = timestamp + DATA_ERROR_RETRY_SECONDS
        finally:
            elapsed = time.monotonic() - symbol_started
            print(
                f"Scan symbol finished: {symbol} status={record.get('status', 'unknown')} "
                f"elapsed={elapsed:.1f}s"
            )
    confirmed.sort(
        key=lambda item: float(item[1].get("oi_priority_score", 0.0)),
        reverse=True,
    )
    return confirmed, updated
