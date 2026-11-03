"""Persistent outcome journal for confirmed OI signals.

Uses closed 15m Binance futures candles. Ambiguous stop/target touches within
one candle are resolved conservatively as stop-first.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List

import requests

BASE_URL = "https://fapi.binance.com"
INTERVAL_MS = 15 * 60 * 1000
MAX_ACTIVE_HOURS = 96


def _fetch_klines(symbol: str, start_ms: int, end_ms: int) -> List[list]:
    response = requests.get(
        BASE_URL + "/fapi/v1/klines",
        params={
            "symbol": symbol,
            "interval": "15m",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("Unexpected Binance kline response")
    return payload


def make_trade_record(signal: Any, metrics: Dict[str, Any], sent_at_ms: int) -> Dict[str, Any]:
    data = signal.to_dict() if hasattr(signal, "to_dict") else dict(signal)
    trade_id = f"{data['symbol']}:{data['side']}:{int(data['candle_close_time'])}"
    return {
        "trade_id": trade_id,
        "signal": data,
        "source": {
            "provider": "Binance + Coinalyze",
            "coinalyze_oi_change_15m_pct": metrics.get("oi_change_15m_window_pct"),
            "coinalyze_taker_buy_ratio": metrics.get("taker_buy_ratio"),
            "funding_rate_raw": metrics.get("funding_rate"),
            "predicted_funding_rate_raw": metrics.get("predicted_funding_rate"),
        },
        "sent_at_ms": int(sent_at_ms),
        "last_checked_ms": int(data["candle_close_time"]) + 1,
        "status": "OPEN",
        "events": [],
        "mfe_pct": 0.0,
        "mae_pct": 0.0,
        "last_error": None,
    }


def _price_extremes(side: str, entry: float, high: float, low: float):
    if side == "LONG":
        return max(0.0, (high / entry - 1.0) * 100.0), max(0.0, (entry / low - 1.0) * 100.0)
    return max(0.0, (entry / low - 1.0) * 100.0), max(0.0, (high / entry - 1.0) * 100.0)


def _touches(side: str, candle: list, signal: Dict[str, Any]):
    open_price, high, low = float(candle[1]), float(candle[2]), float(candle[3])
    stop = float(signal["stop_loss"])
    tp1 = float(signal["take_profit_1"])
    tp2 = float(signal["take_profit_2"])
    if side == "LONG":
        stop_hit = open_price <= stop or low <= stop
        tp1_hit = open_price >= tp1 or high >= tp1
        tp2_hit = open_price >= tp2 or high >= tp2
        stop_fill = open_price if open_price <= stop else stop
        tp1_fill = open_price if open_price >= tp1 else tp1
        tp2_fill = open_price if open_price >= tp2 else tp2
    else:
        stop_hit = open_price >= stop or high >= stop
        tp1_hit = open_price <= tp1 or low <= tp1
        tp2_hit = open_price <= tp2 or low <= tp2
        stop_fill = open_price if open_price >= stop else stop
        tp1_fill = open_price if open_price <= tp1 else tp1
        tp2_fill = open_price if open_price <= tp2 else tp2
    return stop_hit, tp1_hit, tp2_hit, stop_fill, tp1_fill, tp2_fill


def monitor_journal(
    journal: Dict[str, Any],
    now_ms: int | None = None,
    fetcher: Callable[[str, int, int], List[list]] = _fetch_klines,
) -> Dict[str, Any]:
    """Update active records; provider failures are retained per trade, not fatal."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    trades = journal.setdefault("trades", {})
    for trade_id, record in list(trades.items()):
        if record.get("status") in {"CLOSED_SL", "CLOSED_TP2", "TIMED_OUT"}:
            continue
        signal = record["signal"]
        side = signal["side"]
        entry = float(signal["entry"])
        try:
            candles = fetcher(
                signal["symbol"],
                int(record.get("last_checked_ms", int(signal["candle_close_time"]) + 1)),
                now,
            )
            candles = sorted(candles, key=lambda row: int(row[0]))
            for candle in candles:
                open_ms = int(candle[0])
                close_ms = open_ms + INTERVAL_MS - 1
                if close_ms >= now:
                    continue
                high, low = float(candle[2]), float(candle[3])
                mfe, mae = _price_extremes(side, entry, high, low)
                record["mfe_pct"] = max(float(record.get("mfe_pct", 0.0)), mfe)
                record["mae_pct"] = max(float(record.get("mae_pct", 0.0)), mae)

                stop_hit, tp1_hit, tp2_hit, stop_fill, tp1_fill, tp2_fill = _touches(side, candle, signal)
                event_names = {event["event"] for event in record.get("events", [])}

                # OHLC cannot reveal intrabar order. If a stop and any target
                # coexist in one candle, assume the stop happened first.
                if stop_hit:
                    risk = abs(entry - float(signal["stop_loss"]))
                    fill = stop_fill
                    signed = (fill - entry) if side == "LONG" else (entry - fill)
                    record.setdefault("events", []).append({
                        "event": "SL", "time_ms": close_ms, "fill": fill,
                        "r_multiple": signed / risk if risk else None,
                    })
                    record["status"] = "CLOSED_SL"
                    record["exit_time_ms"] = close_ms
                    break

                if tp1_hit and "TP1" not in event_names:
                    risk = abs(entry - float(signal["stop_loss"]))
                    signed = (tp1_fill - entry) if side == "LONG" else (entry - tp1_fill)
                    record.setdefault("events", []).append({
                        "event": "TP1", "time_ms": close_ms, "fill": tp1_fill,
                        "r_multiple": signed / risk if risk else None,
                    })
                    record["status"] = "TP1_HIT"

                if tp2_hit:
                    risk = abs(entry - float(signal["stop_loss"]))
                    signed = (tp2_fill - entry) if side == "LONG" else (entry - tp2_fill)
                    record.setdefault("events", []).append({
                        "event": "TP2", "time_ms": close_ms, "fill": tp2_fill,
                        "r_multiple": signed / risk if risk else None,
                    })
                    record["status"] = "CLOSED_TP2"
                    record["exit_time_ms"] = close_ms
                    break

                record["last_checked_ms"] = close_ms + 1

            if now - int(record["sent_at_ms"]) >= MAX_ACTIVE_HOURS * 60 * 60 * 1000:
                if record.get("status") not in {"CLOSED_SL", "CLOSED_TP2"}:
                    record["status"] = "TIMED_OUT"
                    record["exit_time_ms"] = now
        except Exception as exc:
            record["last_error"] = f"{type(exc).__name__}: {exc}"
    # Bound history without deleting unresolved trades.
    closed = [
        (key, value) for key, value in trades.items()
        if value.get("status") in {"CLOSED_SL", "CLOSED_TP2", "TIMED_OUT"}
    ]
    if len(closed) > 500:
        for key, _ in sorted(closed, key=lambda item: item[1].get("sent_at_ms", 0))[:-500]:
            trades.pop(key, None)
    journal["updated_at_ms"] = now
    return journal
