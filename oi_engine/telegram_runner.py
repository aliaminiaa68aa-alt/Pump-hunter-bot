"""Scheduled alert-driven OI + Coinalyze Telegram runner."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

import requests

from oi_engine.telegram_public_source import poll_public_channel_alerts
from oi_engine.coinalyze_client import CoinalyzeClient
from oi_engine.watchlist_engine import scan_watchlist, format_confirmed_signal

STATE_PATH = Path(".oi_state/sent_signals.json")
WATCHLIST_PATH = Path(".oi_state/watchlist.json")
MAX_WATCHLIST_SYMBOLS = 4  # 4 symbols x 8 metric calls + one market map stays below 40/min
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
        if timestamp - int(record.get("last_alert", record.get("first_seen", timestamp)))
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
    # Start from the public-channel source only. Do not ingest Telegram forwards,
    # direct channel updates delivered to the bot, or any legacy alert inbox.
    watchlist = watch_state.get("symbols", {})
    if not isinstance(watchlist, dict):
        watchlist = {}

    client = CoinalyzeClient(api_key=api_key)
    added = 0
    public_channel = os.getenv("OI_PUBLIC_CHANNEL", "oi_detector").strip()
    public_last_id = int(watch_state.get("public_channel_last_id", 0))
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
    watchlist = prune_watchlist(watchlist)
    confirmed, watchlist = scan_watchlist(watchlist, client=client)

    sent = 0
    for signal, metrics in confirmed:
        key = f"{signal.symbol}:{signal.side}"
        candle_id = int(signal.candle_close_time)
        if state.get(key) == candle_id:
            continue
        send_telegram(token, chat_id, format_confirmed_signal(signal, metrics))
        state[key] = candle_id
        sent += 1
        save_state(state)

    if len(state) > 500:
        state = dict(sorted(state.items(), key=lambda item: item[1])[-500:])
        save_state(state)

    save_watchlist_state({
        "public_channel_last_id": public_last_id,
        "symbols": watchlist,
        "updated_at": int(time.time()),
    })
    print(
        f"Alert-driven OI scan complete: watchlist={len(watchlist)}, "
        f"new_public_channel_alerts={added}, confirmed={len(confirmed)}, telegram_sent={sent}"
    )
    for symbol, record in watchlist.items():
        print(f"Watchlist {symbol}: {record.get('status')} - {record.get('reason', '')} [source={record.get('data_source', 'unknown')}]")


if __name__ == "__main__":
    main()
