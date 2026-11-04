"""Coinalyze API client for derivatives watchlist enrichment.

The API key is read only from COINALYZE_API_KEY. Never log or return it.
Official API: https://api.coinalyze.net/v1
"""
from __future__ import annotations

import os
import time
import math
from collections import deque
from typing import Any, Dict, List, Optional

import requests

BASE_URL = "https://api.coinalyze.net/v1"
INTERVAL = "15min"
WINDOW_SECONDS = 6 * 60 * 60
MAX_REQUESTS_PER_MINUTE = 40
MAX_ALIGNED_CANDLE_AGE_SECONDS = 15 * 60


class CoinalyzeError(RuntimeError):
    pass


class CoinalyzeClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        session: Optional[requests.Session] = None,
        clock=time.time,
        sleeper=time.sleep,
        include_optional_metrics: bool = True,
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
        self.include_optional_metrics = bool(include_optional_metrics)
        self.request_times = deque()
        self._future_markets_cache: Optional[List[dict]] = None
        self._binance_exchange_codes: Optional[set[str]] = None
        self._assets_cache: Optional[List[dict]] = None
        self._crypto_asset_symbols_cache: Optional[set[str]] = None

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

    def get_assets(self) -> List[dict]:
        """Return Coinalyze's asset catalogue, including its asset-category labels."""
        if self._assets_cache is not None:
            return self._assets_cache
        payload = self._get("assets")
        if not isinstance(payload, list) or not payload:
            raise CoinalyzeError("Unexpected or empty Coinalyze assets response")
        self._assets_cache = payload
        return payload

    def get_crypto_asset_symbols(self) -> set[str]:
        """Return only assets explicitly categorized as CRYPTO by Coinalyze."""
        if self._crypto_asset_symbols_cache is not None:
            return self._crypto_asset_symbols_cache
        symbols = set()
        for item in self.get_assets():
            if str(item.get("asset_category", "")).strip().upper() != "CRYPTO":
                continue
            symbol = str(item.get("symbol", "")).strip().upper()
            if "/" in symbol:
                symbol = symbol.split("/", 1)[0]
            if symbol.endswith("USDT"):
                symbol = symbol[:-4]
            if symbol:
                symbols.add(symbol)
        if not symbols:
            raise CoinalyzeError("Coinalyze assets catalogue contains no CRYPTO assets")
        self._crypto_asset_symbols_cache = symbols
        return symbols

    def is_crypto_asset(self, base_asset: str) -> bool:
        return str(base_asset).strip().upper() in self.get_crypto_asset_symbols()

    def get_future_markets(self) -> List[dict]:
        if self._future_markets_cache is not None:
            return self._future_markets_cache
        payload = self._get("future-markets")
        if not isinstance(payload, list):
            raise CoinalyzeError("Unexpected Coinalyze future-markets response")
        self._future_markets_cache = payload
        return payload

    def get_binance_exchange_codes(self) -> set[str]:
        """Resolve Coinalyze exchange codes to the Binance exchange name."""
        if self._binance_exchange_codes is not None:
            return self._binance_exchange_codes
        payload = self._get("exchanges")
        if not isinstance(payload, list):
            raise CoinalyzeError("Unexpected Coinalyze exchanges response")
        codes = {
            str(item.get("code", "")).upper()
            for item in payload
            if "BINANCE" in str(item.get("name", "")).upper()
            and item.get("code")
        }
        # Also accept a literal exchange name in market records for API variants.
        codes.add("BINANCE")
        self._binance_exchange_codes = codes
        return codes

    def resolve_market(self, binance_symbol: str) -> str:
        if not binance_symbol.endswith("USDT"):
            raise CoinalyzeError(f"Unsupported Binance symbol: {binance_symbol}")
        base = binance_symbol[:-4]
        binance_codes = self.get_binance_exchange_codes()
        matches = [
            market for market in self.get_future_markets()
            if str(market.get("exchange", "")).upper() in binance_codes
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

    def fetch_ohlcv_candles(self, binance_symbol: str, start_ms: int, end_ms: int) -> List[list]:
        """Fetch closed 15m OHLCV candles for outcome tracking via Coinalyze."""
        market_symbol = self.resolve_market(binance_symbol)
        history = self._history(
            "ohlcv-history", market_symbol,
            int(start_ms) // 1000, int(end_ms) // 1000,
        )
        candles = []
        for row in history:
            try:
                open_time = int(row["t"]) * 1000
                open_price = float(row["o"])
                high = float(row["h"])
                low = float(row["l"])
                close = float(row["c"])
                values = (open_price, high, low, close)
                if (not all(math.isfinite(value) for value in values)
                        or min(values) <= 0 or high < low):
                    continue
                candles.append([
                    open_time, str(open_price), str(high), str(low), str(close), "0"
                ])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        return sorted(candles, key=lambda item: int(item[0]))

    def fetch_metrics(self, binance_symbol: str, now: Optional[int] = None) -> Dict[str, Any]:
        """Fetch a bounded Coinalyze derivatives snapshot for one Binance USDT perp."""
        market_symbol = self.resolve_market(binance_symbol)
        end = int(now if now is not None else self.clock())
        start = end - WINDOW_SECONDS

        # The live entry engine needs only current funding plus aligned OI/OHLCV.
        # Supplementary OI USD, predicted funding, long/short and liquidation series
        # remain available for diagnostics, but are opt-in to protect scan capacity.
        current_oi = (
            self._first(self._get("open-interest", {
                "symbols": market_symbol, "convert_to_usd": "true"
            }), market_symbol)
            if self.include_optional_metrics else None
        )
        current_funding = self._first(self._get("funding-rate", {
            "symbols": market_symbol
        }), market_symbol)
        predicted_funding = (
            self._first(self._get("predicted-funding-rate", {
                "symbols": market_symbol
            }), market_symbol)
            if self.include_optional_metrics else None
        )

        oi_hist = self._history("open-interest-history", market_symbol, start, end)
        lsr_hist = (
            self._history("long-short-ratio-history", market_symbol, start, end)
            if self.include_optional_metrics else []
        )
        liquidation_hist = (
            self._history("liquidation-history", market_symbol, start, end)
            if self.include_optional_metrics else []
        )
        ohlcv_hist = self._history("ohlcv-history", market_symbol, start, end)

        oi_change_pct = None

        last_lsr = lsr_hist[-1] if lsr_hist else {}
        last_liq = liquidation_hist[-1] if liquidation_hist else {}
        last_ohlcv = ohlcv_hist[-1] if ohlcv_hist else {}

        # Build a closed 15m snapshot from Coinalyze OHLCV + OI history.
        # This is also the fallback when Binance public futures endpoints return HTTP 451.
        market_snapshot = None
        oi_by_time = {
            int(row.get("t", 0)): row for row in oi_hist
            if row.get("t") is not None and row.get("c") is not None
        }
        closed_ohlcv = sorted(
            (row for row in ohlcv_hist
             if row.get("t") is not None and int(row["t"]) + 15 * 60 <= end),
            key=lambda row: int(row["t"]),
        )
        aligned = [
            row for row in closed_ohlcv if int(row["t"]) in oi_by_time
        ]
        # A correctly aligned candle can still be too old to support a live entry.
        # Match the Binance path's 15m staleness tolerance and fail closed when
        # the newest common OHLCV/OI candle is older than one extra interval.
        if aligned:
            latest_aligned_close = int(aligned[-1]["t"]) + 15 * 60
            aligned_age = end - latest_aligned_close
            if aligned_age < 0 or aligned_age > MAX_ALIGNED_CANDLE_AGE_SECONDS:
                aligned = []
        # Confirm OI and taker flow against the same latest CLOSED 15m candle.
        aligned_metrics = aligned[-3:]
        cadence_ok = (
            len(aligned_metrics) == 3
            and all(
                int(b["t"]) - int(a["t"]) == 15 * 60
                for a, b in zip(aligned_metrics, aligned_metrics[1:])
            )
        )
        if cadence_ok:
            previous_oi = float(oi_by_time[int(aligned_metrics[-2]["t"])].get("c", 0) or 0)
            new_oi = float(oi_by_time[int(aligned_metrics[-1]["t"])].get("c", 0) or 0)
            if previous_oi > 0 and new_oi > 0:
                oi_change_pct = (new_oi / previous_oi - 1.0) * 100.0
        if len(closed_ohlcv) >= 22 and len(aligned) >= 3:
            latest_aligned_time = int(aligned[-1]["t"])
            aligned = aligned[-3:]
            if cadence_ok:
                parsed_candles = []
                valid_candles = True
                # Never pair a newer OHLCV candle with an older OI observation.
                common_closed_ohlcv = [
                    row for row in closed_ohlcv
                    if int(row["t"]) <= latest_aligned_time
                ]
                for row in common_closed_ohlcv:
                    try:
                        open_price = float(row["o"])
                        high_price = float(row["h"])
                        low_price = float(row["l"])
                        close_price = float(row["c"])
                        volume = float(row["v"])
                        buy_volume = float(row["bv"])
                        values = (open_price, high_price, low_price, close_price, volume, buy_volume)
                        if (not all(math.isfinite(value) for value in values)
                                or min(open_price, high_price, low_price, close_price, volume) <= 0
                                or buy_volume < 0 or buy_volume > volume):
                            valid_candles = False
                            break
                        # Coinalyze OHLCV exposes base-asset volume, not quote turnover.
                        # Estimate quote volume with OHLC typical price; do not pretend
                        # that close*volume is the exact traded quote volume.
                        typical_price = (open_price + high_price + low_price + close_price) / 4.0
                    except (KeyError, TypeError, ValueError):
                        valid_candles = False
                        break
                    open_ms = int(row["t"]) * 1000
                    parsed_candles.append({
                        "open_time": open_ms,
                        "open": open_price,
                        "high": high_price,
                        "low": low_price,
                        "close": close_price,
                        "volume": volume,
                        "quote_volume": volume * typical_price,
                        "taker_buy_quote": buy_volume * typical_price,
                        "close_time": open_ms + 15 * 60 * 1000 - 1,
                    })
                oi_values = [float(oi_by_time[int(row["t"])]["c"]) for row in aligned]
                if valid_candles and all(value > 0 for value in oi_values):
                    last_oi_time = int(aligned[-1]["t"])
                    market_snapshot = {
                        "symbol": binance_symbol,
                        "candles": parsed_candles,
                        "oi_values": oi_values,
                        "oi_timestamp": (last_oi_time + 15 * 60) * 1000 - 1,
                        "funding_rate": (
                            float(current_funding["value"])
                            if current_funding and current_funding.get("value") is not None
                            else 0.0
                        ),
                    }

        # Taker flow must refer to the same latest closed candle that has aligned OI.
        latest_aligned_ohlcv = aligned[-1] if aligned else {}
        total_volume = float(latest_aligned_ohlcv.get("v", 0) or 0)
        buy_volume = float(latest_aligned_ohlcv.get("bv", 0) or 0)
        taker_buy_ratio = buy_volume / total_volume if total_volume > 0 else None

        return {
            "provider": "Coinalyze",
            "binance_symbol": binance_symbol,
            "market_snapshot": market_snapshot,
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
                "long_short_history_points": len(lsr_hist),
                "liquidation_history_points": len(liquidation_hist),
                "ohlcv_history_points": len(ohlcv_hist),
                "complete": bool(
                    current_funding and oi_change_pct is not None and last_ohlcv
                    and (
                        not self.include_optional_metrics
                        or (current_oi and last_lsr and last_liq)
                    )
                ),
            },
        }
