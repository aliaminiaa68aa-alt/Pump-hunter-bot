"""Alert-driven watchlist scanner with independent Coinalyze confirmation."""
from __future__ import annotations
import time
import math
import requests
from typing import Any, Dict, List, Tuple

from oi_engine.coinalyze_client import CoinalyzeClient, CoinalyzeError
from oi_engine.signal_engine import MarketDataError, Signal, diagnose_snapshot, evaluate_snapshot, fetch_snapshot, format_signal
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
    """Extract upstream 15m/1h OI expansion as ranking context, never as an entry gate."""
    values = {}
    if not isinstance(source_text, str):
        source_text = ""
    for timeframe in ("15m", "1h"):
        match = __import__("re").search(
            rf"([+-]?\d+(?:\.\d+)?)%\s*\([^)]*{timeframe}\)",
            source_text,
            __import__("re").I,
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
    tier = ("🔥 بسیار قوی" if score >= 12 else
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
    return (
        format_signal(signal) + priority_text + "\n\nتأیید مرحله دوم Coinalyze"
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
            signal, pending_setup, setup_reason = advance_pullback_setup(
                snapshot, record.get("pending_setup"), record.get("source_side")
            )
            if signal is None:
                record["pending_setup"] = pending_setup
                record["status"] = "منتظر پولبک و تأیید" if pending_setup else "در حال رصد"
                if pending_setup:
                    record["reason"] = setup_reason
                else:
                    reasons = diagnose_snapshot(snapshot)
                    record["reason"] = setup_reason or ("رد شرایط: " + " | ".join(reasons[:3]))
                continue
            record.pop("pending_setup", None)
            if metrics is None:
                metrics = client.fetch_metrics(symbol, now=timestamp)
                metrics.pop("market_snapshot", None)
            # Upstream OI expansion is a ranking bonus only. It never bypasses
            # closed-candle, pullback, directional-flow, or Coinalyze entry checks.
            metrics.update(source_oi_priority(record.get("source_text", "")))
            record["oi_priority_score"] = metrics["oi_priority_score"]
            record["oi_priority_tier"] = metrics["oi_priority_tier"]
            record["coinalyze"] = metrics
            if not coinalyze_confirms(signal, metrics):
                record["status"] = "نزدیک شرایط ورود"
                oi_value = metrics.get("oi_change_15m_window_pct")
                flow_value = metrics.get("taker_buy_ratio")
                record["reason"] = (
                    f"Coinalyze رد کرد: OI={oi_value if oi_value is not None else 'ناموجود'}%، "
                    f"نسبت خرید={flow_value if flow_value is not None else 'ناموجود'}؛ "
                    + ("برای LONG حداقل OI برابر 0.15% و خرید تهاجمی 52% لازم است"
                       if signal.side == "LONG" else
                       "برای SHORT حداقل OI برابر 0.15% و نسبت خرید حداکثر 48% لازم است")
                )
                continue
            record["status"] = f"تأیید {signal.side}"
            record["reason"] = "تأیید موتور 15m و Coinalyze"
            confirmed.append((signal, metrics))
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
