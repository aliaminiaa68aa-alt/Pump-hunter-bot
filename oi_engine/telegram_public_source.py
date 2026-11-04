"""Read recent posts from a public Telegram channel preview (t.me/s/<channel>)."""
from __future__ import annotations
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Callable
import requests
from oi_engine.alert_ingest import parse_forwarded_oi_alert, parse_oi_alert_direction

PUBLIC_CHANNEL = "oi_detector"
PUBLIC_CHANNEL_URL = "https://t.me/s/{channel}"
MAX_BACKFILL_PAGES = 5
MAX_ALERT_AGE_SECONDS = 6 * 60 * 60
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

def fetch_public_channel_posts(channel=PUBLIC_CHANNEL, timeout=20, before=None):
    slug = channel.lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{5,}", slug):
        raise ValueError("Invalid public Telegram channel username")
    response = requests.get(
        PUBLIC_CHANNEL_URL.format(channel=slug),
        params={"before": int(before)} if before is not None else None,
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
    backfill_pages = 0
    oldest_visible_id = posts[0]["message_id"] if posts else 0
    oldest_visible_date = posts[0]["date"] if posts else 0

    # The public preview normally exposes only its latest 20 posts. If our saved
    # cursor is older than that window, page backwards before advancing the cursor.
    while (
        last_message_id
        and posts
        and oldest_visible_id > last_message_id
        and oldest_visible_date >= timestamp - MAX_ALERT_AGE_SECONDS
        and backfill_pages < MAX_BACKFILL_PAGES
    ):
        older_posts = fetch_public_channel_posts(channel, before=oldest_visible_id)
        older_posts = [
            post for post in older_posts
            if post["message_id"] < oldest_visible_id
        ]
        if not older_posts:
            break
        posts.extend(older_posts)
        backfill_pages += 1
        oldest_post = min(older_posts, key=lambda post: post["message_id"])
        oldest_visible_id = oldest_post["message_id"]
        oldest_visible_date = oldest_post["date"]

    # De-duplicate defensively in case the upstream preview overlaps page edges.
    posts = sorted(
        {post["message_id"]: post for post in posts}.values(),
        key=lambda post: post["message_id"],
    )
    unseen = [post for post in posts if post["message_id"] > last_message_id]
    next_id = last_message_id
    added = 0
    if unseen:
        print(
            f"Public Telegram poll: page_posts={len(posts)} unseen_posts={len(unseen)} "
            f"backfill_pages={backfill_pages} cursor_before={last_message_id} "
            f"page_range={posts[0]['message_id']}-{posts[-1]['message_id']}"
        )
        if (last_message_id and oldest_visible_id > last_message_id
                and oldest_visible_date >= timestamp - MAX_ALERT_AGE_SECONDS):
            print(
                f"WARNING: public Telegram backfill limit reached: cursor={last_message_id}, "
                f"oldest_visible={oldest_visible_id}, pages={backfill_pages}; "
                "older unseen posts may remain outside this scan"
            )
    else:
        newest = posts[-1]["message_id"] if posts else 0
        print(
            f"Public Telegram poll: page_posts={len(posts)} unseen_posts=0 "
            f"backfill_pages={backfill_pages} cursor_before={last_message_id} newest_visible={newest}"
        )
    for post in unseen:
        print(
            f"Public Telegram unseen post: message_id={post['message_id']} "
            f"text={post['text'][:220]!r}"
        )
        if timestamp - post["date"] > MAX_ALERT_AGE_SECONDS or post["date"] > timestamp + 60:
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
        source_side = parse_oi_alert_direction(update)
        # Every valid crypto OI alert must enter the watchlist. A colored
        # LONG/SHORT marker is useful context, but its absence must not discard
        # the alert: the independent market engine evaluates BOTH directions.
        if symbol and source_side not in {"LONG", "SHORT"}:
            print(
                f"Public Telegram OI alert has no explicit side: message_id={post['message_id']} "
                f"symbol={symbol}; both LONG and SHORT will be evaluated"
            )
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
            "source_side": source_side,
            "status": "در حال رصد",
            "reason": "هشدار عمومی Open Interest دریافت شد؛ منتظر تأیید Coinalyze هستیم",
        }
        added += 1
        next_id = post["message_id"]
    return next_id, added
