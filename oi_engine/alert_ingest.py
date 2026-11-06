"""Parse first-stage Open Interest alerts from Telegram messages and channel posts."""
from __future__ import annotations
import re
from typing import Any, Dict, Optional

SYMBOL_PATTERNS = (
    re.compile(r"(?<![A-Z0-9])([A-Z0-9]{2,})\s*/\s*USDT(?![A-Z0-9])", re.I),
    re.compile(r"(?<![A-Z0-9])([A-Z0-9]{2,})\s*[-_]\s*USDT(?![A-Z0-9])", re.I),
    re.compile(r"(?<![A-Z0-9])([A-Z0-9]{2,})USDT(?![A-Z0-9])", re.I),
)

# The upstream OI feed also emits tokenized equities and ETF/index contracts.
# These are explicitly outside this crypto-only scanner's scope.
NON_CRYPTO_BASES = {
    "AAPL", "AMD", "COST", "BYD", "XLE", "UVXY", "HIMS", "HK1810",
    "RIVN", "CSOPSKHYNIX2L", "SMCI", "GME", "TER", "ALAB",
    # Tokenized equity contracts observed in the live OI source feed.
    "PLTR", "MSFT", "GEV", "ASTS", "NVO", "GDX", "LITE", "FWDI",
    # Additional non-crypto tickers observed in the live @oi_detector scan.
    "CSOPSAMSUNG2L", "IBM", "LLY", "ABBV", "KLAC", "AAOI", "CRWV", "SOXS",
    # Equity/ETF symbols seen in the latest live bot alerts; keep scanner crypto-only.
    "MRNA", "EWY", "VST",
}


def _is_crypto_candidate(symbol: str) -> bool:
    return symbol.endswith("USDT") and symbol[:-4] not in NON_CRYPTO_BASES



def parse_oi_alert_direction(update: Dict[str, Any]) -> Optional[str]:
    """Read the colored direction marker immediately before the ticker hashtag."""
    channel_post = update.get("channel_post")
    message = channel_post or update.get("message") or {}
    if not isinstance(message, dict):
        return None
    body = message.get("text") or message.get("caption") or ""
    if not isinstance(body, str):
        return None
    # Real @oi_detector posts include a title before the marker, e.g.
    # "🚨 Crypto Open Interest ... 🔥 🔥 🟢 #SAGA ...".
    marker = re.search(r"(?:^|\s)(?:🔥\s*)*(🟢|🔴)\s*#[A-Z0-9]{2,12}(?=\s|$)", body, re.I)
    if not marker:
        return None
    return "LONG" if marker.group(1) == "🟢" else "SHORT"


def parse_forwarded_oi_alert(update: Dict[str, Any]) -> Optional[str]:
    """Return a normalized crypto USDT symbol from a forwarded message or channel post.

    Direct channel posts are accepted because the bot must be added to the source
    channel. Ordinary private/group messages are accepted only when forwarded.
    Known equity/ETF symbols are rejected even when the source formats them as
    USDT perpetual instruments.
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
            symbol = f"{match.group(1).upper()}USDT"
            return symbol if _is_crypto_candidate(symbol) else None

    # OI Detector posts commonly format the instrument as "#TICKER $price"
    # rather than spelling out TICKERUSDT. Accept that only for direct posts
    # from the configured public-channel ingestion path and only with OI context.
    if channel_post and re.search(r"\bOI\s*\[", body, re.I):
        hashtag_price = re.search(
            r"(?<![A-Z0-9])#([A-Z0-9]{2,12})\s+\$\s*\d",
            body.upper(),
        )
        if hashtag_price:
            symbol = f"{hashtag_price.group(1).upper()}USDT"
            return symbol if _is_crypto_candidate(symbol) else None
    return None
