"""Alert-driven watchlist scanner with independent Coinalyze confirmation."""
from __future__ import annotations
import time
import math
import requests
from typing import Any, Dict, List, Tuple

from oi_engine.coinalyze_client import CoinalyzeClient, CoinalyzeError
from oi_engine.signal_engine import MarketDataError, Signal, _atr, diagnose_snapshot, evaluate_snapshot, fetch_snapshot, format_signal
from oi_engine.pullback_engine import advance_pullback_setup


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

    engine_names = {"OI_BOS": "BOS", "PULLBACK": "Pullback"}
    engine = engine_names.get(str(metrics.get("engine", "")), "OI Signal")
    oi_change = metrics.get("oi_change_15m_window_pct")
    taker_ratio = metrics.get("taker_buy_ratio")
    oi_text = f"{float(oi_change):+.2f}%" if oi_change is not None else "—"
    taker_text = f"{float(taker_ratio):.0%}" if taker_ratio is not None else "—"

    entry = float(signal.entry)
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
        f"{entry_instruction}"
        f"🛡 حد ضرر: <code>{signal.stop_loss:.8g}</code>  |  ریسک: <b>{risk_pct:.2f}%</b>\n"
        f"🎯 هدف ۱: <code>{signal.take_profit_1:.8g}</code>  |  فاصله: <b>{tp1_pct:.2f}%</b>\n"
        f"🚀 هدف ۲: <code>{signal.take_profit_2:.8g}</code>  |  فاصله: <b>{tp2_pct:.2f}%</b>\n"
        f"⚖️ نسبت سود به ریسک تا هدف ۱: <b>{rr_tp1:.2f}R</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"📊 OI ۱۵دقیقه: <b>{oi_text}</b>  |  جریان {taker_text}\n"
        f"⚙️ حجم: {signal.volume_ratio:.1f}x  |  روش: {engine}"
    )


def scan_watchlist(watchlist: Dict[str, Dict[str, Any]], client: CoinalyzeClient | None = None,
                   now: int | None = None) -> Tuple[List[Tuple[Signal, Dict[str, Any]]], Dict[str, Dict[str, Any]]]:
    """Scan only symbols admitted by the public-channel watchlist and persist per-symbol status."""
    client = client or CoinalyzeClient()
    timestamp = int(now if now is not None else time.time())
    confirmed: List[Tuple[Signal, Dict[str, Any]]] = []
    updated = {symbol: dict(record) for symbol, record in watchlist.items()}
    for symbol, record in updated.items():
        symbol_started = time.monotonic()
        print(f"Scan symbol started: {symbol}")
        # Record attempts separately from successful scans. A transient provider
        # failure must not mark a fresh OI alert as fully checked.
        record["last_attempted_at"] = timestamp
        try:
            metrics = None
            try:
                snapshot = fetch_snapshot(symbol)
                record["data_source"] = "Binance Futures API"
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 451:
                    raise
                metrics = client.fetch_metrics(symbol, now=timestamp)
                snapshot = metrics.pop("market_snapshot", None)
                if not snapshot:
                    raise MarketDataError(
                        "Binance returned HTTP 451 and Coinalyze did not provide "
                        "enough aligned 15m OHLCV/OI history for a safe fallback"
                    )
                record["data_source"] = "Coinalyze 15m fallback (Binance HTTP 451)"
                # Coinalyze's closed 15m candles drive setup detection. Fetch a
                # separate recent 1m quote only for execution, never use the
                # older 15m close as a substitute for a MARKET fill.
                live_quote_fetcher = getattr(client, "fetch_live_price", None)
                if callable(live_quote_fetcher):
                    try:
                        live_quote = live_quote_fetcher(symbol, now=timestamp)
                        if live_quote:
                            snapshot.update(live_quote)
                    except (CoinalyzeError, requests.RequestException, ValueError, TypeError) as quote_error:
                        print(f"Live quote unavailable for {symbol}: {quote_error}")
            # Derivatives context is a mandatory pre-signal gate, not a
            # post-signal annotation. Missing optional endpoints fail closed.
            if metrics is None:
                metrics = client.fetch_metrics(symbol, now=timestamp)
            metrics.pop("market_snapshot", None)

            # Evaluate a confirmed closed-candle OI signal independently of
            # the direction label in the source channel. The channel is a
            # discovery feed, not a LONG/SHORT instruction.
            bos_signal = evaluate_snapshot(snapshot)
            previous_pending_setup = record.get("pending_setup")
            # Spot-only execution: never publish SHORT setups, which require
            # borrowing/margin and are not actionable in the user's cash-market plan.
            # Derivatives OI/funding remain confirmation data only.
            pullback_signal, pending_setup, setup_reason = advance_pullback_setup(
                snapshot, previous_pending_setup, "LONG"
            )
            metrics.update(source_oi_priority(record.get("source_text", "")))
            record["oi_priority_score"] = metrics["oi_priority_score"]
            record["oi_priority_tier"] = metrics["oi_priority_tier"]
            record["coinalyze"] = metrics
            # A BOS is only a setup trigger, never an immediate market entry.
            # Every directional entry must pass the later retest/rejection path below.
            geometry_rejection = (
                signal_price_geometry_rejection(pullback_signal)
                if pullback_signal is not None else None
            )
            if pullback_signal is not None and geometry_rejection:
                record["status"] = "رد سیگنال به‌دلیل کیفیت قیمت‌ها"
                record["reason"] = geometry_rejection
                record["pending_setup"] = pending_setup or previous_pending_setup
            elif pullback_signal is not None and coinalyze_confirms(pullback_signal, metrics):
                pullback_metrics = dict(metrics)
                pullback_metrics["engine"] = "PULLBACK"
                pullback_metrics["coinalyze_confirmed"] = True
                candles_for_atr = snapshot.get("candles") or []
                atr_15m = _atr(candles_for_atr) if candles_for_atr else 0.0
                stop_distance = abs(pullback_signal.entry - pullback_signal.stop_loss)
                pullback_metrics["atr_15m"] = atr_15m if atr_15m > 0 else None
                pullback_metrics["stop_distance_pct"] = stop_distance / pullback_signal.entry * 100.0
                pullback_metrics["stop_distance_atr"] = stop_distance / atr_15m if atr_15m > 0 else None
                confirmed.append((pullback_signal, pullback_metrics))
                record.pop("pending_setup", None)
                record["status"] = f"سیگنال {pullback_signal.side} آماده ارسال"
                record["reason"] = "تأیید ساختار، قدرت حرکت، کیفیت ورود و فیلترهای Coinalyze کامل شد"
            else:
                record["pending_setup"] = pending_setup or previous_pending_setup
                if pullback_signal is not None:
                    record["status"] = "منتظر تأیید مشتقات"
                    gate_reasons = coinalyze_rejection_reasons(pullback_signal, metrics)
                    record["reason"] = "رد تأیید مشتقات: " + " | ".join(gate_reasons[:4])
                else:
                    record["status"] = "منتظر پولبک و تأیید" if pending_setup else "در حال رصد"
                    if pending_setup:
                        record["reason"] = setup_reason
                    elif bos_signal is None:
                        reasons = diagnose_snapshot(snapshot)
                        record["reason"] = "رد شرایط ورود: " + " | ".join(reasons[:3])
                    else:
                        # Preserve the pullback engine's exact rejection reason.
                        # Previously this branch replaced it with a generic
                        # message, hiding which breakout quality gate failed.
                        record["reason"] = setup_reason or (
                            "شکست/شتاب اولیه دیده شد اما ورود بدون پولبک و تأیید صادر نمی‌شود"
                        )
            # Only a completed market-data/evaluation pass consumes the fresh-alert
            # priority. Failed requests remain eligible for retry.
            record["last_checked"] = timestamp
        except (CoinalyzeError, MarketDataError, OSError, ValueError, KeyError) as exc:
            record["status"] = "خطای دریافت داده"
            record["reason"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            record["status"] = "خطای غیرمنتظره"
            record["reason"] = f"{type(exc).__name__}: {exc}"
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
