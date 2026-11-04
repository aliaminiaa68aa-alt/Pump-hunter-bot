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
    def raw_funding(value: Any) -> str:
        if value is None:
            return "ناموجود"
        try:
            number = float(value)
            if not math.isfinite(number):
                return "نامعتبر"
            return f"{number:.8g} (خام؛ واحد/بازه نامشخص)"
        except (TypeError, ValueError):
            return "نامعتبر"

    ratio = metrics.get("long_short_ratio")
    long_liq, short_liq = metrics.get("long_liquidations"), metrics.get("short_liquidations")
    funding = metrics.get("funding_rate")
    predicted_funding = metrics.get("predicted_funding_rate")
    funding_warning = ""
    if funding is not None or predicted_funding is not None:
        funding_warning = "\n⚠️ مقادیر Funding از Coinalyze خام‌اند؛ تا تأیید واحد و بازه زمانی، درصد تلقی نکن و مبنای ورود قرار نده."
    priority_15m = metrics.get("source_oi_15m_pct")
    priority_1h = metrics.get("source_oi_1h_pct")
    priority_text = (
        f"\nاولویت رشد OI هشدار: {metrics.get('oi_priority_tier', '🟡 معمولی')} "
        f"(امتیاز {metrics.get('oi_priority_score', 0):.2f}/20)"
        f"\nOI منبع: 15m={priority_15m if priority_15m is not None else 'ناموجود'}%، "
        f"1h={priority_1h if priority_1h is not None else 'ناموجود'}%"
    )
    engine_names = {"OI_BOS": "شکست ساختار (BOS)", "PULLBACK": "پولبک"}
    engine_text = engine_names.get(str(metrics.get("engine", "")), "نامشخص")
    atr = metrics.get("atr_15m")
    stop_pct = metrics.get("stop_distance_pct")
    stop_atr = metrics.get("stop_distance_atr")
    risk_text = (
        f"\nموتور سیگنال: {engine_text}"
        + (f"\nATR تایم‌فریم ۱۵دقیقه: {float(atr):.8g}" if atr is not None else "\nATR تایم‌فریم ۱۵دقیقه: ناموجود")
        + (f"\nفاصله حد ضرر: {float(stop_pct):.2f}% | {float(stop_atr):.2f} ATR" if stop_pct is not None and stop_atr is not None else "\nفاصله حد ضرر: ناموجود")
    )
    return (
        format_signal(signal) + risk_text + priority_text + "\n\nتأیید مرحله دوم Coinalyze"
        + f"\nتغییر OI: {metrics['oi_change_15m_window_pct']:+.2f}%"
        + f"\nنسبت خرید تهاجمی: {metrics['taker_buy_ratio']:.1%}"
        + f"\nFunding خام (Coinalyze): {raw_funding(funding)}"
        + f"\nFunding پیش‌بینی‌شده خام (Coinalyze): {raw_funding(predicted_funding)}"
        + f"\nنسبت حساب‌های Long/Short: {'ناموجود' if ratio is None else f'{float(ratio):.3f}'}"
        + f"\nلیکویید Long: {'ناموجود' if long_liq is None else f'{float(long_liq):,.0f}'}"
        + f"\nلیکویید Short: {'ناموجود' if short_liq is None else f'{float(short_liq):,.0f}'}"
        + funding_warning
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
            # Run OI/BOS independently; it must not wait for a pullback.
            bos_signal = None
            try:
                bos_signal = evaluate_snapshot(snapshot, record.get("source_side"))
            except (KeyError, TypeError, ValueError, MarketDataError):
                bos_signal = None
            previous_pending_setup = record.get("pending_setup")
            pullback_signal, pending_setup, setup_reason = advance_pullback_setup(
                snapshot, previous_pending_setup, record.get("source_side")
            )
            if metrics is None:
                metrics = client.fetch_metrics(symbol, now=timestamp)
                metrics.pop("market_snapshot", None)
            metrics.update(source_oi_priority(record.get("source_text", "")))
            record["oi_priority_score"] = metrics["oi_priority_score"]
            record["oi_priority_tier"] = metrics["oi_priority_tier"]
            record["coinalyze"] = metrics
            if bos_signal is not None and coinalyze_confirms(bos_signal, metrics):
                bos_metrics = dict(metrics)
                bos_metrics["engine"] = "OI_BOS"
                candles_for_atr = snapshot.get("candles") or []
                atr_15m = _atr(candles_for_atr) if candles_for_atr else 0.0
                stop_distance = abs(bos_signal.entry - bos_signal.stop_loss)
                bos_metrics["atr_15m"] = atr_15m if atr_15m > 0 else None
                bos_metrics["stop_distance_pct"] = stop_distance / bos_signal.entry * 100.0
                bos_metrics["stop_distance_atr"] = stop_distance / atr_15m if atr_15m > 0 else None
                confirmed.append((bos_signal, bos_metrics))
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
            else:
                record["pending_setup"] = pending_setup
                record["status"] = "منتظر پولبک و تأیید" if pending_setup else "در حال رصد"
                if pending_setup:
                    record["reason"] = setup_reason
                elif bos_signal is None:
                    reasons = diagnose_snapshot(snapshot)
                    record["reason"] = setup_reason or ("رد شرایط: " + " | ".join(reasons[:3]))
                else:
                    record["reason"] = "BOS تشکیل شد اما تأیید Coinalyze کامل نیست"
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
