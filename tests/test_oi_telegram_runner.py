import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oi_engine.signal_engine import Signal
from oi_engine import telegram_runner


def sample_signal():
    return Signal("TESTUSDT","LONG",100.0,98.0,102.0,104.0,
                  0.8,0.3,0.56,1.4,0.0001,82,900000)


def sample_metrics():
    return {
        "oi_change_15m_window_pct": 0.4, "taker_buy_ratio": 0.60,
        "funding_rate": 0.0001, "predicted_funding_rate": 0.0002,
        "long_short_ratio": 1.2, "long_liquidations": 1200,
        "short_liquidations": 900,
        "data_quality": {"complete": True},
    }


class TelegramRunnerTests(unittest.TestCase):
    def test_duplicate_closed_candle_signal_is_not_resent(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "sent_signals.json"
            watch_path = Path(directory) / "watchlist.json"
            state_path.write_text('{"TESTUSDT:LONG": 900000}', encoding="utf-8")
            with patch.object(telegram_runner, "STATE_PATH", state_path), \
                 patch.object(telegram_runner, "WATCHLIST_PATH", watch_path), \
                 patch.dict(os.environ, {"BOT_TOKEN":"token","CHAT_ID":"chat","COINALYZE_API_KEY":"key"}), \
                 patch.object(telegram_runner, "poll_forwarded_alerts", return_value=(1,0)), \
                 patch.object(telegram_runner, "scan_watchlist", return_value=([(sample_signal(),sample_metrics())], {"TESTUSDT":{"status":"تأیید LONG"}})), \
                 patch.object(telegram_runner, "send_telegram") as send:
                telegram_runner.main()
            send.assert_not_called()

    def test_confirmed_signal_is_sent_and_watchlist_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "sent_signals.json"
            watch_path = Path(directory) / "watchlist.json"
            with patch.object(telegram_runner, "STATE_PATH", state_path), \
                 patch.object(telegram_runner, "WATCHLIST_PATH", watch_path), \
                 patch.dict(os.environ, {"BOT_TOKEN":"token","CHAT_ID":"chat","COINALYZE_API_KEY":"key"}), \
                 patch.object(telegram_runner, "poll_forwarded_alerts", return_value=(2,1)), \
                 patch.object(telegram_runner, "scan_watchlist", return_value=([(sample_signal(),sample_metrics())], {"TESTUSDT":{"status":"تأیید LONG"}})), \
                 patch.object(telegram_runner, "send_telegram") as send:
                telegram_runner.main()
            send.assert_called_once()
            self.assertIn("TESTUSDT:LONG", state_path.read_text(encoding="utf-8"))
            saved = json.loads(watch_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["telegram_update_offset"],2)
            self.assertIn("TESTUSDT",saved["symbols"])

    def test_missing_secrets_stops_before_polling(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(telegram_runner, "poll_forwarded_alerts") as poll:
            with self.assertRaises(SystemExit):
                telegram_runner.main()
            poll.assert_not_called()

    def test_prunes_expired_watchlist_symbols(self):
        now=1000000
        kept=telegram_runner.prune_watchlist({
            "OLDUSDT":{"last_alert":now-telegram_runner.WATCHLIST_TTL_SECONDS-1},
            "NEWUSDT":{"last_alert":now-10},
        },now=now)
        self.assertEqual(list(kept),["NEWUSDT"])


if __name__ == "__main__":
    unittest.main()
