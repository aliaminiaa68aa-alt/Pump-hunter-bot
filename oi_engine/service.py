"""Continuous live outcome monitor with independent multi-provider price fallback."""
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

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"


def fetch_binance_last_prices(symbols: set[str]) -> Dict[str, float]:
    """Fetch live Binance Spot prices only; no derivatives-venue fallback."""
    if not symbols:
        return {}
    wanted = {symbol.upper() for symbol in symbols}
    result: Dict[str, float] = {}
    for symbol in sorted(wanted):
        try:
            response = requests.get(BINANCE_TICKER_URL, params={"symbol": symbol}, timeout=8,
                                    headers={"User-Agent": "Pump-Hunter/1.0"})
            response.raise_for_status()
            item = response.json()
            if not isinstance(item, dict) or item.get("symbol") != symbol:
                raise ValueError("Unexpected Binance Spot ticker response")
            price = float(item.get("price", 0))
            if not (price > 0 and price < float("inf")):
                raise ValueError("Invalid Binance Spot price")
            result[symbol] = price
        except Exception as exc:
            print("Binance Spot live price unavailable for " + symbol + ": " +
                  type(exc).__name__ + ": " + str(exc), flush=True)
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
            if (
                record.get("status") in {"WAITING_ENTRY", "OPEN", "TP1_HIT"}
                and record.get("telegram_delivery_status", "sent") != "pending"
            )
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
    last_heartbeat = 0.0
    while True:
        started = time.monotonic()
        try:
            result = monitor_live_outcomes_once()
            now = time.monotonic()
            if result.get("events") or result.get("missing_symbols"):
                print(f"Live outcome monitor update: {result}", flush=True)
            elif now - last_heartbeat >= 60:
                print(f"Live outcome monitor heartbeat: {result}", flush=True)
                last_heartbeat = now
        except Exception as exc:
            print(f"ERROR live outcome monitor: {type(exc).__name__}: {exc}", flush=True)
        elapsed = time.monotonic() - started
        time.sleep(max(0.1, monitor_interval - elapsed))


if __name__ == "__main__":
    main()
