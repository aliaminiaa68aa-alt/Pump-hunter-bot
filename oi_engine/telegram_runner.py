"""Scheduled alert-driven OI + Coinalyze Telegram runner."""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Dict

import requests

from oi_engine.telegram_public_source import poll_public_channel_alerts
from oi_engine.alert_ingest import _is_crypto_candidate, parse_oi_alert_direction
from oi_engine.coinalyze_client import CoinalyzeClient
from oi_engine.watchlist_engine import scan_watchlist, format_confirmed_signal, source_oi_priority
from oi_engine.outcome_tracker import make_trade_record, monitor_journal, summarize_outcomes

STATE_PATH = Path(".oi_state/sent_signals.json")
WATCHLIST_PATH = Path(".oi_state/watchlist.json")
OUTCOME_PATH = Path(".oi_state/trade_outcomes.json")
MAX_WATCHLIST_SYMBOLS = 12  # Core Coinalyze scan uses 3 calls/symbol; keep below the 40/min limit with normal outcome load
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


def load_outcome_journal() -> Dict[str, Any]:
    raw = _load_json(OUTCOME_PATH, {"trades": {}})
    return raw if isinstance(raw, dict) else {"trades": {}}


def save_outcome_journal(journal: Dict[str, Any]) -> None:
    _save_json(OUTCOME_PATH, journal)


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


def send_telegram(
    token: str, chat_id: str, message: str,
    reply_to_message_id: int | None = None, parse_mode: str | None = None,
) -> int | None:
    data = {
        "chat_id": chat_id, "text": message,
        **({"reply_to_message_id": int(reply_to_message_id)} if reply_to_message_id else {}),
        **({"parse_mode": parse_mode} if parse_mode else {}),
    }
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram rejected message: {payload}")
    result = payload.get("result") or {}
    return int(result["message_id"]) if result.get("message_id") is not None else None


def prune_watchlist(watchlist: Dict[str, Any], now: int | None = None) -> Dict[str, Any]:
    timestamp = int(now if now is not None else time.time())
    kept = {}
    for symbol, record in watchlist.items():
        if not isinstance(symbol, str) or not isinstance(record, dict):
            continue
        if not _is_crypto_candidate(symbol):
            continue
        last_seen = _safe_int(
            record.get("last_alert", record.get("first_seen", timestamp)),
            timestamp,
        )
        if timestamp - last_seen <= WATCHLIST_TTL_SECONDS:
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


def select_watchlist_batch(watchlist, last_scanned_symbol="", limit=MAX_WATCHLIST_SYMBOLS):
    """Prioritize pending setups and strong OI while preserving fair queue rotation."""
    symbols = list(watchlist)
    if not symbols:
        return {}, ""
    count = min(max(1, _safe_int(limit, MAX_WATCHLIST_SYMBOLS)), len(symbols))
    ranked = sorted(
        symbols,
        key=lambda symbol: _safe_float(watchlist[symbol].get("oi_priority_score", 0.0)),
        reverse=True,
    )

    # Pending setups are important, but must not consume the entire scan budget:
    # otherwise fresh alerts can wait up to their one-hour setup expiry.
    pending_all = [
        symbol for symbol in symbols
        if isinstance(watchlist[symbol].get("pending_setup"), dict)
    ]
    pending_all.sort(key=lambda symbol: (
        _safe_int(watchlist[symbol].get("last_checked", 0)),
        _safe_int(watchlist[symbol]["pending_setup"].get("expires_at_close_time", 0)),
    ))
    if len(symbols) <= count or len(pending_all) == len(symbols):
        pending_slots = count
    else:
        pending_slots = max(1, count // 2)
    pending = pending_all[:pending_slots]
    selected = list(pending)

    remaining_slots = count - len(selected)
    hot = [
        symbol for symbol in ranked
        if symbol not in pending_all
        and _safe_float(watchlist[symbol].get("oi_priority_score", 0.0)) > 0.0
    ][:min(2, remaining_slots)]
    selected.extend(hot)

    rotating = [
        symbol for symbol in ranked
        if symbol not in selected and symbol not in pending_all
    ]
    rotating_slots = count - len(selected)
    rotating_selected = []
    if rotating_slots > 0 and rotating:
        # The cursor may itself be a pending or hot symbol. Find its position in
        # the full ranking, then resume with the next eligible rotating symbol.
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

    # If there are too few non-pending candidates to fill the batch, use the
    # remaining slots for the next pending setups in their fair queue order.
    if len(selected) < count:
        for symbol in pending_all:
            if symbol not in selected:
                selected.append(symbol)
                if len(selected) >= count:
                    break

    next_cursor = rotating_selected[-1] if rotating_selected else last_scanned_symbol
    return {symbol: watchlist[symbol] for symbol in selected}, next_cursor


def summarize_scan_diagnostics(watchlist, scanned_batch):
    """Build compact counters explaining where the current scan batch stands."""
    statuses = {}
    reasons = {"coinalyze_rejected": 0, "pending_pullback": 0, "setup_invalidated_or_expired": 0, "data_errors": 0}
    for record in scanned_batch.values():
        status = str(record.get("status", "نامشخص"))
        statuses[status] = statuses.get(status, 0) + 1
        reason = str(record.get("reason", ""))
        if "Coinalyze رد کرد" in reason:
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
        "reasons": reasons,
    }


def blocked_by_active_symbol_trade(signal, outcome_journal: Dict[str, Any]) -> bool:
    """Allow at most one unresolved trade per symbol, regardless of side or engine."""
    symbol = str(signal.symbol).upper()
    terminal_statuses = {"CLOSED_SL", "CLOSED_TP2", "TIMED_OUT"}
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

    client = CoinalyzeClient(api_key=api_key, include_optional_metrics=False)
    # Binance Futures returns HTTP 451 on this runner; use the same Coinalyze
    # 15m OHLCV fallback as the scanner so outcome tracking can still progress.
    previous_events = {
        trade_id: {
            "events": len(record.get("events", [])),
            "post_stop_tp1": bool(record.get("post_stop_tp1_reached")),
            "post_stop_tp2": bool(record.get("post_stop_tp2_reached")),
        }
        for trade_id, record in outcome_journal.get("trades", {}).items()
    }
    outcome_journal = monitor_journal(outcome_journal, fetcher=client.fetch_ohlcv_candles)
    for trade_id, record in outcome_journal.get("trades", {}).items():
        anchor_id = record.get("telegram_message_id")
        if not anchor_id:
            continue
        before = previous_events.get(trade_id, {"events": 0, "post_stop_tp1": False, "post_stop_tp2": False})
        for event in record.get("events", [])[int(before["events"]):]:
            name = event.get("event")
            labels = {
                "TP1": "🎯 تارگت اول خورد\n💰 مدیریت سرمایه: بخشی از سود را ذخیره کنید و حد ضررِ باقی‌مانده را به نقطه ورود منتقل کنید.",
                "TP2": "🏆 تارگت دوم خورد؛ معامله به پایان رسید",
            }
            if name == "SL":
                sl_time = int(event.get("time_ms", 0))
                tp1_was_hit = any(
                    prior.get("event") == "TP1"
                    and int(prior.get("time_ms", 0)) < sl_time
                    for prior in record.get("events", [])
                )
                label = (
                    "🛑 حد ضرر بخش باقی‌مانده خورد؛ تارگت اول قبلاً ثبت شده"
                    if tp1_was_hit else "🛑 حد ضرر قبل از تارگت اول خورد"
                )
            else:
                label = labels.get(name)
            if label:
                ambiguity = " (در همین کندل تارگت هم لمس شده؛ ترتیب دقیق داخل کندل مشخص نیست)" if event.get("ambiguous_same_candle") else ""
                result_pct = _event_price_result(record["signal"], event)
                send_telegram(token, chat_id, label + "\nارز: " + str(record["signal"]["symbol"]) + result_pct + ambiguity,
                              reply_to_message_id=int(anchor_id))
        if record.get("post_stop_tp1_reached") and not before["post_stop_tp1"]:
            move_pct = float(record.get("post_stop_mfe_pct") or 0.0)
            send_telegram(token, chat_id, f"ℹ️ پس از خوردن حد ضرر، قیمت بعداً به تارگت اول رسید.\nبیشترین حرکت مثبت از ورود: +{move_pct:.2f}%",
                          reply_to_message_id=int(anchor_id))
        if record.get("post_stop_tp2_reached") and not before["post_stop_tp2"]:
            move_pct = float(record.get("post_stop_mfe_pct") or 0.0)
            send_telegram(token, chat_id, f"ℹ️ پس از خوردن حد ضرر، قیمت بعداً به تارگت دوم رسید.\nبیشترین حرکت مثبت از ورود: +{move_pct:.2f}%",
                          reply_to_message_id=int(anchor_id))
    save_outcome_journal(outcome_journal)
    outcome_statuses = {}
    for trade in outcome_journal.get("trades", {}).values():
        status = trade.get("status", "UNKNOWN")
        outcome_statuses[status] = outcome_statuses.get(status, 0) + 1
    provider_errors = sum(bool(trade.get("last_error")) for trade in outcome_journal.get("trades", {}).values())
    print(f"Outcome monitor: statuses={outcome_statuses}, provider_errors={provider_errors}")
    print(f"Outcome report: {json.dumps(summarize_outcomes(outcome_journal), ensure_ascii=False, sort_keys=True)}")
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
        message_id = send_telegram(token, chat_id, format_confirmed_signal(signal, metrics), parse_mode="HTML")
        trade_record = make_trade_record(signal, metrics, int(time.time() * 1000))
        trade_record["telegram_chat_id"] = str(chat_id)
        trade_record["telegram_message_id"] = message_id if isinstance(message_id, int) else None
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
    diagnostics = summarize_scan_diagnostics(watchlist, scanned_batch)
    print(f"OI scan diagnostics: {diagnostics}")
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
