"""Coinalyze API client for derivatives watchlist enrichment.

The API key is read only from COINALYZE_API_KEY. Never log or return it.
Official API: https://api.coinalyze.net/v1
"""
from __future__ import annotations

import os
import time
from collections import deque
from typing import Any, Dict, List, Optional

import requests

BASE_URL = "https://api.coinalyze.net/v1"
INTERVAL = "15min"
WINDOW_SECONDS = 6 * 60 * 60
MAX_REQUESTS_PER_MINUTE = 40


class CoinalyzeError(RuntimeError):
    pass


class CoinalyzeClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        session: Optional[requests.Session] = None,
        clock=time.time,
        sleeper=time.sleep,
    ) -> None:
        self.api_key = (api_key or os.getenv("COINALYZE_API_KEY", "")).strip()
        if not self.api_key:
            raise CoinalyzeError("COINALYZE_API_KEY is not configured")
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "OI-Coinalyze-Watchlist/1.0",
            "api_key": self.api_key,
        })
        self.clock = clock
        self.sleeper = sleeper
        self.request_times = deque()
        self._future_markets_cache: Optional[List[dict]] = None

    def _throttle(self) -> None:
        now = self.clock()
        while self.request_times and now - self.request_times[0] >= 60:
            self.request_times.popleft()
        if len(self.request_times) >= MAX_REQUESTS_PER_MINUTE:
            wait = max(0.0, 60 - (now - self.request_times[0])) + 0.05
            self.sleeper(wait)
            now = self.clock()
            while self.request_times and now - self.request_times[0] >= 60:
                self.request_times.popleft()
        self.request_times.append(self.clock())

    def _get(self, endpoint: str, params: Optional[dict] = None) -> Any:
        url = f"{BASE_URL}/{endpoint.lstrip('/')}"
        for attempt in range(3):
            self._throttle()
            response = self.session.get(url, params=params, timeout=15)
            if response.status_code == 429 and attempt < 2:
                retry_after = response.headers.get("Retry-After", "2")
                try:
                    delay = max(1.0, float(retry_after))
                except (TypeError, ValueError):
                    delay = 2.0 * (attempt + 1)
                self.sleeper(min(delay, 30.0))
                continue
            if response.status_code in (401, 403):
                raise CoinalyzeError("Coinalyze rejected the API key or its permissions")
            response.raise_for_status()
            try:
                return response.json()
            except ValueError as exc:
                raise CoinalyzeError(f"Invalid JSON from Coinalyze {endpoint}") from exc
        raise CoinalyzeError(f"Coinalyze rate limit persisted for {endpoint}")

    def get_future_markets(self) -> List[dict]:
        if self._future_markets_cache is not None:
            return self._future_markets_cache
        payload = self._get("future-markets")
        if not isinstance(payload, list):
            raise CoinalyzeError("Unexpected Coinalyze future-markets response")
        self._future_markets_cache = payload
        return payload

    def resolve_market(self, binance_symbol: str) -> str:
        if not binance_symbol.endswith("USDT"):
            raise CoinalyzeError(f"Unsupported Binance symbol: {binance_symbol}")
        base = binance_symbol[:-4]
        matches = [
            market for market in self.get_future_markets()
            if market.get("exchange", "").upper() == "BINANCE"
            and market.get("base_asset", "").upper() == base.upper()
            and market.get("quote_asset", "").upper() == "USDT"
            and market.get("is_perpetual") is True
        ]
        if not matches:
            raise CoinalyzeError(f"No Binance USDT perpetual mapping for {binance_symbol}")
        # Prefer the canonical stablecoin-margined perpetual if multiple records exist.
        matches.sort(key=lambda item: (
            item.get("margined") != "STABLE",
            item.get("symbol", ""),
        ))
        return str(matches[0]["symbol"])

    @staticmethod
    def _first(payload: Any, symbol: str) -> Optional[dict]:
        if not isinstance(payload, list):
            return None
        return next((row for row in payload if row.get("symbol") == symbol), None)

    def _history(self, endpoint: str, symbol: str, start: int, end: int) -> List[dict]:
        payload = self._get(endpoint, {
            "symbols": symbol,
            "interval": INTERVAL,
            "from": start,
            "to": end,
        })
        row = self._first(payload, symbol)
        history = row.get("history", []) if row else []
        return history if isinstance(history, list) else []

    def fetch_metrics(self, binance_symbol: str, now: Optional[int] = None) -> Dict[str, Any]:
        """Fetch a bounded Coinalyze derivatives snapshot for one Binance USDT perp."""
        market_symbol = self.resolve_market(binance_symbol)
        end = int(now if now is not None else self.clock())
        start = end - WINDOW_SECONDS

        current_oi = self._first(self._get("open-interest", {
            "symbols": market_symbol, "convert_to_usd": "true"
        }), market_symbol)
        current_funding = self._first(self._get("funding-rate", {
            "symbols": market_symbol
        }), market_symbol)
        predicted_funding = self._first(self._get("predicted-funding-rate", {
            "symbols": market_symbol
        }), market_symbol)

        oi_hist = self._history("open-interest-history", market_symbol, start, end)
        funding_hist = self._history("funding-rate-history", market_symbol, start, end)
        lsr_hist = self._history("long-short-ratio-history", market_symbol, start, end)
        liquidation_hist = self._history("liquidation-history", market_symbol, start, end)
        ohlcv_hist = self._history("ohlcv-history", market_symbol, start, end)

        oi_change_pct = None
        if len(oi_hist) >= 3:
            old = float(oi_hist[-3].get("c", 0) or 0)
            new = float(oi_hist[-1].get("c", 0) or 0)
            if old > 0:
                oi_change_pct = (new / old - 1.0) * 100.0

        last_lsr = lsr_hist[-1] if lsr_hist else {}
        last_liq = liquidation_hist[-1] if liquidation_hist else {}
        last_ohlcv = ohlcv_hist[-1] if ohlcv_hist else {}

        total_volume = float(last_ohlcv.get("v", 0) or 0)
        buy_volume = float(last_ohlcv.get("bv", 0) or 0)
        taker_buy_ratio = buy_volume / total_volume if total_volume > 0 else None

        return {
            "provider": "Coinalyze",
            "binance_symbol": binance_symbol,
            "market_symbol": market_symbol,
            "as_of": end,
            "current_open_interest_usd": (
                float(current_oi["value"]) if current_oi and current_oi.get("value") is not None else None
            ),
            "oi_change_15m_window_pct": oi_change_pct,
            "funding_rate": (
                float(current_funding["value"]) if current_funding and current_funding.get("value") is not None else None
            ),
            "predicted_funding_rate": (
                float(predicted_funding["value"]) if predicted_funding and predicted_funding.get("value") is not None else None
            ),
            "long_short_ratio": (
                float(last_lsr["r"]) if last_lsr.get("r") is not None else None
            ),
            "long_liquidations": (
                float(last_liq["l"]) if last_liq.get("l") is not None else None
            ),
            "short_liquidations": (
                float(last_liq["s"]) if last_liq.get("s") is not None else None
            ),
            "taker_buy_ratio": taker_buy_ratio,
            "data_quality": {
                "oi_history_points": len(oi_hist),
                "funding_history_points": len(funding_hist),
                "long_short_history_points": len(lsr_hist),
                "liquidation_history_points": len(liquidation_hist),
                "ohlcv_history_points": len(ohlcv_hist),
                "complete": all((current_oi, current_funding, oi_change_pct is not None,
                                 last_lsr, last_liq, last_ohlcv)),
            },
        }
