"""Public Bybit linear-perpetual OHLCV fallback for Binance market candles.

Coinalyze remains exclusively a derivatives-metrics provider. This module
never requests OI, funding, or other derivatives positioning data.
"""
from __future__ import annotations

import time
from typing import Any

import requests

BYBIT_BASE_URL = "https://api.bybit.com"
INTERVAL_MS = {"15": 15 * 60 * 1000, "1": 60 * 1000}


def fetch_bybit_rows(symbol: str, interval: str, start_ms: int | None = None,
                     end_ms: int | None = None, limit: int = 1000) -> list[list]:
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported Bybit interval: {interval}")
    params: dict[str, Any] = {
        "category": "linear", "symbol": symbol, "interval": interval,
        "limit": min(max(int(limit), 1), 1000),
    }
    if start_ms is not None:
        params["start"] = int(start_ms)
    if end_ms is not None:
        params["end"] = int(end_ms)
    response = requests.get(
        BYBIT_BASE_URL + "/v5/market/kline", params=params, timeout=15,
        headers={"User-Agent": "Pump-Hunter/1.0"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("retCode") != 0:
        raise RuntimeError(f"Bybit kline API error: {payload}")
    result = payload.get("result") or {}
    rows = result.get("list")
    if not isinstance(rows, list):
        raise RuntimeError("Unexpected Bybit kline response")
    now_ms = int(time.time() * 1000)
    duration = INTERVAL_MS[interval]
    normalized = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            continue
        start = int(row[0])
        close_time = start + duration - 1
        if start_ms is not None and start < int(start_ms):
            continue
        if end_ms is not None and start > int(end_ms):
            continue
        # Exclude the currently-forming candle for both analysis and replay.
        if close_time >= now_ms:
            continue
        normalized.append([
            start, str(row[1]), str(row[2]), str(row[3]), str(row[4]),
            str(row[5]), close_time, str(row[6]), "0", "0", "0", "0",
        ])
    normalized.sort(key=lambda row: int(row[0]))
    return normalized


def bybit_rows_to_snapshot(rows: list[list], symbol: str) -> dict:
    candles = [{
        "open_time": int(row[0]), "open": float(row[1]),
        "high": float(row[2]), "low": float(row[3]), "close": float(row[4]),
        "volume": float(row[5]), "quote_volume": float(row[7]),
        "taker_buy_quote": 0.0, "close_time": int(row[6]),
    } for row in rows]
    return {"symbol": symbol, "candles": candles, "price_source": "Bybit linear perpetual"}
