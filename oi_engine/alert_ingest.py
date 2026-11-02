"""Parse forwarded first-stage Open Interest alerts from Telegram updates."""
from __future__ import annotations
import re
from typing import Any, Dict, Optional

SYMBOL_PATTERNS = (
    re.compile(r"(?<![A-Z0-9])([A-Z0-9]{2,})\s*/\s*USDT(?![A-Z0-9])", re.I),
    re.compile(r"(?<![A-Z0-9])([A-Z0-9]{2,})\s*[-_]\s*USDT(?![A-Z0-9])", re.I),
    re.compile(r"(?<![A-Z0-9])([A-Z0-9]{2,})USDT(?![A-Z0-9])", re.I),
)

def parse_forwarded_oi_alert(update: Dict[str, Any]) -> Optional[str]:
    """Return a normalized symbol only for an actual forwarded Telegram message."""
    message = update.get("message") or update.get("channel_post") or {}
    if not isinstance(message, dict):
        return None
    forwarded = any(message.get(key) for key in (
        "forward_origin", "forward_from", "forward_from_chat",
        "forward_sender_name", "forward_date",
    ))
    if not forwarded:
        return None
    body = message.get("text") or message.get("caption") or ""
    if not isinstance(body, str):
        return None
    for pattern in SYMBOL_PATTERNS:
        match = pattern.search(body.upper())
        if match:
            return f"{match.group(1).upper()}USDT"
    return None
