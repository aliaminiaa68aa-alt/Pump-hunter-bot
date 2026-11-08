"""Tick-based outcome monitor for continuously running hosts.

This module consumes current Binance USD-M contract prices. It is deliberately
separate from the scheduled signal scanner so a host can monitor open trades
without repeating the expensive market-universe scan.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Mapping


ACTIVE_STATUSES = {"WAITING_ENTRY", "OPEN", "TP1_HIT"}
MAX_PENDING_ENTRY_MS = 60 * 60 * 1000
# Reject a polling fill if the observed price has moved too far beyond the
# planned entry in the adverse direction. This prevents chasing stale alerts.
MAX_ENTRY_SLIPPAGE_PCT = 0.15
TERMINAL_STATUSES = {
    "CLOSED_SL", "CLOSED_TP2", "TIMED_OUT", "EXPIRED_NO_ENTRY",
    "AMBIGUOUS_ENTRY_CANDLE", "AMBIGUOUS_ENTRY_POLL", "MISSED_ENTRY", "INVALID_NO_ENTRY",
}


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 and number < float("inf") else None


def apply_live_prices(
    journal: Dict[str, Any],
    prices: Mapping[str, float],
    now_ms: int | None = None,
) -> Dict[str, Any]:
    """Apply one price snapshot to every active trade, idempotently.

    The first snapshot establishes a baseline. If the entry level has already
    been crossed on that first observation, the setup is marked MISSED_ENTRY:
    the monitor cannot prove that the touch happened after the signal.
    Subsequent threshold crossings are timestamped at observation time.
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    normalized = {
        str(symbol).upper(): parsed
        for symbol, value in prices.items()
        if (parsed := _finite_positive(value)) is not None
    }

    for trade_id, record in journal.setdefault("trades", {}).items():
        status = str(record.get("status", "")).upper()
        if status not in ACTIVE_STATUSES:
            continue
        signal = record.get("signal") or {}
        symbol = str(signal.get("symbol", "")).upper()
        # Pending entries expire one hour after the alert was sent, even if
        # the current symbol price is temporarily unavailable.
        if status == "WAITING_ENTRY":
            sent_at = _finite_positive(record.get("sent_at_ms"))
            if sent_at is not None and now - int(sent_at) >= MAX_PENDING_ENTRY_MS:
                record["status"] = "EXPIRED_NO_ENTRY"
                record["entry_expired_reason"] = "Pending entry exceeded the one-hour validity window"
                record["exit_time_ms"] = now
                continue

        price = normalized.get(symbol)
        if price is None:
            continue

        side = str(signal.get("side", "LONG")).upper()
        if side not in {"LONG", "SHORT"}:
            record["last_error"] = f"Unsupported side: {side}"
            continue
        try:
            entry = float(signal["entry"])
            original_stop = float(signal["stop_loss"])
            tp1 = float(signal["take_profit_1"])
            tp2 = float(signal["take_profit_2"])
        except (KeyError, TypeError, ValueError, OverflowError):
            record["last_error"] = "Invalid entry/SL/TP levels"
            continue

        # A setup is no longer actionable if price reaches its first target
        # before the planned entry is filled. Do not chase the move or later
        # activate this stale entry if price retraces.
        if status == "WAITING_ENTRY":
            target_reached_before_entry = (
                price >= tp1 if side == "LONG" else price <= tp1
            )
            if target_reached_before_entry:
                record["status"] = "EXPIRED_NO_ENTRY"
                record["entry_expired_reason"] = "TP1 reached before entry; setup is no longer actionable"
                record["exit_time_ms"] = now
                record["live_last_price"] = price
                record["live_last_price_ms"] = now
                continue

        previous_price = _finite_positive(record.get("live_last_price"))
        if previous_price is None:
            already_crossed = price <= entry if side == "LONG" else price >= entry
            if status == "WAITING_ENTRY" and already_crossed:
                slippage_pct = (
                    (entry - price) / entry * 100.0
                    if side == "LONG"
                    else (price - entry) / entry * 100.0
                )
                record["status"] = "MISSED_ENTRY"
                record["entry_missed"] = True
                record["entry_missed_reason"] = (
                    "First live price observation was already beyond the entry; "
                    "a post-signal touch cannot be verified"
                )
                record["entry_observed_price"] = price
                record["entry_slippage_pct"] = slippage_pct
                record.setdefault("events", []).append({
                    "event": "MISSED_ENTRY",
                    "time_ms": now,
                    "planned_entry": entry,
                    "observed_price": price,
                    "slippage_pct": slippage_pct,
                    "max_slippage_pct": MAX_ENTRY_SLIPPAGE_PCT,
                    "source": "first_live_price_observation",
                })
                record["exit_time_ms"] = now
            record["live_last_price"] = price
            record["live_last_price_ms"] = now
            record["live_monitor_started_ms"] = now
            continue

        if status == "WAITING_ENTRY":
            crossed_entry = (
                previous_price > entry and price <= entry
                if side == "LONG"
                else previous_price < entry and price >= entry
            )
            if not crossed_entry:
                record["live_last_price"] = price
                record["live_last_price_ms"] = now
                continue
            # A poll can observe the market after it has already gapped
            # through the planned entry. Do not chase an adverse move: the
            # signal is marked missed, not opened at a materially worse price.
            adverse_slippage_pct = (
                (entry - price) / entry * 100.0
                if side == "LONG"
                else (price - entry) / entry * 100.0
            )
            if adverse_slippage_pct > MAX_ENTRY_SLIPPAGE_PCT and not (price <= original_stop if side == "LONG" else price >= original_stop):
                record["status"] = "MISSED_ENTRY"
                record["entry_missed"] = True
                record["entry_missed_reason"] = (
                    f"Adverse entry slippage {adverse_slippage_pct:.4f}% "
                    f"exceeded {MAX_ENTRY_SLIPPAGE_PCT:.2f}%"
                )
                record["entry_observed_price"] = price
                record["entry_slippage_pct"] = adverse_slippage_pct
                record.setdefault("events", []).append({
                    "event": "MISSED_ENTRY",
                    "time_ms": now,
                    "planned_entry": entry,
                    "observed_price": price,
                    "slippage_pct": adverse_slippage_pct,
                    "max_slippage_pct": MAX_ENTRY_SLIPPAGE_PCT,
                    "source": "live_price_poll",
                })
                record["exit_time_ms"] = now
                record["live_last_price"] = price
                record["live_last_price_ms"] = now
                record["last_checked_ms"] = now
                continue

            # Check the protective stop before recording a confirmed fill.
            # If one poll first observes price beyond both entry and stop, the
            # intrapoll order is unknowable: record only an ambiguous touch.
            # This prevents a misleading ENTRY notification followed by an
            # immediate "ambiguous" notification for the same snapshot.
            entry_stop_also_crossed = (
                price <= original_stop if side == "LONG"
                else price >= original_stop
            )
            if entry_stop_also_crossed:
                record["status"] = "AMBIGUOUS_ENTRY_POLL"
                record["entry_poll_ambiguous"] = True
                record["possible_entry_fill_price"] = entry
                record["possible_entry_time_ms"] = now
                record["exit_time_ms"] = now
                record.setdefault("events", []).append({
                    "event": "AMBIGUOUS_ENTRY_POLL",
                    "time_ms": now,
                    "possible_entry_fill": entry,
                    "observed_price": price,
                    "stop_also_crossed": True,
                    "source": "live_price_poll",
                })
                record["live_last_price"] = price
                record["live_last_price_ms"] = now
                record["last_checked_ms"] = now
                continue

            fill = entry
            record["entry_fill_price"] = fill
            record["entry_time_ms"] = now
            record["status"] = "OPEN"
            record.setdefault("events", []).append({
                "event": "ENTRY", "time_ms": now, "fill": fill,
                "source": "live_price_poll",
            })
            status = "OPEN"

        events = record.setdefault("events", [])
        event_names = {str(event.get("event")) for event in events}
        stop = entry if "TP1" in event_names else original_stop

        stop_crossed = (
            price <= stop if side == "LONG" else price >= stop
        )
        tp1_crossed = (
            price >= tp1 if side == "LONG" else price <= tp1
        )
        tp2_crossed = (
            price >= tp2 if side == "LONG" else price <= tp2
        )

        # If a single poll interval spans adverse and favorable barriers, the
        # order is unknowable. Resolve conservatively as stop-first.
        if stop_crossed:
            if "SL" not in event_names:
                events.append({
                    "event": "SL", "time_ms": now, "fill": price,
                    "source": "live_price_poll",
                    "ambiguous_same_poll": bool(tp1_crossed or tp2_crossed),
                })
            record["status"] = "CLOSED_SL"
            record["exit_time_ms"] = now
        else:
            if tp1_crossed and "TP1" not in event_names:
                events.append({
                    "event": "TP1", "time_ms": now, "fill": tp1,
                    "source": "live_price_poll",
                })
                record["status"] = "TP1_HIT"
                record["breakeven_stop_active"] = True
                record["breakeven_stop_price"] = entry
            if tp2_crossed and "TP2" not in event_names:
                events.append({
                    "event": "TP2", "time_ms": now, "fill": tp2,
                    "source": "live_price_poll",
                })
                record["status"] = "CLOSED_TP2"
                record["exit_time_ms"] = now

        record["live_last_price"] = price
        record["live_last_price_ms"] = now
        record["last_checked_ms"] = now

    journal["updated_at_ms"] = now
    return journal
