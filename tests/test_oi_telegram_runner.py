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


class TelegramRunnerTests(unittest.TestCase):
    def _run_main(self, directory, scan_result, public_result=(123, 1), sent_state=None):
        state_path = Path(directory) / "sent_signals.json"
        watch_path = Path(directory) / "watchlist.json"
        if sent_state is not None:
            state_path.write_text(json.dumps(sent_state), encoding="utf-8")
        env = {"BOT_TOKEN": "token", "CHAT_ID": "chat", "COINALYZE_API_KEY": "key"}
        with patch.object(telegram_runner, "STATE_PATH", state_path), \
             patch.object(telegram_runner, "WATCHLIST_PATH", watch_path), \
             patch.dict(os.environ, env), \
             patch.object(telegram_runner, "CoinalyzeClient", return_value=FakeClient()), \
             patch.object(telegram_runner, "poll_public_channel_alerts", return_value=public_result) as poll, \
             patch.object(telegram_runner, "scan_watchlist", return_value=scan_result), \
             patch.object(telegram_runner, "send_telegram") as send:
            telegram_runner.main()
        return state_path, watch_path, poll, send

    def test_confirmed_signal_is_sent_and_public_cursor_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            state, watch, poll, send = self._run_main(
                directory,
                ([(sample_signal(), sample_metrics())], {"TESTUSDT": {"status": "تأیید LONG"}}),
            )
            send.assert_called_once()
            self.assertIn("TESTUSDT:LONG", state.read_text(encoding="utf-8"))
            saved = json.loads(watch.read_text(encoding="utf-8"))
            self.assertEqual(saved["public_channel_last_id"], 123)
            self.assertIn("TESTUSDT", saved["symbols"])
            poll.assert_called_once()

    def test_duplicate_closed_candle_signal_is_not_resent(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, _, send = self._run_main(
                directory,
                ([(sample_signal(), sample_metrics())], {"TESTUSDT": {"status": "تأیید LONG"}}),
                sent_state={"TESTUSDT:LONG": 900000},
            )
            send.assert_not_called()

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

    def test_prunes_expired_watchlist_symbols(self):
        now = 1000000
        kept = telegram_runner.prune_watchlist({
            "OLDUSDT": {"last_alert": now - telegram_runner.WATCHLIST_TTL_SECONDS - 1},
            "NEWUSDT": {"last_alert": now - 10},
        }, now=now)
        self.assertEqual(list(kept), ["NEWUSDT"])


if __name__ == "__main__":
    unittest.main()
