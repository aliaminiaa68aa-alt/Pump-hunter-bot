"""Public Bybit linear-perpetual OHLCV fallback for Binance market candles.

Coinalyze remains exclusively a derivatives-metrics provider. This module
never requests OI, funding, or other derivatives positioning data.
"""
from __future__ import annotations

import time
from typing import Any

import requests

BYBIT_BASE_URL = "https://api.bybit.com"
GATE_BASE_URLS = ("https://api.gateio.ws", "https://fx-api.gateio.ws")
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



def fetch_gateio_rows(symbol: str, interval: str, start_ms: int | None = None,
                      end_ms: int | None = None, limit: int = 1000) -> list[list]:
    """Fetch public Gate.io USDT perpetual candles and normalize to Binance row layout."""
    if interval not in {"15m", "1m"}:
        raise ValueError(f"Unsupported Gate.io interval: {interval}")
    contract = symbol[:-4] + "_USDT" if symbol.endswith("USDT") else symbol
    params: dict[str, Any] = {"contract": contract, "interval": interval}
    if start_ms is not None and end_ms is not None:
        params["from"] = int(start_ms) // 1000
        params["to"] = int(end_ms) // 1000
    else:
        params["limit"] = min(max(int(limit), 1), 2000)
    last_error: Exception | None = None
    payload = None
    for base in GATE_BASE_URLS:
        try:
            response = requests.get(
                base + "/api/v4/futures/usdt/candlesticks",
                params=params, timeout=15,
                headers={"Accept": "application/json", "User-Agent": "Pump-Hunter/1.0"},
            )
            response.raise_for_status()
            payload = response.json()
            break
        except Exception as exc:
            last_error = exc
    if payload is None:
        raise RuntimeError(f"Gate.io candle API unavailable: {last_error}")
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected Gate.io candle response: {payload}")
    duration = 15 * 60 * 1000 if interval == "15m" else 60 * 1000
    now_ms = int(time.time() * 1000)
    normalized = []
    for row in payload:
        if isinstance(row, dict):
            start = int(row.get("t", 0)) * 1000
            open_price, high, low, close = row.get("o"), row.get("h"), row.get("l"), row.get("c")
            volume = row.get("v", 0)
            quote_volume = row.get("sum", row.get("v", 0))
        elif isinstance(row, (list, tuple)) and len(row) >= 6:
            start = int(row[0]) * 1000
            quote_volume, close, high, low, open_price = row[1:6]
            volume = row[6] if len(row) > 6 else quote_volume
        else:
            continue
        close_time = start + duration - 1
        if start_ms is not None and start < int(start_ms):
            continue
        if end_ms is not None and start > int(end_ms):
            continue
        if close_time >= now_ms:
            continue
        normalized.append([
            start, str(open_price), str(high), str(low), str(close), str(volume),
            close_time, str(quote_volume), "0", "0", "0", "0",
        ])
    normalized.sort(key=lambda row: int(row[0]))
    return normalized
