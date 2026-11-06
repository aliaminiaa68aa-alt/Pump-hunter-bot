"""Continuous live outcome monitor with an independent periodic signal scanner."""
from __future__ import annotations

import os
import threading
import time
from typing import Dict

import requests

from oi_engine.live_price_monitor import apply_live_prices
from oi_engine.telegram_runner import (
    OUTCOME_JOURNAL_LOCK,
    load_outcome_journal,
    save_outcome_journal,
    dispatch_outcome_notifications,
    main as run_signal_scan,
)

BINANCE_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/price"


def fetch_binance_last_prices(symbols: set[str]) -> Dict[str, float]:
    """Fetch one Binance USD-M last-price snapshot for all active symbols."""
    if not symbols:
        return {}
    response = requests.get(BINANCE_TICKER_URL, timeout=8)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("Unexpected Binance ticker response")
    wanted = {symbol.upper() for symbol in symbols}
    result: Dict[str, float] = {}
    for item in payload:
        symbol = str(item.get("symbol", "")).upper()
        if symbol in wanted:
            try:
                price = float(item.get("price"))
            except (TypeError, ValueError, OverflowError):
                continue
            if price > 0 and price < float("inf"):
                result[symbol] = price
    # A partial ticker response must not block every other active trade.
    # Missing symbols remain untouched by apply_live_prices; only a completely
    # empty usable snapshot fails closed so stale data can never create fills.
    if not result:
        raise RuntimeError(f"Binance ticker returned no usable active symbols: {sorted(wanted)}")
    return result


def monitor_live_outcomes_once() -> dict:
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("BOT_TOKEN and CHAT_ID are required")

    # Hold the shared journal lock for the complete read/modify/dispatch cycle.
    # The scanner only registers new trades under this same lock and never runs
    # the historical outcome replay in external-monitor mode.
    with OUTCOME_JOURNAL_LOCK:
        journal = load_outcome_journal()
        active = {
            str(record.get("signal", {}).get("symbol", "")).upper()
            for record in journal.get("trades", {}).values()
            if record.get("status") in {"WAITING_ENTRY", "OPEN", "TP1_HIT"}
        }
        active.discard("")
        before = sum(len(record.get("events", [])) for record in journal.get("trades", {}).values())
        prices = fetch_binance_last_prices(active) if active else {}
        missing_symbols = sorted(active - prices.keys())
        if prices:
            apply_live_prices(journal, prices)
        save_outcome_journal(journal)
        journal = dispatch_outcome_notifications(journal, token, chat_id)
        save_outcome_journal(journal)
        after = sum(len(record.get("events", [])) for record in journal.get("trades", {}).values())
        return {
            "active_symbols": len(active),
            "events": max(0, after - before),
            "prices_received": len(prices),
            "missing_symbols": missing_symbols,
        }


def _scanner_loop(scan_interval: int) -> None:
    # The scanner's journal replay/dispatch is disabled; the live monitor owns
    # outcome transitions and notifications while this thread scans the market.
    os.environ["PUMP_HUNTER_EXTERNAL_LIVE_MONITOR"] = "1"
    while True:
        started = time.monotonic()
        try:
            print("Starting background signal scan", flush=True)
            run_signal_scan()
            print("Background signal scan completed", flush=True)
        except Exception as exc:
            print(f"ERROR signal scan: {type(exc).__name__}: {exc}", flush=True)
        elapsed = time.monotonic() - started
        time.sleep(max(1, scan_interval - elapsed))


def main() -> None:
    scan_interval = max(60, int(os.getenv("SIGNAL_SCAN_INTERVAL_SECONDS", "300")))
    monitor_interval = max(2, int(os.getenv("LIVE_MONITOR_INTERVAL_SECONDS", "10")))
    os.environ["PUMP_HUNTER_EXTERNAL_LIVE_MONITOR"] = "1"
    scanner = threading.Thread(
        target=_scanner_loop,
        args=(scan_interval,),
        name="signal-scanner",
        daemon=True,
    )
    scanner.start()
    print(
        "Pump Hunter service started: "
        f"scan_interval={scan_interval}s live_price_poll={monitor_interval}s",
        flush=True,
    )
    # This foreground loop is independent of the scanner thread: a slow scan
    # cannot pause the 10-second outcome polling cadence.
    while True:
        started = time.monotonic()
        try:
            result = monitor_live_outcomes_once()
            if result.get("events") or result.get("missing_symbols"):
                print(f"Live outcome monitor update: {result}", flush=True)
        except Exception as exc:
            print(f"ERROR live outcome monitor: {type(exc).__name__}: {exc}", flush=True)
        elapsed = time.monotonic() - started
        time.sleep(max(0.1, monitor_interval - elapsed))


if __name__ == "__main__":
    main()
