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
MAX_PENDING_ENTRY_HOURS = 1


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
    data["engine"] = metrics.get("engine", data.get("engine", "UNSPECIFIED"))
    trade_id = f"{data['engine']}:{data['symbol']}:{data['side']}:{int(data['candle_close_time'])}"
    return {
        "trade_id": trade_id,
        "signal": data,
        "source": {
            "provider": metrics.get("provider", "Coinalyze + signal-engine market feed"),
            "data_as_of": metrics.get("as_of"),
            "coinalyze_oi_change_15m_pct": metrics.get("oi_change_15m_window_pct"),
            "coinalyze_taker_buy_ratio": metrics.get("taker_buy_ratio"),
            "funding_rate_raw": metrics.get("funding_rate"),
            "predicted_funding_rate_raw": metrics.get("predicted_funding_rate"),
        },
        "sent_at_ms": int(sent_at_ms),
        "last_checked_ms": int(data["candle_close_time"]) + 1,
        "status": "WAITING_ENTRY",
        "events": [],
        "entry_fill_price": None,
        "entry_time_ms": None,
        "mfe_pct": 0.0,
        "mae_pct": 0.0,
        "mae_before_tp1_pct": 0.0,
        "mfe_before_tp1_pct": 0.0,
        "tp1_candle_excursion_ambiguous": False,
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
    micro_fetcher: Callable[[str, int, int], List[list]] | None = None,
) -> Dict[str, Any]:
    """Update active records using 1m replay when available, otherwise 15m candles."""
    active_fetcher = micro_fetcher or fetcher
    interval_ms = 60 * 1000 if micro_fetcher is not None else INTERVAL_MS
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    trades = journal.setdefault("trades", {})
    for trade_id, record in list(trades.items()):
        status = record.get("status")
        signal = record["signal"]

        # Keep observing stopped trades for 24h to measure post-stop follow-through.
        # Historical status remains CLOSED_SL; this is diagnostic, not a reopened trade.
        if status == "CLOSED_SL":
            post_stop_until = int(record.get("post_stop_until_ms",
                                             int(record.get("exit_time_ms", now)) + 24 * 60 * 60 * 1000))
            if now > post_stop_until:
                record["post_stop_tracking"] = "COMPLETE"
                continue
            try:
                cursor = int(record.get("post_stop_last_checked_ms",
                                        int(record.get("exit_time_ms", now)) + 1))
                # The monitor runs every five minutes, but only closed 15m candles
                # can change an outcome. Avoid spending an API request between closes.
                if now < cursor + interval_ms:
                    record["post_stop_tracking"] = "ACTIVE"
                    continue
                candles = sorted(active_fetcher(signal["symbol"], cursor, min(now, post_stop_until)),
                                 key=lambda row: int(row[0]))
                for candle in candles:
                    open_ms = int(candle[0])
                    close_ms = open_ms + interval_ms - 1
                    if close_ms >= now:
                        continue
                    high, low = float(candle[2]), float(candle[3])
                    entry = float(signal["entry"])
                    side = signal["side"]
                    favorable = ((high / entry - 1.0) * 100.0 if side == "LONG"
                                 else (entry / low - 1.0) * 100.0)
                    record["post_stop_mfe_pct"] = max(
                        float(record.get("post_stop_mfe_pct", 0.0)), favorable
                    )
                    tp1 = float(signal["take_profit_1"])
                    tp2 = float(signal["take_profit_2"])
                    if (high >= tp1 if side == "LONG" else low <= tp1):
                        record["post_stop_tp1_reached"] = True
                    if (high >= tp2 if side == "LONG" else low <= tp2):
                        record["post_stop_tp2_reached"] = True
                    record["post_stop_last_checked_ms"] = close_ms + 1
                record["post_stop_tracking"] = "COMPLETE" if now >= post_stop_until else "ACTIVE"
            except Exception as exc:
                record["post_stop_error"] = f"{type(exc).__name__}: {exc}"
            continue

        if status in {"CLOSED_TP2", "TIMED_OUT", "EXPIRED_NO_ENTRY"}:
            continue
        side = signal["side"]
        entry = float(record.get("entry_fill_price") or signal["entry"])
        try:
            cursor = int(record.get("last_checked_ms", int(signal["candle_close_time"]) + 1))
            # No closed candle can exist yet; keep timeout handling but skip the provider call.
            if now < cursor + interval_ms:
                timeout_hours = (MAX_PENDING_ENTRY_HOURS if status == "WAITING_ENTRY"
                                  else MAX_ACTIVE_HOURS)
                if now - int(record["sent_at_ms"]) >= timeout_hours * 60 * 60 * 1000:
                    record["status"] = ("EXPIRED_NO_ENTRY" if status == "WAITING_ENTRY"
                                        else "TIMED_OUT")
                    record["exit_time_ms"] = now
                continue
            candles = active_fetcher(signal["symbol"], cursor, now)
            candles = sorted(candles, key=lambda row: int(row[0]))
            for candle in candles:
                open_ms = int(candle[0])
                close_ms = open_ms + interval_ms - 1
                if close_ms >= now:
                    continue
                event_names = {event["event"] for event in record.get("events", [])}
                if record.get("status") == "WAITING_ENTRY":
                    entry_level = float(signal["entry"])
                    high, low, open_price = float(candle[2]), float(candle[3]), float(candle[1])
                    if not (low <= entry_level <= high):
                        record["last_checked_ms"] = close_ms + 1
                        continue
                    fill = (min(open_price, entry_level) if side == "LONG"
                            else max(open_price, entry_level))
                    record["entry_fill_price"] = fill
                    record["entry_time_ms"] = close_ms
                    record["status"] = "OPEN"
                    record.setdefault("events", []).append({
                        "event": "ENTRY", "time_ms": close_ms, "fill": fill,
                    })
                    entry = fill
                    event_names.add("ENTRY")
                # After TP1, the remaining position is protected at breakeven.
                # The TP1 candle itself is still evaluated against the original
                # stop because OHLC cannot establish intrabar event ordering.
                active_signal = signal
                if "TP1" in event_names:
                    active_signal = dict(signal)
                    active_signal["stop_loss"] = entry
                    record["breakeven_stop_active"] = True
                    record["breakeven_stop_price"] = entry
                stop_hit, tp1_hit, tp2_hit, stop_fill, tp1_fill, tp2_fill = _touches(
                    side, candle, active_signal
                )

                # OHLC cannot reveal intrabar order. If a stop and any target
                # coexist in one candle, assume the stop happened first and do
                # not credit later unknown intrabar extremes to MFE/MAE.
                if stop_hit:
                    if "TP1" not in event_names and not tp1_hit:
                        stop_adverse = (abs(stop_fill - entry) / entry * 100.0) if entry else 0.0
                        record["mae_before_tp1_pct"] = max(
                            float(record.get("mae_before_tp1_pct", 0.0)), stop_adverse
                        )
                    elif tp1_hit:
                        record["tp1_candle_excursion_ambiguous"] = True
                    risk = abs(entry - float(signal["stop_loss"]))
                    fill = stop_fill
                    signed = (fill - entry) if side == "LONG" else (entry - fill)
                    ambiguous_targets = bool(tp1_hit or tp2_hit)
                    record.setdefault("events", []).append({
                        "event": "SL", "time_ms": close_ms, "fill": fill,
                        "r_multiple": signed / risk if risk else None,
                        "ambiguous_same_candle": ambiguous_targets,
                        "same_candle_targets": [
                            name for name, touched in (("TP1", tp1_hit), ("TP2", tp2_hit))
                            if touched
                        ],
                    })
                    if ambiguous_targets:
                        record["ambiguous_stop_target_candles"] = (
                            int(record.get("ambiguous_stop_target_candles", 0)) + 1
                        )
                    record["status"] = "CLOSED_SL"
                    record["exit_time_ms"] = close_ms
                    record["post_stop_last_checked_ms"] = close_ms + 1
                    record["post_stop_until_ms"] = close_ms + 24 * 60 * 60 * 1000
                    record["post_stop_mfe_pct"] = 0.0
                    record["post_stop_tp1_reached"] = False
                    record["post_stop_tp2_reached"] = False
                    record["post_stop_tracking"] = "ACTIVE"
                    record["mae_pct"] = max(
                        float(record.get("mae_pct", 0.0)),
                        abs(fill - entry) / entry * 100.0 if entry else 0.0,
                    )
                    break

                high, low = float(candle[2]), float(candle[3])
                mfe, mae = _price_extremes(side, entry, high, low)
                record["mfe_pct"] = max(float(record.get("mfe_pct", 0.0)), mfe)
                record["mae_pct"] = max(float(record.get("mae_pct", 0.0)), mae)

                if "TP1" not in event_names:
                    if tp1_hit:
                        # Intrabar order is unknown: exclude both excursions from
                        # the candle that first touched TP1.
                        record["tp1_candle_excursion_ambiguous"] = True
                    else:
                        adverse_before_tp1 = (
                            (entry - low) / entry * 100.0 if side == "LONG"
                            else (high - entry) / entry * 100.0
                        )
                        favorable_before_tp1 = (
                            (high - entry) / entry * 100.0 if side == "LONG"
                            else (entry - low) / entry * 100.0
                        )
                        record["mae_before_tp1_pct"] = max(
                            float(record.get("mae_before_tp1_pct", 0.0)),
                            max(0.0, adverse_before_tp1),
                        )
                        record["mfe_before_tp1_pct"] = max(
                            float(record.get("mfe_before_tp1_pct", 0.0)),
                            max(0.0, favorable_before_tp1),
                        )

                if tp1_hit and "TP1" not in event_names:
                    risk = abs(entry - float(signal["stop_loss"]))
                    signed = (tp1_fill - entry) if side == "LONG" else (entry - tp1_fill)
                    record.setdefault("events", []).append({
                        "event": "TP1", "time_ms": close_ms, "fill": tp1_fill,
                        "r_multiple": signed / risk if risk else None,
                    })
                    record["status"] = "TP1_HIT"
                    record["breakeven_stop_active"] = True
                    record["breakeven_stop_price"] = entry

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

            timeout_hours = (MAX_PENDING_ENTRY_HOURS
                              if record.get("status") == "WAITING_ENTRY"
                              else MAX_ACTIVE_HOURS)
            if now - int(record["sent_at_ms"]) >= timeout_hours * 60 * 60 * 1000:
                if record.get("status") not in {"CLOSED_SL", "CLOSED_TP2"}:
                    record["status"] = ("EXPIRED_NO_ENTRY"
                                        if record.get("status") == "WAITING_ENTRY"
                                        else "TIMED_OUT")
                    record["exit_time_ms"] = now
        except Exception as exc:
            record["last_error"] = f"{type(exc).__name__}: {exc}"
    # Bound history without deleting unresolved trades.
    closed = [
        (key, value) for key, value in trades.items()
        if value.get("status") in {"CLOSED_SL", "CLOSED_TP2", "TIMED_OUT", "EXPIRED_NO_ENTRY"}
    ]
    if len(closed) > 500:
        for key, _ in sorted(closed, key=lambda item: item[1].get("sent_at_ms", 0))[:-500]:
            trades.pop(key, None)
    journal["updated_at_ms"] = now
    return journal


def summarize_outcomes(journal: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize observed outcomes without treating ambiguous OHLC candles as certain."""
    trades = list(journal.get("trades", {}).values())
    report: Dict[str, Any] = {
        "total": len(trades), "statuses": {}, "tp1_before_sl": 0,
        "sl_before_tp1": 0, "sl_then_later_tp1": 0, "sl_then_later_tp2": 0,
        "tp2_reached": 0, "ambiguous_stop_target": 0, "provider_errors": 0,
        "entry_touched": 0, "not_entered": 0, "entry_touched_decided": 0,
        "entry_touched_win_rate_pct": None,
        "groups": {}, "by_engine_side": {}, "trade_details": [],
    }
    groups: Dict[str, List[dict]] = {}
    for record in trades:
        status = str(record.get("status", "UNKNOWN"))
        report["statuses"][status] = report["statuses"].get(status, 0) + 1
        events = sorted(record.get("events", []), key=lambda event: int(event.get("time_ms", 0)))
        names = [event.get("event") for event in events]
        # Older journal records predate explicit ENTRY events. A recorded
        # TP/SL event or an active/closed trade status proves the entry was
        # reached, so keep historical outcome statistics backward-compatible.
        entered = (
            "ENTRY" in names
            or record.get("entry_fill_price") is not None
            or any(name in {"SL", "TP1", "TP2"} for name in names)
            or status in {"OPEN", "TP1_HIT", "CLOSED_SL", "CLOSED_TP2"}
        )
        if entered:
            report["entry_touched"] += 1
        else:
            report["not_entered"] += 1
        first_tp1 = next((i for i, name in enumerate(names) if name == "TP1"), None)
        first_sl = next((i for i, name in enumerate(names) if name == "SL"), None)
        first_sl_ambiguous = (
            first_sl is not None and bool(events[first_sl].get("ambiguous_same_candle"))
        )
        if first_sl_ambiguous:
            outcome = "ambiguous_stop_target"
            if entered:
                report["entry_touched_decided"] += 1
        elif first_tp1 is not None and (first_sl is None or first_tp1 < first_sl):
            report["tp1_before_sl"] += 1
            outcome = "tp1_before_sl"
            if entered:
                report["entry_touched_decided"] += 1
        elif first_sl is not None:
            report["sl_before_tp1"] += 1
            outcome = "sl_before_tp1"
            if entered:
                report["entry_touched_decided"] += 1
        else:
            outcome = status.lower()
        if record.get("post_stop_tp1_reached"):
            report["sl_then_later_tp1"] += 1
        if record.get("post_stop_tp2_reached"):
            report["sl_then_later_tp2"] += 1
        if "TP2" in names or record.get("post_stop_tp2_reached"):
            report["tp2_reached"] += 1
        if any(event.get("ambiguous_same_candle") for event in events):
            report["ambiguous_stop_target"] += 1
        if record.get("last_error") or record.get("post_stop_error"):
            report["provider_errors"] += 1
        signal = record.get("signal", {})
        metrics = {
            "movement_strength": signal.get("movement_strength"),
            "entry_quality": signal.get("entry_quality"),
            "atr_15m": signal.get("atr_15m"),
            "stop_distance_atr": signal.get("stop_distance_atr"),
            "mae_before_tp1_pct": record.get("mae_before_tp1_pct"),
            "mfe_before_tp1_pct": record.get("mfe_before_tp1_pct"),
        }
        if record.get("tp1_candle_excursion_ambiguous"):
            report.setdefault("tp1_excursion_ambiguous", 0)
            report["tp1_excursion_ambiguous"] += 1
        groups.setdefault(outcome, []).append(metrics)
        entry = float(signal.get("entry") or 0.0)
        stop = float(signal.get("stop_loss") or 0.0)
        risk_pct = abs(entry - stop) / entry * 100.0 if entry > 0 else None
        report["trade_details"].append({
            "trade_id": record.get("trade_id"),
            "symbol": signal.get("symbol"),
            "side": signal.get("side", "UNKNOWN"),
            "status": status,
            "entry": entry or None,
            "stop_loss": stop or None,
            "risk_pct": round(risk_pct, 4) if risk_pct is not None else None,
            "atr_15m": signal.get("atr_15m"),
            "stop_distance_atr": signal.get("stop_distance_atr"),
            "setup_level": signal.get("setup_level"),
            "pullback_extreme": signal.get("pullback_extreme"),
            "ema20": signal.get("ema20"),
            "ema50": signal.get("ema50"),
            "entry_quality": signal.get("entry_quality"),
            "movement_strength": signal.get("movement_strength"),
            "mae_before_tp1_pct": record.get("mae_before_tp1_pct"),
            "mfe_before_tp1_pct": record.get("mfe_before_tp1_pct"),
            "post_stop_mfe_pct": record.get("post_stop_mfe_pct"),
            "post_stop_tp1_reached": bool(record.get("post_stop_tp1_reached")),
            "post_stop_tp2_reached": bool(record.get("post_stop_tp2_reached")),
        })
        engine = str(signal.get("engine", signal.get("strategy", "UNSPECIFIED")))
        side = str(signal.get("side", "UNKNOWN"))
        key = f"{engine}:{side}"
        bucket = report["by_engine_side"].setdefault(key, {"total": 0, "statuses": {}})
        bucket["total"] += 1
        bucket["statuses"][status] = bucket["statuses"].get(status, 0) + 1
    if report["entry_touched_decided"]:
        report["entry_touched_win_rate_pct"] = round(
            report["tp1_before_sl"] / report["entry_touched_decided"] * 100.0, 2
        )
    for name, rows in groups.items():
        summary = {"count": len(rows)}
        for field in (
            "movement_strength", "entry_quality", "mae_before_tp1_pct",
            "mfe_before_tp1_pct", "atr_15m", "stop_distance_atr",
        ):
            values = [float(row[field]) for row in rows if row.get(field) is not None]
            summary[field + "_avg"] = round(sum(values) / len(values), 2) if values else None
        report["groups"][name] = summary
    return report
