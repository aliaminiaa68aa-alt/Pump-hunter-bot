"""Alert-driven watchlist scanner with independent Coinalyze confirmation."""
from __future__ import annotations
import time
import requests
from typing import Any, Dict, List, Tuple

from oi_engine.coinalyze_client import CoinalyzeClient, CoinalyzeError
from oi_engine.signal_engine import MarketDataError, Signal, evaluate_snapshot, fetch_snapshot, format_signal


def coinalyze_confirms(signal: Signal, metrics: Dict[str, Any]) -> bool:
    """Fail closed unless Coinalyze OI and taker flow independently align."""
    quality = metrics.get("data_quality") or {}
    oi_change = metrics.get("oi_change_15m_window_pct")
    taker_ratio = metrics.get("taker_buy_ratio")
    if not quality.get("complete") or oi_change is None or taker_ratio is None:
        return False
    if float(oi_change) < 0.15:
        return False
    if signal.side == "LONG":
        return float(taker_ratio) >= 0.52
    if signal.side == "SHORT":
        return float(taker_ratio) <= 0.48
    return False


def format_confirmed_signal(signal: Signal, metrics: Dict[str, Any]) -> str:
    def pct(value: Any) -> str:
        return "ناموجود" if value is None else f"{float(value):.5%}"
    ratio = metrics.get("long_short_ratio")
    long_liq, short_liq = metrics.get("long_liquidations"), metrics.get("short_liquidations")
    return (
        format_signal(signal) + "\n\nتأیید مستقل Coinalyze"
        + f"\nتغییر OI: {metrics['oi_change_15m_window_pct']:+.2f}%"
        + f"\nنسبت خرید تهاجمی: {metrics['taker_buy_ratio']:.1%}"
        + f"\nFunding: {pct(metrics.get('funding_rate'))}"
        + f"\nFunding پیش‌بینی‌شده: {pct(metrics.get('predicted_funding_rate'))}"
        + f"\nنسبت حساب‌های Long/Short: {'ناموجود' if ratio is None else f'{float(ratio):.3f}'}"
        + f"\nلیکویید Long: {'ناموجود' if long_liq is None else f'{float(long_liq):,.0f}'}"
        + f"\nلیکویید Short: {'ناموجود' if short_liq is None else f'{float(short_liq):,.0f}'}"
    )


def scan_watchlist(watchlist: Dict[str, Dict[str, Any]], client: CoinalyzeClient | None = None,
                   now: int | None = None) -> Tuple[List[Tuple[Signal, Dict[str, Any]]], Dict[str, Dict[str, Any]]]:
    """Scan only symbols introduced by forwarded alerts and persist per-symbol status."""
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
            signal = evaluate_snapshot(snapshot)
            if signal is None:
                record["status"] = "در حال رصد"
                record["reason"] = "شرایط ورود در کندل بسته‌شده تأیید نشد"
                continue
            if metrics is None:
                metrics = client.fetch_metrics(symbol, now=timestamp)
                metrics.pop("market_snapshot", None)
            record["coinalyze"] = metrics
            if not coinalyze_confirms(signal, metrics):
                record["status"] = "نزدیک شرایط ورود"
                record["reason"] = "تأیید مستقل Coinalyze کامل یا هم‌جهت نیست"
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
    return confirmed, updated
