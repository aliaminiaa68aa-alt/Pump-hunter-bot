import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oi_engine.signal_engine import Signal
from oi_engine import telegram_runner


def sample_signal():
    return Signal(
        symbol="TESTUSDT",
        side="LONG",
        entry=100.0,
        stop_loss=98.0,
        take_profit_1=102.0,
        take_profit_2=104.0,
        oi_change_pct=0.8,
        price_change_pct=0.3,
        taker_buy_ratio=0.56,
        volume_ratio=1.4,
        funding_rate=0.0001,
        confidence=82,
        candle_close_time=900000,
    )


class TelegramRunnerTests(unittest.TestCase):
    def test_duplicate_closed_candle_signal_is_not_resent(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "sent_signals.json"
            state_path.write_text('{"TESTUSDT:LONG": 900000}', encoding="utf-8")
            with patch.object(telegram_runner, "STATE_PATH", state_path), \
                 patch.dict(os.environ, {"BOT_TOKEN": "token", "CHAT_ID": "chat"}), \
                 patch.object(telegram_runner, "scan", return_value=[sample_signal()]), \
                 patch.object(telegram_runner, "send_telegram") as send:
                telegram_runner.main()
            send.assert_not_called()

    def test_new_signal_is_sent_once_and_state_is_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "sent_signals.json"
            with patch.object(telegram_runner, "STATE_PATH", state_path), \
                 patch.dict(os.environ, {"BOT_TOKEN": "token", "CHAT_ID": "chat"}), \
                 patch.object(telegram_runner, "scan", return_value=[sample_signal()]), \
                 patch.object(telegram_runner, "send_telegram") as send:
                telegram_runner.main()
            send.assert_called_once()
            self.assertIn("TESTUSDT:LONG", state_path.read_text(encoding="utf-8"))

    def test_missing_telegram_secrets_stops_before_scanning(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(telegram_runner, "scan") as scan:
            with self.assertRaises(SystemExit):
                telegram_runner.main()
            scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
