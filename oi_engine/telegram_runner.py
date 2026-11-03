"""Scheduled alert-driven OI + Coinalyze Telegram runner."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

import requests

from oi_engine.telegram_public_source import poll_public_channel_alerts
from oi_engine.alert_ingest import _is_crypto_candidate, parse_oi_alert_direction
from oi_engine.coinalyze_client import CoinalyzeClient
from oi_engine.watchlist_engine import scan_watchlist, format_confirmed_signal, source_oi_priority
from oi_engine.outcome_tracker import make_trade_record, monitor_journal

STATE_PATH = Path(".oi_state/sent_signals.json")
WATCHLIST_PATH = Path(".oi_state/watchlist.json")
OUTCOME_PATH = Path(".oi_state/trade_outcomes.json")
MAX_WATCHLIST_SYMBOLS = 8  # Coinalyze throttles at 40/min; larger batches reduce watchlist revisit time
WATCHLIST_TTL_SECONDS = 24 * 60 * 60


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(path)


def load_state() -> Dict[str, int]:
    raw = _load_json(STATE_PATH, {})
    return raw if isinstance(raw, dict) else {}


def save_state(state: Dict[str, int]) -> None:
    _save_json(STATE_PATH, state)


def load_watchlist_state() -> Dict[str, Any]:
    raw = _load_json(WATCHLIST_PATH, {})
    return raw if isinstance(raw, dict) else {}


def save_watchlist_state(state: Dict[str, Any]) -> None:
    _save_json(WATCHLIST_PATH, state)


def load_outcome_journal() -> Dict[str, Any]:
    raw = _load_json(OUTCOME_PATH, {"trades": {}})
    return raw if isinstance(raw, dict) else {"trades": {}}


def save_outcome_journal(journal: Dict[str, Any]) -> None:
    _save_json(OUTCOME_PATH, journal)


def send_telegram(token: str, chat_id: str, message: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": message},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram rejected message: {payload}")


def prune_watchlist(watchlist: Dict[str, Any], now: int | None = None) -> Dict[str, Any]:
    timestamp = int(now if now is not None else time.time())
    return {
        symbol: record for symbol, record in watchlist.items()
        if _is_crypto_candidate(symbol)
        and timestamp - int(record.get("last_alert", record.get("first_seen", timestamp)))
        <= WATCHLIST_TTL_SECONDS
    }


def _has_supported_perpetual(client: CoinalyzeClient, symbol: str) -> bool:
    """Return False only for a genuine missing Binance perpetual mapping."""
    from oi_engine.coinalyze_client import CoinalyzeError
    try:
        return bool(client.resolve_market(symbol))
    except CoinalyzeError as exc:
        if str(exc).startswith("No Binance USDT perpetual mapping"):
            return False
        raise


def select_watchlist_batch(watchlist, last_scanned_symbol="", limit=MAX_WATCHLIST_SYMBOLS):
    """Keep the strongest OI candidates hot while rotating the rest fairly."""
    symbols = list(watchlist)
    if not symbols:
        return {}, ""
    count = min(max(1, int(limit)), len(symbols))
    ranked = sorted(
        symbols,
        key=lambda symbol: float(watchlist[symbol].get("oi_priority_score", 0.0)),
        reverse=True,
    )
    # Recheck the top two OI-expansion candidates on every cycle. Rotate all
    # remaining slots so a strong alert cannot permanently starve older alerts.
    hot_count = min(2, count, len(ranked))
    hot = ranked[:hot_count]
    rotating = ranked[hot_count:]
    rotating_slots = count - hot_count
    if rotating_slots <= 0 or not rotating:
        selected = hot
    else:
        try:
            start = (rotating.index(last_scanned_symbol) + 1) % len(rotating)
        except ValueError:
            start = 0
        rotating_selected = [
            rotating[(start + offset) % len(rotating)]
            for offset in range(min(rotating_slots, len(rotating)))
        ]
        selected = hot + rotating_selected
    return {symbol: watchlist[symbol] for symbol in selected}, selected[-1]


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    api_key = os.getenv("COINALYZE_API_KEY")
    if not token or not chat_id:
        raise SystemExit("BOT_TOKEN and CHAT_ID GitHub secrets are required")
    if not api_key:
        raise SystemExit("COINALYZE_API_KEY GitHub secret is required")

    state = load_state()
    watch_state = load_watchlist_state()
    outcome_journal = load_outcome_journal()
    # Start from the public-channel source only. Do not ingest Telegram forwards,
    # direct channel updates delivered to the bot, or any legacy alert inbox.
    watchlist = watch_state.get("symbols", {})
    if not isinstance(watchlist, dict):
        watchlist = {}
    # Migrate older cached alerts: recover direction from their original source text.
    # Never infer a side when the source marker is absent or ambiguous.
    for record in watchlist.values():
        if isinstance(record, dict) and record.get("source_side") not in {"LONG", "SHORT"}:
            source_text = record.get("source_text", "")
            recovered_side = parse_oi_alert_direction({"channel_post": {"text": source_text}})
            if recovered_side:
                record["source_side"] = recovered_side

    client = CoinalyzeClient(api_key=api_key)
    # Binance Futures returns HTTP 451 on this runner; use the same Coinalyze
    # 15m OHLCV fallback as the scanner so outcome tracking can still progress.
    outcome_journal = monitor_journal(outcome_journal, fetcher=client.fetch_ohlcv_candles)
    save_outcome_journal(outcome_journal)
    outcome_statuses = {}
    for trade in outcome_journal.get("trades", {}).values():
        status = trade.get("status", "UNKNOWN")
        outcome_statuses[status] = outcome_statuses.get(status, 0) + 1
    provider_errors = sum(bool(trade.get("last_error")) for trade in outcome_journal.get("trades", {}).values())
    print(f"Outcome monitor: statuses={outcome_statuses}, provider_errors={provider_errors}")
    added = 0
    public_channel = os.getenv("OI_PUBLIC_CHANNEL", "oi_detector").strip()
    public_last_id = int(watch_state.get("public_channel_last_id", 0))
    # Free expired slots before ingesting new public alerts; otherwise stale symbols
    # can occupy all four slots and block fresh candidates for another polling cycle.
    watchlist = prune_watchlist(watchlist)
    try:
        public_last_id, public_added = poll_public_channel_alerts(
            public_channel, public_last_id, watchlist,
            symbol_validator=lambda symbol: _has_supported_perpetual(client, symbol)
        )
        added += public_added
        print(f"Public Telegram source @{public_channel}: new_alerts={public_added}")
    except requests.RequestException as exc:
        # Keep the previous cursor and continue scanning existing watchlist symbols.
        print(f"WARNING: public Telegram source fetch failed: {exc}")
    # Persist ingestion before any market-data calls. If a scan times out or the
    # provider stalls, newly received alerts and the Telegram cursor survive.
    save_watchlist_state({
        "public_channel_last_id": public_last_id,
        "symbols": watchlist,
        "scan_cursor": str(watch_state.get("scan_cursor", "")),
        "updated_at": int(time.time()),
    })
    # Parse source OI expansion for every alert before selecting the scan batch.
    # These values only prioritize scanning; they do not authorize entries.
    for symbol, record in watchlist.items():
        if isinstance(record, dict):
            priority = source_oi_priority(record.get("source_text", ""))
            record.update(priority)

    # Keep the complete alert queue. Revisit the two strongest OI candidates
    # every cycle and rotate the remaining slots to preserve fair coverage.
    batch, next_scan_cursor = select_watchlist_batch(
        watchlist, str(watch_state.get("scan_cursor", ""))
    )
    confirmed, scanned_batch = scan_watchlist(batch, client=client)
    watchlist.update(scanned_batch)

    sent = 0
    for signal, metrics in confirmed:
        key = f"{signal.symbol}:{signal.side}"
        candle_id = int(signal.candle_close_time)
        if state.get(key) == candle_id:
            continue
        send_telegram(token, chat_id, format_confirmed_signal(signal, metrics))
        trade_record = make_trade_record(signal, metrics, int(time.time() * 1000))
        outcome_journal.setdefault("trades", {}).setdefault(trade_record["trade_id"], trade_record)
        save_outcome_journal(outcome_journal)
        state[key] = candle_id
        sent += 1
        save_state(state)

    if len(state) > 500:
        state = dict(sorted(state.items(), key=lambda item: item[1])[-500:])
        save_state(state)

    save_outcome_journal(outcome_journal)
    save_watchlist_state({
        "public_channel_last_id": public_last_id,
        "symbols": watchlist,
        "scan_cursor": next_scan_cursor or str(watch_state.get("scan_cursor", "")),
        "updated_at": int(time.time()),
    })
    print(
        f"Alert-driven OI scan complete: watchlist={len(watchlist)}, "
        f"new_public_channel_alerts={added}, confirmed={len(confirmed)}, telegram_sent={sent}, "
        f"tracked_trades={len(outcome_journal.get('trades', {}))}"
    )
    for symbol, record in scanned_batch.items():
        print(
            f"Watchlist {symbol}: {record.get('status')} - {record.get('reason', '')} "
            f"[market_source={record.get('data_source', 'unknown')}, "
            f"alert_message_id={record.get('source_message_id', 'unknown')}]"
        )


if __name__ == "__main__":
    main()
