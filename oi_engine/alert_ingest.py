"""Parse first-stage Open Interest alerts from Telegram messages and channel posts."""
from __future__ import annotations
import re
from typing import Any, Dict, Optional

SYMBOL_PATTERNS = (
    re.compile(r"(?<![A-Z0-9])([A-Z0-9]{2,})\s*/\s*USDT(?![A-Z0-9])", re.I),
    re.compile(r"(?<![A-Z0-9])([A-Z0-9]{2,})\s*[-_]\s*USDT(?![A-Z0-9])", re.I),
    re.compile(r"(?<![A-Z0-9])([A-Z0-9]{2,})USDT(?![A-Z0-9])", re.I),
)

def parse_forwarded_oi_alert(update: Dict[str, Any]) -> Optional[str]:
    """Return a normalized USDT symbol from a forwarded message or a channel post.

    Direct channel posts are accepted because the bot must be added to the source
    channel. Ordinary private/group messages are accepted only when forwarded.
    """
    channel_post = update.get("channel_post")
    message = channel_post or update.get("message") or {}
    if not isinstance(message, dict):
        return None

    # A channel_post is delivered directly by Telegram to a bot that has been
    # added to the source channel; direct posts do not carry forward metadata.
    if not channel_post:
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

    # OI Detector posts commonly format the instrument as "#TICKER $price"
    # rather than spelling out TICKERUSDT. Accept that only for direct posts
    # from the configured public-channel ingestion path and only with OI context.
    if channel_post and re.search(r"\bOI\s*\[", body, re.I):
        hashtag_price = re.search(
            r"(?<![A-Z0-9])#([A-Z0-9]{2,12})\s+\$\s*\d",
            body.upper(),
        )
        if hashtag_price:
            return f"{hashtag_price.group(1).upper()}USDT"
    return None
