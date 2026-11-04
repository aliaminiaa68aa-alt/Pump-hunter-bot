"""Fresh market-price validation for confirmed OI entries.

Uses Gate.io's public USDT perpetual trade tape. A signal is not sent if the
latest trade is stale, unavailable, or too far from the strategy entry.
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict

import requests

BASE_URL = "https://api.gateio.ws/api/v4"
MAX_TRADE_AGE_SECONDS = 30
MAX_CLOCK_SKEW_SECONDS = 3
MAX_ENTRY_DEVIATION_PCT = 0.50
RISK_FRACTION_TOLERANCE = 0.20


class EntryPriceError(RuntimeError):
    """Raised when a trustworthy current market price cannot be obtained."""


def gate_contract_for_symbol(symbol: str) -> str:
    value = str(symbol or "").upper().strip()
    if not value.endswith("USDT") or len(value) <= 4:
        raise EntryPriceError(f"Unsupported USDT perpetual symbol: {symbol!r}")
    base = value[:-4]
    if not base.replace("_", "").isalnum():
        raise EntryPriceError(f"Invalid symbol for Gate contract mapping: {symbol!r}")
    return f"{base}_USDT"


def fetch_gate_last_trade(
    symbol: str,
    session: Any = requests,
    now: float | None = None,
    max_age_seconds: int = MAX_TRADE_AGE_SECONDS,
) -> Dict[str, float]:
    """Fetch the latest public Gate USDT-perpetual trade and validate its age."""
    contract = gate_contract_for_symbol(symbol)
    try:
        response = session.get(
            f"{BASE_URL}/futures/usdt/trades",
            params={"contract": contract, "limit": 1},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise EntryPriceError(f"Gate latest-trade request failed for {contract}: {exc}") from exc

    if not isinstance(payload, list) or not payload:
        raise EntryPriceError(f"Gate returned no recent trades for {contract}")
    row = payload[0]
    if not isinstance(row, dict) or str(row.get("contract", "")).upper() != contract:
        raise EntryPriceError(f"Gate returned an unexpected trade for {contract}")

    try:
        price = float(row["price"])
        trade_time = float(row["create_time"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise EntryPriceError(f"Gate trade data is missing price/time for {contract}") from exc

    if not math.isfinite(price) or price <= 0 or not math.isfinite(trade_time) or trade_time <= 0:
        raise EntryPriceError(f"Gate trade data is invalid for {contract}")

    current = float(now if now is not None else time.time())
    age = current - trade_time
    if age < -MAX_CLOCK_SKEW_SECONDS:
        raise EntryPriceError(f"Gate trade timestamp is in the future for {contract}")
    if age > max_age_seconds:
        raise EntryPriceError(
            f"Gate latest trade is stale for {contract}: age={age:.1f}s "
            f"(limit={max_age_seconds}s)"
        )
    return {"price": price, "trade_time": trade_time, "age_seconds": max(0.0, age)}


def evaluate_entry_freshness(signal: Any, market_price: float) -> Dict[str, Any]:
    """Reject both adverse slippage and favorable price-chasing beyond risk tolerance."""
    try:
        entry = float(signal.entry)
        stop = float(signal.stop_loss)
        current = float(market_price)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise EntryPriceError("Signal entry, stop, or market price is invalid") from exc

    if not all(math.isfinite(value) and value > 0 for value in (entry, stop, current)):
        raise EntryPriceError("Signal entry, stop, or market price is non-positive/non-finite")
    risk_pct = abs(entry - stop) / entry * 100.0
    if risk_pct <= 0:
        raise EntryPriceError("Signal stop distance is invalid")

    # Allow at most 20% of planned stop-risk as entry drift, capped at 0.50%.
    # The absolute deviation is used so the bot never encourages chasing a move.
    tolerance_pct = min(MAX_ENTRY_DEVIATION_PCT, risk_pct * RISK_FRACTION_TOLERANCE)
    deviation_pct = (current - entry) / entry * 100.0
    accepted = abs(deviation_pct) <= tolerance_pct
    return {
        "accepted": accepted,
        "market_price": current,
        "deviation_pct": deviation_pct,
        "tolerance_pct": tolerance_pct,
        "risk_pct": risk_pct,
        "reason": (
            "fresh" if accepted
            else f"entry drift {deviation_pct:+.3f}% exceeds {tolerance_pct:.3f}%"
        ),
    }
