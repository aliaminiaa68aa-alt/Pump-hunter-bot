"""Binance Spot public market-data helpers. No derivatives venue is used for prices."""
from __future__ import annotations
import math
import time
import requests

BASE_URL = "https://api.binance.com"
INTERVALS = {"15": "15m", "15m": "15m", "1": "1m", "1m": "1m"}

def fetch_spot_rows(symbol: str, interval: str, start_ms: int | None = None,
                    end_ms: int | None = None, limit: int = 1000) -> list[list]:
    interval = INTERVALS.get(str(interval))
    if interval is None:
        raise ValueError("Unsupported Binance Spot interval")
    params = {"symbol": str(symbol).upper(), "interval": interval,
              "limit": min(max(int(limit), 1), 1000)}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    response = requests.get(BASE_URL + "/api/v3/klines", params=params, timeout=15,
                            headers={"User-Agent": "Pump-Hunter/1.0"})
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise RuntimeError("Unexpected Binance Spot kline response")
    duration = 900_000 if interval == "15m" else 60_000
    now_ms = int(time.time() * 1000)
    return [row for row in rows if isinstance(row, list) and len(row) >= 12
            and int(row[0]) + duration - 1 < now_ms]

def fetch_spot_live_price(symbol: str) -> float:
    response = requests.get(BASE_URL + "/api/v3/ticker/price",
                            params={"symbol": str(symbol).upper()}, timeout=12,
                            headers={"User-Agent": "Pump-Hunter/1.0"})
    response.raise_for_status()
    payload = response.json()
    price = float(payload.get("price", 0)) if isinstance(payload, dict) else 0
    if not math.isfinite(price) or price <= 0:
        raise RuntimeError("Invalid Binance Spot ticker price")
    return price

def fetch_spot_active_symbols() -> list[str]:
    response = requests.get(BASE_URL + "/api/v3/exchangeInfo", timeout=15,
                            headers={"User-Agent": "Pump-Hunter/1.0"})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected Binance Spot exchangeInfo response")
    return [str(item["symbol"]) for item in payload.get("symbols", [])
            if item.get("status") == "TRADING" and item.get("isSpotTradingAllowed", True)
            and item.get("quoteAsset") == "USDT" and str(item.get("symbol", "")).endswith("USDT")]
