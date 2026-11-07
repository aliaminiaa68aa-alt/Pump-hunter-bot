"""Scheduled alert-driven OI + Coinalyze Telegram runner."""
from __future__ import annotations

import json
import math
import os
import time
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import requests

from oi_engine.telegram_public_source import poll_public_channel_alerts
from oi_engine.alert_ingest import _is_crypto_candidate, parse_oi_alert_direction
from oi_engine.coinalyze_client import CoinalyzeClient
from oi_engine.watchlist_engine import scan_watchlist, format_confirmed_signal, source_oi_priority
from oi_engine.outcome_tracker import (
    make_trade_record, monitor_journal, summarize_outcomes,
    _fetch_klines, _fetch_1m_klines,
)

STATE_PATH = Path(".oi_state/sent_signals.json")
WATCHLIST_PATH = Path(".oi_state/watchlist.json")
OUTCOME_PATH = Path(".oi_state/trade_outcomes.json")
MAX_WATCHLIST_SYMBOLS = 8  # Reserve capacity for derivatives metrics, discovery, and outcome monitoring.
MAX_MARKET_QUEUE_SYMBOLS = 60
MARKET_DISCOVERY_PER_RUN = 12
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


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


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


OUTCOME_JOURNAL_LOCK = threading.RLock()


def load_outcome_journal() -> Dict[str, Any]:
    raw = _load_json(OUTCOME_PATH, {"trades": {}})
    return raw if isinstance(raw, dict) else {"trades": {}}


def save_outcome_journal(journal: Dict[str, Any]) -> None:
    with OUTCOME_JOURNAL_LOCK:
        _save_json(OUTCOME_PATH, journal)


def register_outcome_trade(trade_record: Dict[str, Any]) -> None:
    """Merge a newly sent signal into the latest journal without clobbering live updates."""
    with OUTCOME_JOURNAL_LOCK:
        latest = load_outcome_journal()
        latest.setdefault("trades", {}).setdefault(trade_record["trade_id"], trade_record)
        save_outcome_journal(latest)


def _event_price_result(signal: Dict[str, Any], event: Dict[str, Any]) -> str:
    """Report signed price movement from entry; this is not leveraged account PnL."""
    entry = float(signal.get("entry") or 0)
    if entry <= 0:
        return ""
    side = str(signal.get("side", "LONG")).upper()
    name = event.get("event")
    fallback_key = {"TP1": "take_profit_1", "TP2": "take_profit_2", "SL": "stop_loss"}.get(name)
    fill = event.get("fill")
    if fill is None and fallback_key:
        fill = signal.get(fallback_key)
    if fill is None:
        return ""
    fill = float(fill)
    signed_pct = ((fill - entry) if side == "LONG" else (entry - fill)) / entry * 100.0
    if name in {"TP1", "TP2"}:
        return f"\nسود قیمتی: +{signed_pct:.2f}%"
    if name == "SL":
        return f"\nضرر قیمتی: {signed_pct:.2f}%"
    return ""


class TelegramAmbiguousDeliveryError(RuntimeError):
    """Telegram may have accepted a message despite returning a server error."""


def send_telegram(
    token: str, chat_id: str, message: str,
    reply_to_message_id: int | None = None, parse_mode: str | None = None,
) -> int:
    """Send one Telegram message and return its confirmed message ID.

    Retry only failures known not to have created a message (connect timeout,
    or Telegram's explicit 429 rejection). A read timeout is ambiguous: retrying
    could duplicate a message that Telegram already accepted.
    """
    data = {
        "chat_id": chat_id, "text": message,
        **({"reply_to_message_id": int(reply_to_message_id)} if reply_to_message_id else {}),
        **({"parse_mode": parse_mode} if parse_mode else {}),
    }
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(3):
        try:
            response = requests.post(url, data=data, timeout=(10, 25))
        except requests.exceptions.ConnectTimeout:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
            continue

        try:
            payload = response.json()
        except ValueError as exc:
            if response.status_code >= 500:
                raise TelegramAmbiguousDeliveryError(
                    f"Telegram returned HTTP {response.status_code} without a readable response"
                ) from exc
            response.raise_for_status()
            raise RuntimeError("Telegram returned a non-JSON sendMessage response") from exc

        # A 5xx response is ambiguous: the request reached Telegram's server,
        # and we cannot prove that no message was created before the failure.
        if response.status_code >= 500:
            raise TelegramAmbiguousDeliveryError(
                f"Telegram returned HTTP {response.status_code}: {payload}"
            )

        if response.status_code == 429:
            retry_after = _safe_int(
                (payload.get("parameters") or {}).get("retry_after"), attempt + 1
            )
            if attempt == 2:
                raise RuntimeError(f"Telegram rate limit persisted after retries: {payload}")
            time.sleep(max(1, min(retry_after, 30)))
            continue

        if not payload.get("ok"):
            # Preserve Telegram's description (e.g. "reply message not found")
            # so outcome notifications can safely fall back to a standalone post.
            raise RuntimeError(f"Telegram rejected message: {payload}")
        response.raise_for_status()
        result = payload.get("result") or {}
        message_id = result.get("message_id")
        if message_id is None:
            raise RuntimeError(f"Telegram accepted response without message_id: {payload}")
        return int(message_id)

    raise RuntimeError("Telegram message delivery failed after safe retries")


def verify_telegram_connection(token: str, chat_id: str) -> None:
    """Verify bot authentication and target-chat access before scanning."""
    me_response = requests.get(
        f"https://api.telegram.org/bot{token}/getMe", timeout=15
    )
    me_response.raise_for_status()
    me_payload = me_response.json()
    if not me_payload.get("ok"):
        raise RuntimeError(f"Telegram bot authentication failed: {me_payload}")

    chat_response = requests.get(
        f"https://api.telegram.org/bot{token}/getChat",
        params={"chat_id": chat_id}, timeout=15,
    )
    chat_response.raise_for_status()
    chat_payload = chat_response.json()
    if not chat_payload.get("ok"):
        raise RuntimeError(f"Telegram target chat is unavailable: {chat_payload}")
    print("Telegram preflight: bot authentication OK; target chat accessible")


def _is_ambiguous_telegram_delivery(exc: BaseException) -> bool:
    """True when Telegram may have accepted a message but its response was lost."""
    return isinstance(exc, TelegramAmbiguousDeliveryError) or (
        not isinstance(exc, requests.exceptions.ConnectTimeout)
        and isinstance(
            exc,
            (
                requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
            ),
        )
    )


def send_outcome_update(
    token: str, chat_id: str, message: str,
    reply_to_message_id: int | None = None,
) -> int | None:
    """Send a non-entry outcome update without failing a whole scan on an ambiguous timeout."""
    try:
        return send_telegram(
            token, chat_id, message, reply_to_message_id=reply_to_message_id
        )
    except requests.exceptions.ReadTimeout:
        # Telegram may have accepted the message before the response timed out.
        # Do not retry blindly: that could create a duplicate reply. Confirmed
        # entry alerts still use send_telegram directly and remain fail-closed.
        print(
            "WARNING: Telegram outcome update timed out; delivery status is unknown; "
            "skipping retry to avoid a duplicate message"
        )
        return None



def _format_event_time_iran(event: Dict[str, Any]) -> str:
    timestamp = _safe_int(event.get("time_ms"), 0)
    if not timestamp:
        return ""
    local = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).astimezone(
        timezone(timedelta(hours=3, minutes=30))
    )
    return local.strftime("%H:%M:%S")


def dispatch_outcome_notifications(journal: Dict[str, Any], token: str, chat_id: str) -> Dict[str, Any]:
    """Send unsent outcome events and persist per-event delivery state for retries."""
    now_ms = int(time.time() * 1000)
    # Keep unannounced outcomes retryable for the full active-trade window.
    # A delayed/queued GitHub Actions run must not silently discard real fills.
    legacy_cutoff = now_ms - 96 * 60 * 60 * 1000
    for trade_id, record in journal.get("trades", {}).items():
        # Distinguish a genuinely missed setup from a delayed polling cycle.
        # If entry happened BEFORE the signal was sent, suppress the whole
        # trade. If entry happened AFTER the signal but this runner noticed it
        # late, suppress only the stale ENTRY ping; preserve TP/SL reporting.
        events = record.get("events", [])
        entry_event = next((event for event in events if event.get("event") == "ENTRY"), None)
        late_entry_notification = False
        if entry_event:
            entry_time = _safe_int(entry_event.get("time_ms"), 0)
            sent_at = _safe_int(record.get("sent_at_ms"), 0)
            entry_predates_signal = bool(entry_time and sent_at and entry_time <= sent_at)
            # Repair records classified by the previous overly broad rule:
            # a valid post-signal entry was marked MISSED_ENTRY solely because
            # the five-minute runner noticed it more than two minutes later.
            if (
                record.get("entry_missed")
                and entry_time and sent_at and entry_time > sent_at
            ):
                record["entry_missed"] = False
                record.pop("entry_missed_reason", None)
                event_names = [event.get("event") for event in events]
                if "TP2" in event_names:
                    record["status"] = "CLOSED_TP2"
                elif "SL" in event_names:
                    record["status"] = "CLOSED_SL"
                elif "TP1" in event_names:
                    record["status"] = "TP1_HIT"
                else:
                    record["status"] = "OPEN"
                delivery = record.setdefault("notification_state", {})
                for index, event in enumerate(events):
                    previous = delivery.get(str(index), {})
                    if previous.get("reason") == "missed_entry":
                        if event.get("event") == "ENTRY":
                            delivery[str(index)] = {
                                "status": "ignored",
                                "reason": "late_entry_notification",
                            }
                        else:
                            delivery[str(index)] = {"status": "pending", "attempts": 0}
                print(
                    f"Recovered valid delayed trade outcomes: trade={trade_id} "
                    f"entry_time_ms={entry_time} sent_at_ms={sent_at} "
                    f"events={event_names}"
                )
            # Always report a confirmed post-signal entry, even when polling
            # noticed it late. The message is explicitly marked historical below;
            # suppressing it made users see TP/SL without ever seeing ENTRY.
            if entry_predates_signal and not record.get("entry_missed"):
                record["entry_missed"] = True
                record["status"] = "MISSED_ENTRY"
                record["entry_missed_reason"] = "Entry touch occurred before the signal was sent"
                print(
                    f"Outcome trade invalidated: entry predates signal: trade={trade_id} "
                    f"entry_time_ms={entry_time} sent_at_ms={sent_at}"
                )
            if record.get("entry_missed"):
                delivery = record.setdefault("notification_state", {})
                for index in range(len(events)):
                    delivery[str(index)] = {"status": "ignored", "reason": "missed_entry"}
                continue
        anchor_id = record.get("telegram_message_id")
        # A missing original-message ID must not suppress outcome alerts.
        # Send them as standalone chat messages when reply threading is unavailable.
        if not anchor_id:
            print(
                f"WARNING: outcome trade has no Telegram anchor; "
                f"sending standalone notifications: trade={trade_id}"
            )
        events = record.get("events", [])
        delivery = record.setdefault("notification_state", {})
        for index, event in enumerate(events):
            key = str(index)
            if key not in delivery:
                timestamp = _safe_int(event.get("time_ms"), 0)
                # Events generated in the current monitoring window must be
                # delivered even when the trade has already closed in the same
                # replay cycle. The old status gate silently discarded ENTRY,
                # TP1, TP2 and SL for fast trades that completed before dispatch.
                supported_event = event.get("event") in {"ENTRY", "TP1", "TP2", "SL", "AMBIGUOUS_ENTRY_POLL", "AMBIGUOUS_ENTRY_CANDLE"}
                sent_at = _safe_int(record.get("sent_at_ms"), 0)
                if (
                    not timestamp or timestamp < legacy_cutoff
                    or (sent_at and timestamp < sent_at)
                    or not supported_event
                ):
                    delivery[key] = {"status": "legacy"}
        for index, event in enumerate(events):
            key = str(index)
            previous = delivery.get(key, {})
            if previous.get("status") in {"sent", "legacy", "ignored", "unknown"}:
                continue
            name = event.get("event")
            symbol = str(record.get("signal", {}).get("symbol", "UNKNOWN"))
            timestamp = _safe_int(event.get("time_ms"), 0)
            delayed = bool(timestamp and now_ms - timestamp > 120000)
            clock = _format_event_time_iran(event)
            if name == "ENTRY":
                # Stale ENTRY events were filtered above. Never emit a message
                # claiming the entry had already activated.
                label = f"🟡 ورود فعال شد؛ قیمت اجرا: {float(event.get('fill', record['signal']['entry'])):.8g}"
            elif name == "TP1":
                label = "🎯 تارگت اول خورد"
            elif name == "TP2":
                label = "🏆 تارگت دوم خورد؛ معامله به پایان رسید"
            elif name == "SL":
                label = "🛑 حد ضرر خورد"
            elif name == "AMBIGUOUS_ENTRY_POLL":
                label = "⚠️ ورود و حد ضرر در یک بازه پایش لمس شدند؛ نتیجه معامله قابل تأیید نیست"
            elif name == "AMBIGUOUS_ENTRY_CANDLE":
                label = "⚠️ ورود و حد ضرر/تارگت داخل یک کندل ۱۵دقیقه‌ای لمس شدند؛ ترتیب برخورد مشخص نیست و نتیجه معامله قابل تأیید نیست"
            else:
                delivery[key] = {"status": "ignored"}
                continue
            message = label + "\nارز: " + symbol
            if clock:
                message += f"\nزمان لمس: {clock} (به وقت ایران)"
            if delayed and name == "ENTRY":
                message += "\nاین اعلان با تأخیر ارسال شده؛ مبنای ورود تازه نیست."
            elif delayed and name in {"TP1", "TP2", "SL"}:
                message += "\nاین گزارش مربوط به نتیجه قبلی است؛ برای ورود جدید اقدام نکنید."
            message += _event_price_result(record["signal"], event)
            if event.get("ambiguous_same_candle"):
                message += "\nترتیب لمس حد ضرر و تارگت در این کندل مشخص نیست."
            delivery[key] = {
                "status": "pending",
                "attempts": _safe_int(previous.get("attempts"), 0) + 1,
                "last_attempt_ms": now_ms,
            }
            save_outcome_journal(journal)
            try:
                sent_id = send_telegram(
                    token, chat_id, message,
                    reply_to_message_id=int(anchor_id) if anchor_id else None,
                )
            except Exception as exc:
                # Telegram sendMessage has no idempotency key. A read timeout or
                # broken connection can happen AFTER Telegram accepted the post.
                # Mark delivery unknown and never auto-retry it: retrying could
                # duplicate a real ENTRY/TP/SL notification.
                ambiguous_delivery = _is_ambiguous_telegram_delivery(exc)
                if ambiguous_delivery:
                    delivery[key] = {
                        "status": "unknown",
                        "attempts": delivery[key]["attempts"],
                        "last_attempt_ms": now_ms,
                        "reason": "Telegram response lost; delivery may already have succeeded; automatic retry suppressed",
                        "last_error": f"{type(exc).__name__}: {exc}",
                    }
                    print(
                        f"Outcome notification delivery UNKNOWN; automatic retry suppressed: "
                        f"trade={trade_id} event={name} index={index} "
                        f"error={type(exc).__name__}: {exc}"
                    )
                    save_outcome_journal(journal)
                    continue
                error_text = str(exc).lower()
                # If Telegram rejects only the reply reference (for example,
                # because the original signal was deleted), retry as a normal
                # standalone message so the TP/SL alert is not lost.
                reply_reference_rejected = bool(anchor_id) and any(
                    phrase in error_text
                    for phrase in (
                        "reply message not found",
                        "message to be replied not found",
                        "reply_to_message_id",
                        "message thread not found",
                    )
                )
                if reply_reference_rejected:
                    try:
                        sent_id = send_telegram(token, chat_id, message)
                        print(
                            f"Outcome notification delivered standalone after reply-reference rejection: "
                            f"trade={trade_id} event={name} index={index} telegram_message_id={sent_id}"
                        )
                    except Exception as fallback_exc:
                        delivery[key]["last_error"] = (
                            f"reply failed: {type(exc).__name__}: {exc}; "
                            f"standalone retry failed: {type(fallback_exc).__name__}: {fallback_exc}"
                        )
                        print(
                            f"Outcome notification pending retry: trade={trade_id} event={name} "
                            f"index={index} standalone_error={type(fallback_exc).__name__}: {fallback_exc}"
                        )
                        save_outcome_journal(journal)
                        continue
                else:
                    delivery[key]["last_error"] = f"{type(exc).__name__}: {exc}"
                    print(f"Outcome notification pending retry: trade={trade_id} event={name} index={index} error={type(exc).__name__}: {exc}")
                    save_outcome_journal(journal)
                    continue
            delivery[key] = {
                "status": "sent",
                "attempts": delivery[key]["attempts"],
                "sent_at_ms": int(time.time() * 1000),
                "telegram_message_id": sent_id,
            }
            print(f"Outcome notification delivered: trade={trade_id} event={name} index={index} telegram_message_id={sent_id}")
            save_outcome_journal(journal)
    return journal

def prune_watchlist(watchlist: Dict[str, Any], now: int | None = None) -> Dict[str, Any]:
    timestamp = int(now if now is not None else time.time())
    kept = {}
    for symbol, record in watchlist.items():
        if not isinstance(symbol, str) or not isinstance(record, dict):
            continue
        if not _is_crypto_candidate(symbol):
            continue
        is_market_candidate = record.get("source_type") == "market_universe"
        last_seen = _safe_int(
            record.get("last_alert") or record.get("last_checked")
            or record.get("first_seen") or timestamp,
            timestamp,
        )
        # Keep rotating market-universe candidates long enough to reach them;
        # channel alerts retain the shorter freshness window.
        ttl = 7 * WATCHLIST_TTL_SECONDS if is_market_candidate else WATCHLIST_TTL_SECONDS
        if timestamp - last_seen <= ttl or isinstance(record.get("pending_setup"), dict):
            kept[symbol] = record
    return kept


def _has_supported_perpetual(client: CoinalyzeClient, symbol: str) -> bool:
    """Return False only for a genuine missing Binance perpetual mapping."""
    from oi_engine.coinalyze_client import CoinalyzeError
    try:
        return bool(client.resolve_market(symbol))
    except CoinalyzeError as exc:
        if str(exc).startswith("No Binance USDT perpetual mapping"):
            return False
        raise


def add_market_universe_candidates(
    watchlist: Dict[str, Any],
    client: CoinalyzeClient,
    last_universe_symbol: str = "",
    now: int | None = None,
) -> tuple[str, int]:
    """Add a rotating slice of non-channel crypto perps as the SECOND scan source.

    Channel alerts remain first priority. Market-universe candidates are queued
    behind fresh channel alerts and pending setups, and all use the same engine.
    """
    discover = getattr(client, "get_active_binance_usdt_perpetual_symbols", None)
    if not callable(discover):
        print("Market-universe scan unavailable: provider has no symbol discovery method")
        return last_universe_symbol, 0
    symbols = discover()
    if not symbols:
        print("Market-universe scan: provider returned no supported USDT perpetuals")
        return last_universe_symbol, 0

    queued = {
        symbol for symbol, record in watchlist.items()
        if isinstance(record, dict) and record.get("source_type") == "market_universe"
    }
    # Migrate older persisted queues to the current 60-coin rotation cap.
    # Keep pending setups first, then never-scanned/least-recently-scanned
    # candidates so trimming does not starve the rotation.
    if len(queued) > MAX_MARKET_QUEUE_SYMBOLS:
        ranked_queue = sorted(
            queued,
            key=lambda symbol: (
                0 if isinstance(watchlist[symbol].get("pending_setup"), dict) else 1,
                _safe_int(watchlist[symbol].get("last_checked", 0)),
                -_safe_float(watchlist[symbol].get("oi_priority_score", 0.0)),
                _safe_int(watchlist[symbol].get("first_seen", 0)),
            ),
        )
        keep = set(ranked_queue[:MAX_MARKET_QUEUE_SYMBOLS])
        for symbol in queued - keep:
            watchlist.pop(symbol, None)
        queued = keep
        print(
            f"Independent market universe queue trimmed to {MAX_MARKET_QUEUE_SYMBOLS}; "
            f"preserved={len(queued)}"
        )
    free_slots = max(0, MAX_MARKET_QUEUE_SYMBOLS - len(queued))
    if free_slots == 0:
        # A fixed-size queue must not freeze forever at its first 60 coins.
        # Retire only candidates already attempted and without a pending setup;
        # newly discovered symbols then replace them while the universe cursor
        # keeps moving through the complete Coinalyze-supported market.
        evictable = [
            symbol for symbol in queued
            if _safe_int(watchlist[symbol].get("last_attempted_at", 0)) > 0
            and not isinstance(watchlist[symbol].get("pending_setup"), dict)
        ]
        evictable.sort(key=lambda symbol: (
            _safe_int(watchlist[symbol].get("last_attempted_at", 0)),
            _safe_int(watchlist[symbol].get("last_checked", 0)),
            _safe_int(watchlist[symbol].get("first_seen", 0)),
        ))
        for symbol in evictable[:max(1, min(MARKET_DISCOVERY_PER_RUN, len(evictable)))]:
            watchlist.pop(symbol, None)
            queued.discard(symbol)
        free_slots = max(0, MAX_MARKET_QUEUE_SYMBOLS - len(queued))
        if free_slots == 0:
            return last_universe_symbol, 0

    try:
        start = symbols.index(last_universe_symbol) + 1
    except ValueError:
        start = 0
    added = 0
    next_cursor = last_universe_symbol
    timestamp = int(now if now is not None else time.time())
    max_add = min(free_slots, MARKET_DISCOVERY_PER_RUN)
    for offset in range(len(symbols)):
        symbol = symbols[(start + offset) % len(symbols)]
        if symbol in watchlist or not _is_crypto_candidate(symbol):
            continue
        watchlist[symbol] = {
            "source_type": "market_universe",
            "first_seen": timestamp,
            "last_checked": 0,
            "oi_priority_score": 0.0,
            "oi_priority_tier": "MARKET",
            "status": "در صف اسکن مستقل بازار",
            "reason": "نامزد اسکن بازار؛ هنوز هشدار کانال OI برای این ارز ثبت نشده است",
        }
        next_cursor = symbol
        added += 1
        if added >= max_add:
            break

    print(
        f"Independent market universe: supported={len(symbols)}, "
        f"queued_before={len(queued)}, added={added}, "
        f"queued_after={len(queued) + added}, cursor={next_cursor}"
    )
    return next_cursor, added


def select_watchlist_batch(watchlist, last_scanned_symbol="", limit=MAX_WATCHLIST_SYMBOLS):
    """Prioritize fresh alerts while reserving capacity for expiring pullback setups."""
    symbols = list(watchlist)
    if not symbols:
        return {}, ""

    count = min(max(1, _safe_int(limit, MAX_WATCHLIST_SYMBOLS)), len(symbols))
    score = lambda symbol: _safe_float(watchlist[symbol].get("oi_priority_score", 0.0))

    fresh_alerts = [
        symbol for symbol in symbols
        if _safe_int(
            watchlist[symbol].get(
                "last_alert_received_at", watchlist[symbol].get("last_alert", 0)
            )
        ) > _safe_int(watchlist[symbol].get("last_checked", 0))
    ]
    # First attempts stay ahead of failed retries.
    fresh_alerts.sort(
        key=lambda symbol: (
            _safe_int(watchlist[symbol].get("last_attempted_at", 0)),
            -_safe_int(watchlist[symbol].get("last_alert_received_at", watchlist[symbol].get("last_alert", 0))),
            -score(symbol),
        )
    )

    pending_all = [
        symbol for symbol in symbols
        if isinstance(watchlist[symbol].get("pending_setup"), dict)
    ]
    # The nearest expiry is the most urgent; age breaks ties.
    pending_all.sort(key=lambda symbol: (
        0 if watchlist[symbol].get("source_type") == "oi_channel" else 1,
        _safe_int(watchlist[symbol].get("last_checked", 0)),
        _safe_int(watchlist[symbol]["pending_setup"].get("expires_at_close_time", 0)),
    ))

    # Pending setups need frequent rechecks: reserve up to four of twelve
    # slots, while keeping at least two thirds of capacity for fresh/hot
    # candidates. Smaller batches retain the previous two-slot ceiling.
    pending_target = (
        min(len(pending_all), max(2, min(4, count // 3)))
        if count >= 2 and pending_all else 0
    )
    market_candidates = [
        symbol for symbol in symbols
        if watchlist[symbol].get("source_type") == "market_universe"
        and not isinstance(watchlist[symbol].get("pending_setup"), dict)
    ]
    # Failed market-data attempts remain in the queue. Move recently attempted
    # candidates behind never-attempted/older candidates so they cannot occupy
    # the same reserved slots on every run.
    market_candidates.sort(key=lambda symbol: (
        _safe_int(watchlist[symbol].get("last_attempted_at", 0)),
        _safe_int(watchlist[symbol].get("first_seen", 0)),
    ))
    # Guarantee up to two independent-market slots in the 8-symbol batch.
    # This keeps the 60-coin universe moving while preserving channel priority.
    market_reserve = (
        min(len(market_candidates), min(2, max(1, count // 4)))
        if market_candidates and count >= 3 else 0
    )
    fresh_limit = max(0, count - pending_target - market_reserve)
    selected = fresh_alerts[:fresh_limit]

    # A pending setup may itself be a fresh alert; count it toward the reserve
    # rather than selecting it twice or wasting a reserved slot.
    selected_pending = sum(1 for symbol in selected if symbol in pending_all)
    pending_slots = max(0, pending_target - selected_pending)
    pending_remaining = [symbol for symbol in pending_all if symbol not in selected]
    pending = pending_remaining[:pending_slots]
    selected.extend(pending)

    # If a pending setup was already included among fresh alerts, use the
    # freed capacity for the next fresh alerts instead of leaving a slot idle.
    channel_capacity = count - market_reserve
    if len(selected) < channel_capacity:
        fresh_remaining = [symbol for symbol in fresh_alerts if symbol not in selected]
        selected.extend(fresh_remaining[:channel_capacity - len(selected)])

    # Channel alerts and channel pending setups are selected first; then
    # guarantee forward progress through the independent market universe.
    market_added = 0
    for symbol in market_candidates:
        if symbol not in selected:
            selected.append(symbol)
            market_added += 1
            if market_added >= market_reserve or len(selected) >= count:
                break

    remaining_slots = count - len(selected)
    if remaining_slots <= 0:
        return {symbol: watchlist[symbol] for symbol in selected}, last_scanned_symbol

    remaining = [symbol for symbol in symbols if symbol not in selected]
    ranked = sorted(remaining, key=score, reverse=True)
    pending_remaining = [symbol for symbol in pending_all if symbol not in selected]

    # If every watchlist symbol is pending, use otherwise-idle capacity
    # to revisit more setups. Otherwise preserve slots for hot and rotating
    # candidates instead of letting a large pending queue monopolize a batch.
    if len(selected) < count and len(pending_all) == len(symbols):
        for symbol in pending_remaining:
            if symbol not in selected:
                selected.append(symbol)
                if len(selected) >= count:
                    break
    remaining_slots = count - len(selected)

    hot = [
        symbol for symbol in ranked
        if symbol not in pending_all and score(symbol) > 0.0
    ][:min(2, remaining_slots)]
    selected.extend(hot)

    rotating = [
        symbol for symbol in ranked
        if symbol not in selected and symbol not in pending_all
    ]
    rotating_slots = count - len(selected)
    rotating_selected = []
    if rotating_slots > 0 and rotating:
        rank_position = {symbol: index for index, symbol in enumerate(ranked)}
        cursor_position = rank_position.get(last_scanned_symbol, -1)
        start = next(
            (index for index, symbol in enumerate(rotating)
             if rank_position[symbol] > cursor_position),
            0,
        )
        rotating_selected = [
            rotating[(start + offset) % len(rotating)]
            for offset in range(min(rotating_slots, len(rotating)))
        ]
        selected.extend(rotating_selected)

    next_cursor = rotating_selected[-1] if rotating_selected else last_scanned_symbol
    return {symbol: watchlist[symbol] for symbol in selected}, next_cursor

def summarize_scan_diagnostics(watchlist, scanned_batch):
    """Build compact counters explaining where the current scan batch stands."""
    statuses = {}
    sources = {}
    reasons = {"coinalyze_rejected": 0, "pending_pullback": 0, "setup_invalidated_or_expired": 0, "data_errors": 0}
    for record in scanned_batch.values():
        source = str(record.get("source_type", "oi_channel"))
        sources[source] = sources.get(source, 0) + 1
        status = str(record.get("status", "نامشخص"))
        statuses[status] = statuses.get(status, 0) + 1
        reason = str(record.get("reason", ""))
        # Count both the legacy wording and the current live-engine wording.
        # The latter used to be invisible in diagnostics, falsely reporting 0.
        if ("Coinalyze رد کرد" in reason
                or "تأیید مستقل Coinalyze کافی نیست" in reason
                or "رد تأیید مشتقات:" in reason):
            reasons["coinalyze_rejected"] += 1
        if (isinstance(record.get("pending_setup"), dict)
                or status == "منتظر پولبک و تأیید"):
            reasons["pending_pullback"] += 1
        if any(word in reason for word in ("باطل", "منقضی", "مهلت پولبک", "setup پاک شد")):
            reasons["setup_invalidated_or_expired"] += 1
        if "خطا" in status:
            reasons["data_errors"] += 1
    high_priority = sum(
        1 for record in watchlist.values()
        if isinstance(record, dict) and float(record.get("oi_priority_score", 0.0)) > 0.0
    )
    return {
        "watchlist_total": len(watchlist),
        "high_oi_priority": high_priority,
        "scanned": len(scanned_batch),
        "not_scanned_this_cycle": max(0, len(watchlist) - len(scanned_batch)),
        "statuses": statuses,
        "sources": sources,
        "reasons": reasons,
    }


def blocked_by_active_symbol_trade(signal, outcome_journal: Dict[str, Any]) -> bool:
    """Allow at most one unresolved trade per symbol, regardless of side or engine."""
    symbol = str(signal.symbol).upper()
    terminal_statuses = {"CLOSED_SL", "CLOSED_TP2", "TIMED_OUT", "EXPIRED_NO_ENTRY", "AMBIGUOUS_ENTRY_CANDLE", "AMBIGUOUS_ENTRY_POLL", "INVALID_NO_ENTRY", "MISSED_ENTRY"}
    for record in outcome_journal.get("trades", {}).values():
        prior_signal = record.get("signal", {})
        if str(prior_signal.get("symbol", "")).upper() != symbol:
            continue
        # OPEN and TP1_HIT both represent an unresolved position. Do not let
        # another LONG/SHORT alert for this coin create a second simultaneous trade.
        if str(record.get("status", "OPEN")).upper() not in terminal_statuses:
            return True
    return False


def blocked_by_same_candle_opposite_stop(signal, outcome_journal: Dict[str, Any]) -> bool:
    """Block an opposite-side re-entry until a newer 15m candle has closed."""
    symbol = str(signal.symbol)
    side = str(signal.side).upper()
    candle_close_time = int(signal.candle_close_time)
    for record in outcome_journal.get("trades", {}).values():
        prior_signal = record.get("signal", {})
        if str(prior_signal.get("symbol", "")) != symbol:
            continue
        prior_side = str(prior_signal.get("side", "")).upper()
        if prior_side == side or prior_side not in {"LONG", "SHORT"}:
            continue
        if record.get("status") != "CLOSED_SL":
            continue
        stop_times = [
            int(event.get("time_ms", 0))
            for event in record.get("events", [])
            if event.get("event") == "SL"
        ]
        if stop_times and candle_close_time <= max(stop_times):
            return True
    return False


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    api_key = os.getenv("COINALYZE_API_KEY")
    if not token or not chat_id:
        raise SystemExit("BOT_TOKEN and CHAT_ID GitHub secrets are required")
    if not api_key:
        raise SystemExit("COINALYZE_API_KEY GitHub secret is required")
    verify_telegram_connection(token, chat_id)

    state = load_state()
    watch_state = load_watchlist_state()
    outcome_journal = load_outcome_journal()
    # @oi_detector alerts are the first-priority source; the independent
    # Coinalyze-supported crypto perpetual universe is scanned after them.
    # Never delete queued market candidates between scheduled runs.
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

    client = CoinalyzeClient(api_key=api_key, include_optional_metrics=False)
    # Price history is Binance USD-M first, then Bybit linear perpetual.
    # Coinalyze is never used as a price/candle fallback.
    binance_feed = {"15m": None, "1m": None}

    def fetch_trade_15m(symbol: str, start_ms: int, end_ms: int):
        rows = _fetch_klines(symbol, start_ms, end_ms)
        binance_feed["15m"] = getattr(_fetch_klines, "last_source", "unknown") == "Binance USD-M"
        return rows

    def fetch_trade_1m(symbol: str, start_ms: int, end_ms: int):
        rows = _fetch_1m_klines(symbol, start_ms, end_ms)
        binance_feed["1m"] = getattr(_fetch_1m_klines, "last_source", "unknown") == "Binance USD-M"
        return rows

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

    # Add a bounded rotating slice of the broader supported crypto market.
    # Channel alerts remain ahead of these candidates in batch selection.
    market_cursor = str(watch_state.get("market_universe_cursor", ""))
    try:
        market_cursor, market_added = add_market_universe_candidates(
            watchlist, client, market_cursor, now=int(time.time())
        )
        print(f"Market universe queue update: added={market_added}, total={sum(1 for r in watchlist.values() if isinstance(r, dict) and r.get('source_type') == 'market_universe')}")
    except Exception as exc:
        # A discovery/API failure must not discard fresh channel alerts.
        print(f"WARNING: market-universe discovery failed: {type(exc).__name__}: {exc}")

    # Persist both source cursors before market-data calls so newly ingested
    # channel alerts and the rotating market queue survive a later timeout.
    save_watchlist_state({
        "public_channel_last_id": public_last_id,
        "market_universe_cursor": market_cursor,
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

    # Fresh OI-channel alerts and their pending setups are prioritized;
    # reserved slots keep the broader crypto market rotating in the background.
    batch, next_scan_cursor = select_watchlist_batch(
        watchlist, str(watch_state.get("scan_cursor", ""))
    )
    confirmed, scanned_batch = scan_watchlist(batch, client=client)
    watchlist.update(scanned_batch)

    # One unresolved position per coin: suppress repeated same-side signals and
    # opposite-side flips until the existing trade reaches SL, TP2, or timeout.
    before_active_filter = len(confirmed)
    confirmed = [
        (signal, metrics) for signal, metrics in confirmed
        if not blocked_by_active_symbol_trade(signal, outcome_journal)
    ]
    active_blocked = before_active_filter - len(confirmed)
    if active_blocked:
        print(f"Duplicate-symbol guard: suppressed {active_blocked} signal(s) because a trade is still active")

    # A stop and an opposite signal can be observed in the same runner cycle.
    # Require a strictly newer closed 15m candle before allowing the reversal.
    confirmed = [
        (signal, metrics) for signal, metrics in confirmed
        if not blocked_by_same_candle_opposite_stop(signal, outcome_journal)
    ]

    sent = 0
    for signal, metrics in confirmed:
        key = f"{metrics.get('engine', 'UNSPECIFIED')}:{signal.symbol}:{signal.side}"
        candle_id = int(signal.candle_close_time)
        if state.get(key) == candle_id:
            continue

        # Persist the trade and reserve this signal BEFORE the network request.
        # Telegram sendMessage has no idempotency key: if the request succeeds
        # but its response is lost, a restart must not send the same alert twice.
        trade_record = make_trade_record(signal, metrics, int(time.time() * 1000))
        trade_record["telegram_chat_id"] = str(chat_id)
        trade_record["telegram_message_id"] = None
        trade_record["telegram_delivery_status"] = "pending"
        trade_id = trade_record["trade_id"]
        created_trade = trade_id not in outcome_journal.setdefault("trades", {})
        outcome_journal["trades"].setdefault(trade_id, trade_record)
        state[key] = candle_id
        save_state(state)
        if os.getenv("PUMP_HUNTER_EXTERNAL_LIVE_MONITOR") == "1":
            register_outcome_trade(trade_record)
            outcome_journal = load_outcome_journal()
        else:
            save_outcome_journal(outcome_journal)

        try:
            message_id = send_telegram(
                token, chat_id, format_confirmed_signal(signal, metrics), parse_mode="HTML"
            )
        except Exception as exc:
            # Always merge delivery state into the latest journal under the
            # shared lock. The live monitor may be running in another thread.
            with OUTCOME_JOURNAL_LOCK:
                latest = load_outcome_journal()
                tracked = latest.get("trades", {}).get(trade_id)
                if tracked is None and created_trade:
                    tracked = trade_record
                    latest.setdefault("trades", {})[trade_id] = tracked
                if _is_ambiguous_telegram_delivery(exc):
                    if tracked is not None:
                        tracked["telegram_delivery_status"] = "unknown"
                        tracked["telegram_delivery_error"] = f"{type(exc).__name__}: {exc}"
                    save_outcome_journal(latest)
                    outcome_journal = latest
                    print(
                        "Signal Telegram delivery UNKNOWN; duplicate retry suppressed: "
                        f"trade={trade_id} symbol={signal.symbol} "
                        f"error={type(exc).__name__}: {exc}"
                    )
                    continue

                # A definite Telegram rejection is safe to retry on a later scan.
                state.pop(key, None)
                if created_trade:
                    latest.get("trades", {}).pop(trade_id, None)
                save_outcome_journal(latest)
                outcome_journal = latest
            save_state(state)
            raise

        # Re-read under the same lock used by the live monitor so its journal
        # updates cannot be overwritten by this scanner's stale in-memory copy.
        with OUTCOME_JOURNAL_LOCK:
            latest = load_outcome_journal()
            tracked = latest.get("trades", {}).get(trade_id)
            if tracked is None:
                tracked = trade_record
                latest.setdefault("trades", {})[trade_id] = tracked
            tracked["telegram_message_id"] = message_id if isinstance(message_id, int) else None
            tracked["telegram_delivery_status"] = "sent"
            tracked.pop("telegram_delivery_error", None)
            save_outcome_journal(latest)
            outcome_journal = latest
        sent += 1

    if os.getenv("PUMP_HUNTER_EXTERNAL_LIVE_MONITOR") != "1":
        previous_events = {
            trade_id: {
                "events": len(record.get("events", [])),
                "post_stop_tp1": bool(record.get("post_stop_tp1_reached")),
                "post_stop_tp2": bool(record.get("post_stop_tp2_reached")),
            }
            for trade_id, record in outcome_journal.get("trades", {}).items()
        }
        outcome_journal = monitor_journal(
            outcome_journal,
            fetcher=fetch_trade_15m,
            micro_fetcher=fetch_trade_1m,
        )
        print(
            "Outcome price feed: "
            f"15m={('Binance USD-M' if binance_feed['15m'] else 'Bybit/Gate fallback') if binance_feed['15m'] is not None else 'not used'}; "
            f"1m={('Binance USD-M' if binance_feed['1m'] else 'Bybit/Gate fallback') if binance_feed['1m'] is not None else 'not used'}"
        )
        save_outcome_journal(outcome_journal)
        outcome_journal = dispatch_outcome_notifications(outcome_journal, token, chat_id)
        outcome_statuses = {}
        for trade in outcome_journal.get("trades", {}).values():
            status = trade.get("status", "UNKNOWN")
            outcome_statuses[status] = outcome_statuses.get(status, 0) + 1
        provider_errors = sum(bool(trade.get("last_error")) for trade in outcome_journal.get("trades", {}).values())
        print(f"Outcome monitor: statuses={outcome_statuses}, provider_errors={provider_errors}")
        print(f"Outcome report: {json.dumps(summarize_outcomes(outcome_journal), ensure_ascii=False, sort_keys=True)}")
        for trade_id, trade in outcome_journal.get("trades", {}).items():
            events = [f"{event.get('event')}@{_safe_int(event.get('time_ms'), 0)}" for event in trade.get("events", [])]
            delivery = trade.get("notification_state", {})
            delivery_summary = {str(index): state.get("status", "unknown") for index, state in delivery.items() if isinstance(state, dict)}
            print(
                "Outcome trade diagnostic: "
                f"trade={trade_id} status={trade.get('status')} "
                f"events={events} notifications={delivery_summary} "
                f"replay_count={trade.get('last_replay_candle_count', 'n/a')} "
                f"replay_last_ms={trade.get('last_replay_last_candle_ms', 'n/a')} "
                f"last_error={trade.get('last_error') or 'none'}"
            )
        if len(state) > 500:
            state = dict(sorted(state.items(), key=lambda item: item[1])[-500:])
            save_state(state)
        save_outcome_journal(outcome_journal)
    else:
        outcome_journal = load_outcome_journal()
        print("Outcome replay/dispatch delegated to continuous live monitor")

    save_watchlist_state({
        "public_channel_last_id": public_last_id,
        "market_universe_cursor": market_cursor,
        "symbols": watchlist,
        "scan_cursor": next_scan_cursor or str(watch_state.get("scan_cursor", "")),
        "updated_at": int(time.time()),
    })
    diagnostics = summarize_scan_diagnostics(watchlist, scanned_batch)
    print(f"OI scan diagnostics: {diagnostics}")
    print(
        f"Alert-driven OI scan complete: watchlist={len(watchlist)}, "
        f"new_public_channel_alerts={added}, "
        f"confirmed={len(confirmed)}, telegram_sent={sent}, "
        f"tracked_trades={len(outcome_journal.get('trades', {}))}"
    )
    for symbol, record in scanned_batch.items():
        print(
            f"Watchlist {symbol}: {record.get('status')} - {record.get('reason', '')} "
            f"[source={record.get('source_type', 'oi_channel')}, market_source={record.get('data_source', 'unknown')}, "
            f"alert_message_id={record.get('source_message_id', 'unknown')}]"
        )


if __name__ == "__main__":
    main()