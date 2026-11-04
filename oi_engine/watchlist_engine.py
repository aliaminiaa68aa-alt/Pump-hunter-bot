"""Alert-driven watchlist scanner with independent Coinalyze confirmation."""
from __future__ import annotations
import time
import math
import requests
from typing import Any, Dict, List, Tuple

from oi_engine.coinalyze_client import CoinalyzeClient, CoinalyzeError
from oi_engine.signal_engine import MarketDataError, Signal, _atr, diagnose_snapshot, evaluate_snapshot, fetch_snapshot, format_signal
from oi_engine.pullback_engine import advance_pullback_setup


def coinalyze_confirms(signal: Signal, metrics: Dict[str, Any]) -> bool:
    """Fail closed unless Coinalyze OI and taker flow independently align."""
    oi_change = metrics.get("oi_change_15m_window_pct")
    taker_ratio = metrics.get("taker_buy_ratio")
    if oi_change is None or taker_ratio is None:
        return False
    try:
        oi_change = float(oi_change)
        taker_ratio = float(taker_ratio)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(oi_change) or not math.isfinite(taker_ratio):
        return False
    if oi_change < 0.15 or not 0.0 <= taker_ratio <= 1.0:
        return False
    # Funding, long/short ratio and liquidation history are supplementary context.
    # Missing optional endpoints must not veto an otherwise valid OI + taker-flow confirmation.
    if signal.side == "LONG":
        return taker_ratio >= 0.52
    if signal.side == "SHORT":
        return taker_ratio <= 0.48
    return False


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

    return (
        f"{title}  <b>{symbol}</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"💵 ورود: <code>{signal.entry:.8g}</code>\n"
        f"🛡 حد ضرر: <code>{signal.stop_loss:.8g}</code>\n"
        f"🎯 هدف ۱: <code>{signal.take_profit_1:.8g}</code>\n"
        f"🚀 هدف ۲: <code>{signal.take_profit_2:.8g}</code>\n"
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
        record["last_checked"] = timestamp
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
            # Evaluate a confirmed closed-candle OI signal independently of
            # the direction label in the source channel. The channel is a
            # discovery feed, not a LONG/SHORT instruction.
            bos_signal = evaluate_snapshot(snapshot)
            previous_pending_setup = record.get("pending_setup")
            pullback_signal, pending_setup, setup_reason = advance_pullback_setup(
                snapshot, previous_pending_setup, None
            )
            if metrics is None:
                metrics = client.fetch_metrics(symbol, now=timestamp)
                metrics.pop("market_snapshot", None)
            metrics.update(source_oi_priority(record.get("source_text", "")))
            record["oi_priority_score"] = metrics["oi_priority_score"]
            record["oi_priority_tier"] = metrics["oi_priority_tier"]
            record["coinalyze"] = metrics
            bos_confirmed = False
            if (pullback_signal is None and previous_pending_setup is None
                    and pending_setup is None and bos_signal is not None
                    and coinalyze_confirms(bos_signal, metrics)):
                bos_metrics = dict(metrics)
                bos_metrics["engine"] = "OI_BOS"
                candles_for_atr = snapshot.get("candles") or []
                atr_15m = _atr(candles_for_atr) if candles_for_atr else 0.0
                stop_distance = abs(bos_signal.entry - bos_signal.stop_loss)
                bos_metrics["atr_15m"] = atr_15m if atr_15m > 0 else None
                bos_metrics["stop_distance_pct"] = stop_distance / bos_signal.entry * 100.0
                bos_metrics["stop_distance_atr"] = stop_distance / atr_15m if atr_15m > 0 else None
                confirmed.append((bos_signal, bos_metrics))
                bos_confirmed = True
                record.pop("pending_setup", None)
                record["status"] = f"تأیید {bos_signal.side}"
                record["reason"] = "تأیید سیگنال مستقل OI و Coinalyze"
            if pullback_signal is not None:
                if coinalyze_confirms(pullback_signal, metrics):
                    pullback_metrics = dict(metrics)
                    pullback_metrics["engine"] = "PULLBACK"
                    candles_for_atr = snapshot.get("candles") or []
                    atr_15m = _atr(candles_for_atr) if candles_for_atr else 0.0
                    stop_distance = abs(pullback_signal.entry - pullback_signal.stop_loss)
                    pullback_metrics["atr_15m"] = atr_15m if atr_15m > 0 else None
                    pullback_metrics["stop_distance_pct"] = stop_distance / pullback_signal.entry * 100.0
                    pullback_metrics["stop_distance_atr"] = stop_distance / atr_15m if atr_15m > 0 else None
                    confirmed.append((pullback_signal, pullback_metrics))
                    record.pop("pending_setup", None)
                    record["status"] = f"تأیید {pullback_signal.side}"
                    record["reason"] = "تأیید موتور Pullback و Coinalyze"
                else:
                    record["pending_setup"] = pending_setup or previous_pending_setup
                    record["status"] = "نزدیک شرایط ورود"
                    record["reason"] = "سیگنال پولبک منتظر تأیید OI/جریان سفارش است"
            elif not bos_confirmed:
                record["pending_setup"] = pending_setup
                record["status"] = "منتظر پولبک و تأیید" if pending_setup else "در حال رصد"
                if pending_setup:
                    record["reason"] = setup_reason
                elif bos_signal is None:
                    reasons = diagnose_snapshot(snapshot)
                    record["reason"] = "رد شرایط ورود: " + " | ".join(reasons[:3])
                else:
                    record["reason"] = "سیگنال مستقل تشکیل شد اما تأیید Coinalyze کامل نیست"
            if bos_signal is not None and not coinalyze_confirms(bos_signal, metrics):
                record["reason"] = record.get("reason", "") + " | تأیید OI_BOS در Coinalyze کامل نشد"
        except (CoinalyzeError, MarketDataError, OSError, ValueError, KeyError) as exc:
            record["status"] = "خطای دریافت داده"
            record["reason"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            record["status"] = "خطای غیرمنتظره"
            record["reason"] = f"{type(exc).__name__}: {exc}"
    confirmed.sort(
        key=lambda item: float(item[1].get("oi_priority_score", 0.0)),
        reverse=True,
    )
    return confirmed, updated
