"""Single-process VPS service: periodic signal scan + continuous live outcome checks."""
from __future__ import annotations

import os
import time
from typing import Dict

import requests

from oi_engine.live_price_monitor import apply_live_prices
from oi_engine.telegram_runner import (
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
    missing = wanted - result.keys()
    if missing:
        raise RuntimeError(f"Binance ticker missing active symbols: {sorted(missing)}")
    return result


def monitor_live_outcomes_once() -> dict:
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("BOT_TOKEN and CHAT_ID are required")

    journal = load_outcome_journal()
    active = {
        str(record.get("signal", {}).get("symbol", "")).upper()
        for record in journal.get("trades", {}).values()
        if record.get("status") in {"WAITING_ENTRY", "OPEN", "TP1_HIT"}
    }
    active.discard("")
    if not active:
        return {"active_symbols": 0, "events": 0}

    prices = fetch_binance_last_prices(active)
    before = sum(len(record.get("events", [])) for record in journal["trades"].values())
    apply_live_prices(journal, prices)
    save_outcome_journal(journal)
    journal = dispatch_outcome_notifications(journal, token, chat_id)
    save_outcome_journal(journal)
    after = sum(len(record.get("events", [])) for record in journal["trades"].values())
    return {
        "active_symbols": len(active),
        "events": max(0, after - before),
        "prices_received": len(prices),
    }


def main() -> None:
    scan_interval = max(60, int(os.getenv("SIGNAL_SCAN_INTERVAL_SECONDS", "300")))
    monitor_interval = max(2, int(os.getenv("LIVE_MONITOR_INTERVAL_SECONDS", "10")))
    next_scan = 0.0
    print(
        "Pump Hunter service started: "
        f"scan_interval={scan_interval}s live_price_poll={monitor_interval}s",
        flush=True,
    )
    while True:
        now = time.monotonic()
        if now >= next_scan:
            try:
                print("Starting scheduled signal scan", flush=True)
                run_signal_scan()
                print("Scheduled signal scan completed", flush=True)
            except Exception as exc:
                print(f"ERROR signal scan: {type(exc).__name__}: {exc}", flush=True)
            next_scan = time.monotonic() + scan_interval

        try:
            result = monitor_live_outcomes_once()
            if result.get("events"):
                print(f"Live outcome events: {result}", flush=True)
        except Exception as exc:
            print(f"ERROR live outcome monitor: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(monitor_interval)


if __name__ == "__main__":
    main()
