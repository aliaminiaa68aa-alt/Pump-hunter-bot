import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oi_engine.signal_engine import Signal
from oi_engine import telegram_runner


def sample_signal():
    return Signal("TESTUSDT", "LONG", 100.0, 98.0, 102.0, 104.0,
                  0.8, 0.3, 0.56, 1.4, 0.0001, 82, 900000)


def sample_metrics():
    return {
        "oi_change_15m_window_pct": 0.4,
        "taker_buy_ratio": 0.60,
        "funding_rate": 0.0001,
        "predicted_funding_rate": 0.0002,
        "long_short_ratio": 1.2,
        "long_liquidations": 1200,
        "short_liquidations": 900,
        "data_quality": {"complete": True},
    }


class FakeClient:
    def resolve_market(self, symbol):
        return symbol

    def fetch_ohlcv_candles(self, symbol, start_ms, end_ms):
        return []


class TelegramRunnerTests(unittest.TestCase):
    def test_opposite_signal_blocked_on_same_stop_candle_but_allowed_on_newer_close(self):
        journal = {
            "trades": {
                "old-long": {
                    "status": "CLOSED_SL",
                    "signal": {"symbol": "TESTUSDT", "side": "LONG"},
                    "events": [{"event": "SL", "time_ms": 900000}],
                }
            }
        }
        same_candle_short = Signal(
            "TESTUSDT", "SHORT", 100.0, 102.0, 98.0, 96.0,
            0.8, -0.3, 0.40, 1.4, 0.0001, 82, 900000
        )
        next_candle_short = Signal(
            "TESTUSDT", "SHORT", 100.0, 102.0, 98.0, 96.0,
            0.8, -0.3, 0.40, 1.4, 0.0001, 82, 1800000
        )
        self.assertTrue(telegram_runner.blocked_by_same_candle_opposite_stop(
            same_candle_short, journal
        ))
        self.assertFalse(telegram_runner.blocked_by_same_candle_opposite_stop(
            next_candle_short, journal
        ))

    def test_same_side_signal_is_not_blocked_by_previous_stop(self):
        journal = {
            "trades": {
                "old-long": {
                    "status": "CLOSED_SL",
                    "signal": {"symbol": "TESTUSDT", "side": "LONG"},
                    "events": [{"event": "SL", "time_ms": 900000}],
                }
            }
        }
        next_long = sample_signal()
        self.assertFalse(telegram_runner.blocked_by_same_candle_opposite_stop(
            next_long, journal
        ))

    def test_same_symbol_active_trade_blocks_repeated_same_side_signal(self):
        journal = {
            "trades": {
                "active-short": {
                    "status": "OPEN",
                    "signal": {"symbol": "TESTUSDT", "side": "SHORT"},
                    "events": [],
                }
            }
        }
        repeated_short = Signal(
            "TESTUSDT", "SHORT", 99.0, 101.0, 97.0, 95.0,
            0.8, -0.3, 0.40, 0.4, 0.0001, 70, 1800000
        )
        self.assertTrue(telegram_runner.blocked_by_active_symbol_trade(repeated_short, journal))

    def test_same_symbol_active_trade_blocks_opposite_side_signal(self):
        journal = {
            "trades": {
                "active-short": {
                    "status": "TP1_HIT",
                    "signal": {"symbol": "TESTUSDT", "side": "SHORT"},
                    "events": [{"event": "TP1", "time_ms": 1000000}],
                }
            }
        }
        new_long = sample_signal()
        self.assertTrue(telegram_runner.blocked_by_active_symbol_trade(new_long, journal))

    def test_closed_trade_does_not_block_new_signal(self):
        journal = {
            "trades": {
                "closed-short": {
                    "status": "CLOSED_SL",
                    "signal": {"symbol": "TESTUSDT", "side": "SHORT"},
                    "events": [{"event": "SL", "time_ms": 1000000}],
                }
            }
        }
        self.assertFalse(telegram_runner.blocked_by_active_symbol_trade(sample_signal(), journal))

    def test_trade_update_reports_signed_price_percentage_for_long_and_short(self):
        long_signal = {"entry": 100.0, "side": "LONG", "take_profit_1": 102.0, "stop_loss": 98.0}
        short_signal = {"entry": 100.0, "side": "SHORT", "take_profit_1": 98.0, "stop_loss": 102.0}
        self.assertEqual(
            telegram_runner._event_price_result(long_signal, {"event": "TP1", "fill": 102.0}),
            "\nسود قیمتی: +2.00%",
        )
        self.assertEqual(
            telegram_runner._event_price_result(long_signal, {"event": "SL", "fill": 98.0}),
            "\nضرر قیمتی: -2.00%",
        )
        self.assertEqual(
            telegram_runner._event_price_result(short_signal, {"event": "TP1", "fill": 98.0}),
            "\nسود قیمتی: +2.00%",
        )
        self.assertEqual(
            telegram_runner._event_price_result(short_signal, {"event": "SL", "fill": 102.0}),
            "\nضرر قیمتی: -2.00%",
        )

    def _run_main(self, directory, scan_result, public_result=(123, 1), sent_state=None, initial_watchlist=None, now=None):
        state_path = Path(directory) / "sent_signals.json"
        watch_path = Path(directory) / "watchlist.json"
        outcome_path = Path(directory) / "trade_outcomes.json"
        if sent_state is not None:
            state_path.write_text(json.dumps(sent_state), encoding="utf-8")
        if initial_watchlist is not None:
            watch_path.write_text(json.dumps({"public_channel_last_id": 10, "symbols": initial_watchlist}), encoding="utf-8")
        env = {"BOT_TOKEN": "token", "CHAT_ID": "chat", "COINALYZE_API_KEY": "key"}
        with patch.object(telegram_runner, "STATE_PATH", state_path), \
             patch.object(telegram_runner, "WATCHLIST_PATH", watch_path), \
             patch.object(telegram_runner, "OUTCOME_PATH", outcome_path), \
             patch.dict(os.environ, env), \
             patch.object(telegram_runner, "CoinalyzeClient", return_value=FakeClient()), \
             patch.object(telegram_runner, "poll_public_channel_alerts", return_value=public_result) as poll, \
             patch.object(telegram_runner, "scan_watchlist", return_value=scan_result), \
             patch.object(telegram_runner, "send_telegram") as send, \
             patch.object(telegram_runner.time, "time", return_value=now if now is not None else 2000000):
            telegram_runner.main()
        return state_path, watch_path, poll, send

    def test_confirmed_signal_is_sent_and_public_cursor_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            state, watch, poll, send = self._run_main(
                directory,
                ([(sample_signal(), sample_metrics())], {"TESTUSDT": {"status": "تأیید LONG"}}),
            )
            journal = json.loads((Path(directory) / "trade_outcomes.json").read_text(encoding="utf-8"))
            self.assertEqual(len(journal["trades"]), 1)
            tracked = next(iter(journal["trades"].values()))
            self.assertEqual(tracked["signal"]["symbol"], "TESTUSDT")
            self.assertEqual(tracked["status"], "WAITING_ENTRY")
            send.assert_called_once()
            self.assertIn("UNSPECIFIED:TESTUSDT:LONG", state.read_text(encoding="utf-8"))
            saved = json.loads(watch.read_text(encoding="utf-8"))
            self.assertEqual(saved["public_channel_last_id"], 123)
            self.assertIn("TESTUSDT", saved["symbols"])
            poll.assert_called_once()

    def test_ingested_alerts_are_persisted_before_scanning(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "sent_signals.json"
            watch_path = Path(directory) / "watchlist.json"
            outcome_path = Path(directory) / "trade_outcomes.json"
            env = {"BOT_TOKEN": "token", "CHAT_ID": "chat", "COINALYZE_API_KEY": "key"}
            def ingest(channel, cursor, watchlist, **kwargs):
                watchlist["NEWUSDT"] = {"status": "در حال رصد", "last_alert": 2000000}
                return 123, 1
            def scan(batch, client=None):
                saved = json.loads(watch_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["public_channel_last_id"], 123)
                self.assertIn("NEWUSDT", saved["symbols"])
                return [], batch
            with patch.object(telegram_runner, "STATE_PATH", state_path), \
                 patch.object(telegram_runner, "WATCHLIST_PATH", watch_path), \
                 patch.object(telegram_runner, "OUTCOME_PATH", outcome_path), \
                 patch.dict(os.environ, env), \
                 patch.object(telegram_runner, "CoinalyzeClient", return_value=FakeClient()), \
                 patch.object(telegram_runner, "poll_public_channel_alerts", side_effect=ingest), \
                 patch.object(telegram_runner, "scan_watchlist", side_effect=scan), \
                 patch.object(telegram_runner, "send_telegram"), \
                 patch.object(telegram_runner.time, "time", return_value=2000000):
                telegram_runner.main()

    def test_duplicate_closed_candle_signal_is_not_resent(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, _, send = self._run_main(
                directory,
                ([(sample_signal(), sample_metrics())], {"TESTUSDT": {"status": "تأیید LONG"}}),
                sent_state={"UNSPECIFIED:TESTUSDT:LONG": 900000},
            )
            send.assert_not_called()

    def test_confirmed_short_is_sent_and_tracked(self):
        short = Signal("TESTUSDT", "SHORT", 100.0, 102.0, 98.0, 96.0,
                       0.8, -0.3, 0.40, 1.4, 0.0001, 82, 900000)
        with tempfile.TemporaryDirectory() as directory:
            _, _, _, send = self._run_main(
                directory,
                ([(short, sample_metrics())], {"TESTUSDT": {"status": "تأیید SHORT"}}),
            )
            journal_path = Path(directory) / "trade_outcomes.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            send.assert_called_once()
            self.assertEqual(len(journal["trades"]), 1)
            tracked = next(iter(journal["trades"].values()))
            self.assertEqual(tracked["signal"]["side"], "SHORT")
            self.assertEqual(tracked["status"], "WAITING_ENTRY")

    def test_no_signal_does_not_send_telegram_message(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, _, send = self._run_main(directory, ([], {}))
            send.assert_not_called()

    def test_missing_secrets_stops_before_public_poll(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(telegram_runner, "poll_public_channel_alerts") as poll:
            with self.assertRaises(SystemExit):
                telegram_runner.main()
            poll.assert_not_called()

    def test_scan_diagnostics_count_priority_status_and_unscanned_symbols(self):
        watchlist = {
            "AUSDT": {"oi_priority_score": 11.4},
            "BUSDT": {"oi_priority_score": 0.0},
            "CUSDT": {"oi_priority_score": 6.2},
        }
        scanned = {
            "AUSDT": {"status": "نزدیک شرایط ورود", "reason": "Coinalyze رد کرد: OI=0.1%"},
            "BUSDT": {"status": "منتظر پولبک و تأیید", "reason": "شکست LONG ثبت شد؛ منتظر پولبک"},
        }
        result = telegram_runner.summarize_scan_diagnostics(watchlist, scanned)
        self.assertEqual(result["watchlist_total"], 3)
        self.assertEqual(result["high_oi_priority"], 2)
        self.assertEqual(result["scanned"], 2)
        self.assertEqual(result["not_scanned_this_cycle"], 1)
        self.assertEqual(result["reasons"]["coinalyze_rejected"], 1)
        self.assertEqual(result["reasons"]["pending_pullback"], 1)

    def test_scan_diagnostics_do_not_call_unconfirmed_breakouts_pending_pullbacks(self):
        watchlist = {"AUSDT": {}, "BUSDT": {}}
        scanned = {
            "AUSDT": {"status": "در حال رصد", "reason": "شکست اولیه هنوز تأیید نشده"},
            "BUSDT": {"status": "منتظر پولبک و تأیید", "reason": "منتظر پولبک"},
        }
        result = telegram_runner.summarize_scan_diagnostics(watchlist, scanned)
        self.assertEqual(result["reasons"]["pending_pullback"], 1)

    def test_watchlist_batch_rotates_without_dropping_symbols(self):
        watchlist = {f"COIN{i}USDT": {"status": "در حال رصد"} for i in range(11)}
        first, cursor = telegram_runner.select_watchlist_batch(watchlist, limit=8)
        second, next_cursor = telegram_runner.select_watchlist_batch(watchlist, cursor, limit=8)
        self.assertEqual(list(first), [f"COIN{i}USDT" for i in range(8)])
        self.assertEqual(list(second), ["COIN8USDT", "COIN9USDT", "COIN10USDT", "COIN0USDT", "COIN1USDT", "COIN2USDT", "COIN3USDT", "COIN4USDT"])
        self.assertEqual(cursor, "COIN7USDT")
        self.assertEqual(next_cursor, "COIN4USDT")
        self.assertEqual(len(watchlist), 11)

    def test_pending_pullbacks_are_scanned_before_hot_oi_and_rotation(self):
        watchlist = {
            "HOT1USDT": {"oi_priority_score": 12.0},
            "HOT2USDT": {"oi_priority_score": 9.0},
            "PENDING_LATEUSDT": {"pending_setup": {"expires_at_close_time": 9000}},
            "PENDING_SOONUSDT": {"pending_setup": {"expires_at_close_time": 3000}},
            **{f"ROT{i}USDT": {} for i in range(8)},
        }
        batch, cursor = telegram_runner.select_watchlist_batch(watchlist, limit=8)
        self.assertEqual(list(batch)[:2], ["PENDING_SOONUSDT", "PENDING_LATEUSDT"])
        self.assertEqual(list(batch)[2:4], ["HOT1USDT", "HOT2USDT"])
        self.assertEqual(len(batch), 8)
        self.assertEqual(len(set(batch)), 8)

    def test_pending_pullback_uses_slot_before_oi_hot_candidate(self):
        watchlist = {
            "HOT1USDT": {"oi_priority_score": 12.0},
            "HOT2USDT": {"oi_priority_score": 9.0},
            "PENDINGUSDT": {"pending_setup": {"expires_at_close_time": 3000}},
            **{f"ROT{i}USDT": {} for i in range(8)},
        }
        batch, _ = telegram_runner.select_watchlist_batch(watchlist, limit=8)
        self.assertIn("PENDINGUSDT", batch)
        self.assertIn("HOT1USDT", batch)
        self.assertIn("HOT2USDT", batch)
        self.assertEqual(len(batch), 8)

    def test_many_pending_setups_do_not_starve_hot_or_new_symbols_and_rotate(self):
        watchlist = {
            **{
                f"PEND{i}USDT": {
                    "pending_setup": {"expires_at_close_time": 10000 + i},
                    "last_checked": 0,
                }
                for i in range(8)
            },
            "HOT1USDT": {"oi_priority_score": 12.0},
            "HOT2USDT": {"oi_priority_score": 9.0},
            **{f"NEW{i}USDT": {} for i in range(6)},
        }
        first, cursor = telegram_runner.select_watchlist_batch(watchlist, limit=8)
        self.assertEqual(len(first), 8)
        self.assertEqual(sum("pending_setup" in record for record in first.values()), 4)
        self.assertIn("HOT1USDT", first)
        self.assertIn("HOT2USDT", first)
        self.assertTrue(any(symbol.startswith("NEW") for symbol in first))

        for record in first.values():
            if "pending_setup" in record:
                record["last_checked"] = 100
        second, _ = telegram_runner.select_watchlist_batch(watchlist, cursor, limit=8)
        second_pending = {
            symbol for symbol, record in second.items()
            if "pending_setup" in record
        }
        self.assertTrue(second_pending.intersection({"PEND4USDT", "PEND5USDT", "PEND6USDT", "PEND7USDT"}))

    def test_watchlist_pruning_ignores_malformed_cached_records(self):
        kept = telegram_runner.prune_watchlist({
            "BTCUSDT": {"last_alert": "not-a-timestamp"},
            "ETHUSDT": None,
            "UVXYUSDT": {"last_alert": 999990},
        }, now=1000000)
        self.assertEqual(list(kept), ["BTCUSDT"])

    def test_twenty_four_symbols_are_all_scanned_once_across_three_batches(self):
        watchlist = {f"COIN{i:02d}USDT": {"status": "در حال رصد"} for i in range(24)}
        cursor = ""
        visited = []
        for _ in range(3):
            batch, cursor = telegram_runner.select_watchlist_batch(watchlist, cursor, limit=8)
            self.assertEqual(len(batch), 8)
            visited.extend(batch)
        self.assertEqual(len(visited), 24)
        self.assertEqual(len(set(visited)), 24)
        self.assertEqual(set(visited), set(watchlist))
        self.assertEqual(cursor, "COIN23USDT")

    def test_prunes_non_crypto_symbols_from_legacy_watchlist(self):
        kept = telegram_runner.prune_watchlist({
            "BTCUSDT": {"last_alert": 999990},
            "UVXYUSDT": {"last_alert": 999990},
            "COSTUSDT": {"last_alert": 999990},
        }, now=1000000)
        self.assertEqual(list(kept), ["BTCUSDT"])

    def test_prunes_expired_watchlist_symbols(self):
        now = 1000000
        kept = telegram_runner.prune_watchlist({
            "OLDUSDT": {"last_alert": now - telegram_runner.WATCHLIST_TTL_SECONDS - 1},
            "NEWUSDT": {"last_alert": now - 10},
        }, now=now)
        self.assertEqual(list(kept), ["NEWUSDT"])

    def test_expired_slots_are_removed_before_public_alert_ingestion(self):
        now = 2000000
        expired = {
            f"OLD{i}USDT": {
                "last_alert": now - telegram_runner.WATCHLIST_TTL_SECONDS - 10
            }
            for i in range(4)
        }
        with tempfile.TemporaryDirectory() as directory:
            _, watch_path, poll, _ = self._run_main(
                directory, ([], {}), initial_watchlist=expired, now=now
            )
            ingested_watchlist = poll.call_args.args[2]
            self.assertEqual(ingested_watchlist, {})
            self.assertEqual(json.loads(watch_path.read_text(encoding="utf-8"))["symbols"], {})


    def test_migrates_cached_source_direction_without_guessing_ambiguous_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "sent_signals.json"
            watch_path = Path(directory) / "watchlist.json"
            outcome_path = Path(directory) / "trade_outcomes.json"
            initial = {
                "BTCUSDT": {
                    "last_alert": 2000000,
                    "source_text": "🚨 Crypto Open Interest 🔥 🔥 🟢 #BTC $1.0 • OI [15m]",
                },
                "ETHUSDT": {
                    "last_alert": 2000000,
                    "source_text": "🚨 Crypto Open Interest #ETH $1.0 • OI [15m]",
                },
            }
            watch_path.write_text(json.dumps({
                "public_channel_last_id": 10, "symbols": initial
            }), encoding="utf-8")
            captured = {}
            def scan(batch, client=None):
                captured.update(batch)
                return [], batch
            env = {"BOT_TOKEN": "token", "CHAT_ID": "chat", "COINALYZE_API_KEY": "key"}
            with patch.object(telegram_runner, "STATE_PATH", state_path), \
                 patch.object(telegram_runner, "WATCHLIST_PATH", watch_path), \
                 patch.object(telegram_runner, "OUTCOME_PATH", outcome_path), \
                 patch.dict(os.environ, env), \
                 patch.object(telegram_runner, "CoinalyzeClient", return_value=FakeClient()), \
                 patch.object(telegram_runner, "poll_public_channel_alerts", return_value=(10, 0)), \
                 patch.object(telegram_runner, "scan_watchlist", side_effect=scan), \
                 patch.object(telegram_runner, "send_telegram"), \
                 patch.object(telegram_runner.time, "time", return_value=2000000):
                telegram_runner.main()
            self.assertEqual(captured["BTCUSDT"].get("source_side"), "LONG")
            self.assertNotIn("source_side", captured["ETHUSDT"])
            saved = json.loads(watch_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["symbols"]["BTCUSDT"]["source_side"], "LONG")
            self.assertNotIn("source_side", saved["symbols"]["ETHUSDT"])


if __name__ == "__main__":
    unittest.main()
