"""Scheduled Telegram runner for the standalone OI signal engine."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict

import requests

from oi_engine.signal_engine import scan, format_signal

STATE_PATH = Path(".oi_state/sent_signals.json")


def load_state() -> Dict[str, int]:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: Dict[str, int]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Replace atomically so an interrupted runner cannot leave truncated JSON.
    temporary_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(STATE_PATH)


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


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    if not token or not chat_id:
        raise SystemExit("BOT_TOKEN and CHAT_ID GitHub secrets are required")

    state = load_state()
    signals = scan(max_symbols=int(os.getenv("OI_MAX_SYMBOLS", "100")))
    sent = 0

    for signal in signals:
        # One notification per symbol and closed 15m candle.
        key = f"{signal.symbol}:{signal.side}"
        candle_id = int(signal.candle_close_time)
        if state.get(key) == candle_id:
            continue
        send_telegram(token, chat_id, format_signal(signal))
        state[key] = candle_id
        sent += 1
        # Persist immediately after each confirmed Telegram delivery. If a later
        # message fails, already-delivered signals won't be sent again next run.
        save_state(state)

    # Retain only the latest 500 symbol/direction records.
    if len(state) > 500:
        state = dict(sorted(state.items(), key=lambda item: item[1])[-500:])
        save_state(state)
    print(f"OI scan complete: candidates={len(signals)}, telegram_sent={sent}")


if __name__ == "__main__":
    main()
