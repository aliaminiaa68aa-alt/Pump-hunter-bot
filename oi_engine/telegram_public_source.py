"""Read recent posts from a public Telegram channel preview (t.me/s/<channel>)."""
from __future__ import annotations
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Callable
import requests
from oi_engine.alert_ingest import parse_forwarded_oi_alert

PUBLIC_CHANNEL = "oi_detector"
PUBLIC_CHANNEL_URL = "https://t.me/s/{channel}"
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

class _ChannelParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.current = None
        self.posts = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if self.current is None:
            post_key = values.get("data-post")
            classes = (values.get("class") or "").split()
            if post_key and "tgme_widget_message" in classes:
                self.current = {"post_key": post_key, "text_parts": [], "date": None}
                self.depth = 1
                return
        else:
            if tag not in VOID_TAGS:
                self.depth += 1
            if tag == "time" and values.get("datetime"):
                self.current["date"] = values["datetime"]

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS and self.current is not None:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.current is None:
            return
        self.depth -= 1
        if self.depth <= 0:
            self.current["text"] = " ".join(" ".join(self.current["text_parts"]).split())
            self.posts.append(self.current)
            self.current = None
            self.depth = 0

    def handle_data(self, data):
        if self.current is not None and data.strip():
            self.current["text_parts"].append(data)

def _unix_time(value, fallback):
    if not value:
        return fallback
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, OverflowError):
        return fallback

def fetch_public_channel_posts(channel=PUBLIC_CHANNEL, timeout=20):
    slug = channel.lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{5,}", slug):
        raise ValueError("Invalid public Telegram channel username")
    response = requests.get(
        PUBLIC_CHANNEL_URL.format(channel=slug),
        headers={"User-Agent": "Mozilla/5.0 (compatible; OIAlertMonitor/1.0)"},
        timeout=timeout,
    )
    response.raise_for_status()
    parser = _ChannelParser()
    parser.feed(response.text)
    posts = []
    now = int(time.time())
    for item in parser.posts:
        match = re.fullmatch(re.escape(slug) + r"/(\d+)", item["post_key"])
        if not match:
            continue
        text = item.get("text", "")
        if not text:
            continue
        posts.append({
            "message_id": int(match.group(1)),
            "date": _unix_time(item.get("date"), now),
            "text": text,
            "channel": slug,
        })
    return sorted(posts, key=lambda post: post["message_id"])

def poll_public_channel_alerts(channel, last_message_id, watchlist, now=None, symbol_validator: Callable[[str], bool] | None = None):
    """Ingest every unseen recent alert; scan capacity is managed separately by the runner."""
    timestamp = int(now if now is not None else time.time())
    posts = fetch_public_channel_posts(channel)
    unseen = [post for post in posts if post["message_id"] > last_message_id]
    next_id = last_message_id
    added = 0
    if unseen:
        print(
            f"Public Telegram poll: page_posts={len(posts)} unseen_posts={len(unseen)} "
            f"cursor_before={last_message_id} page_range={posts[0]['message_id']}-{posts[-1]['message_id']}"
        )
    else:
        newest = posts[-1]["message_id"] if posts else 0
        print(
            f"Public Telegram poll: page_posts={len(posts)} unseen_posts=0 "
            f"cursor_before={last_message_id} newest_visible={newest}"
        )
    for post in unseen:
        print(
            f"Public Telegram unseen post: message_id={post['message_id']} "
            f"text={post['text'][:220]!r}"
        )
        if timestamp - post["date"] > 6 * 60 * 60 or post["date"] > timestamp + 60:
            print(
                f"Public Telegram post skipped by age/time: message_id={post['message_id']} "
                f"post_date={post['date']} now={timestamp}"
            )
            next_id = post["message_id"]
            continue
        update = {"channel_post": {
            "text": post["text"], "date": post["date"], "message_id": post["message_id"],
            "chat": {"username": post["channel"]},
        }}
        symbol = parse_forwarded_oi_alert(update)
        if not symbol:
            preview = post["text"][:240].replace("\\n", " ")
            print(
                f"Public Telegram OI post not parsed: message_id={post['message_id']} "
                f"text={preview!r}"
            )
            next_id = post["message_id"]
            continue
        if symbol_validator is not None:
            try:
                if not symbol_validator(symbol):
                    print(
                        f"Public Telegram alert ignored: message_id={post['message_id']} "
                        f"symbol={symbol} reason=no supported USDT perpetual mapping"
                    )
                    next_id = post["message_id"]
                    continue
            except Exception as exc:
                # Do not permanently discard an alert if market metadata is temporarily unavailable.
                print(f"WARNING: market validation failed for {symbol}: {type(exc).__name__}: {exc}")
                break
        existing = watchlist.get(symbol, {})
        print(
            f"Public Telegram OI alert parsed: message_id={post['message_id']} "
            f"symbol={symbol} already_watched={bool(existing)}"
        )
        watchlist[symbol] = {
            **existing,
            "first_seen": existing.get("first_seen", post["date"]),
            "last_alert": post["date"],
            "source": "telegram_public_preview",
            "source_channel": post["channel"],
            "source_message_id": post["message_id"],
            "source_text": post["text"][:3000],
            "status": "در حال رصد",
            "reason": "هشدار عمومی Open Interest دریافت شد؛ منتظر تأیید Coinalyze هستیم",
        }
        added += 1
        next_id = post["message_id"]
    return next_id, added
