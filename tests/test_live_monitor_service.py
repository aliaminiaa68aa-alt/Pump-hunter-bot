import unittest
from unittest.mock import Mock, patch

from oi_engine.service import fetch_binance_last_prices


class LiveMonitorServiceTests(unittest.TestCase):
    @patch("oi_engine.service.requests.get")
    def test_fetches_only_requested_binance_contract_prices(self, get):
        response = Mock()
        response.json.return_value = [
            {"symbol": "BTCUSDT", "price": "67000.5"},
            {"symbol": "ETHUSDT", "price": "2500.25"},
            {"symbol": "OTHERUSDT", "price": "1.0"},
        ]
        get.return_value = response

        prices = fetch_binance_last_prices({"BTCUSDT", "ETHUSDT"})

        self.assertEqual(prices, {"BTCUSDT": 67000.5, "ETHUSDT": 2500.25})
        get.assert_called_once()
        response.raise_for_status.assert_called_once()

    @patch("oi_engine.service.requests.get")
    def test_partial_snapshot_keeps_available_symbols(self, get):
        response = Mock()
        response.json.return_value = [{"symbol": "BTCUSDT", "price": "67000"}]
        get.return_value = response

        prices = fetch_binance_last_prices({"BTCUSDT", "ETHUSDT"})

        self.assertEqual(prices, {"BTCUSDT": 67000.0})

    @patch("oi_engine.service.requests.get")
    def test_empty_usable_snapshot_fails_closed(self, get):
        response = Mock()
        response.json.return_value = [{"symbol": "OTHERUSDT", "price": "1.0"}]
        get.return_value = response

        with self.assertRaisesRegex(RuntimeError, "no usable active symbols"):
            fetch_binance_last_prices({"BTCUSDT", "ETHUSDT"})

    def test_empty_symbol_set_does_not_call_network(self):
        with patch("oi_engine.service.requests.get") as get:
            self.assertEqual(fetch_binance_last_prices(set()), {})
            get.assert_not_called()


class LiveOutcomePipelineTests(unittest.TestCase):
    def _trade_journal(self):
        from oi_engine.outcome_tracker import make_trade_record

        signal = {
            "symbol": "TESTUSDT", "side": "LONG", "entry": 100.0,
            "stop_loss": 98.0, "take_profit_1": 102.0,
            "take_profit_2": 104.0, "candle_close_time": 900,
        }
        record = make_trade_record(signal, {"engine": "BOS"}, 1_000)
        record["telegram_message_id"] = 700
        return {"trades": {record["trade_id"]: record}}, record

    def _run_price_sequence(self, journal, snapshots):
        from oi_engine import service, telegram_runner

        with patch.dict("os.environ", {"BOT_TOKEN": "token", "CHAT_ID": "chat"}):
            with patch.object(service, "load_outcome_journal", return_value=journal):
                with patch.object(service, "save_outcome_journal"):
                    with patch.object(
                        service, "fetch_binance_last_prices", side_effect=snapshots
                    ):
                        with patch.object(telegram_runner, "save_outcome_journal"):
                            with patch.object(
                                telegram_runner, "send_telegram",
                                side_effect=[801, 802, 803, 804],
                            ) as send:
                                results = [
                                    service.monitor_live_outcomes_once()
                                    for _ in snapshots
                                ]
        return send, results

    def test_live_service_sends_entry_tp1_and_tp2_notifications(self):
        journal, record = self._trade_journal()
        send, results = self._run_price_sequence(
            journal,
            [
                {"TESTUSDT": 101.0},  # baseline
                {"TESTUSDT": 99.9},   # ENTRY
                {"TESTUSDT": 102.1},  # TP1
                {"TESTUSDT": 104.1},  # TP2
            ],
        )

        self.assertEqual(
            [event["event"] for event in record["events"]],
            ["ENTRY", "TP1", "TP2"],
        )
        self.assertEqual(record["status"], "CLOSED_TP2")
        self.assertEqual(send.call_count, 3)
        messages = [call.args[2] for call in send.call_args_list]
        self.assertIn("ورود فعال شد", messages[0])
        self.assertIn("تارگت اول خورد", messages[1])
        self.assertIn("تارگت دوم خورد", messages[2])
        self.assertTrue(all(
            call.kwargs["reply_to_message_id"] == 700
            for call in send.call_args_list
        ))
        self.assertTrue(all(
            state["status"] == "sent"
            for state in record["notification_state"].values()
        ))
        self.assertEqual(results[-1]["events"], 1)

    def test_live_service_sends_entry_and_stop_loss_notifications(self):
        journal, record = self._trade_journal()
        send, _ = self._run_price_sequence(
            journal,
            [
                {"TESTUSDT": 101.0},  # baseline
                {"TESTUSDT": 99.9},   # ENTRY
                {"TESTUSDT": 97.9},   # SL
            ],
        )

        self.assertEqual(
            [event["event"] for event in record["events"]],
            ["ENTRY", "SL"],
        )
        self.assertEqual(record["status"], "CLOSED_SL")
        self.assertEqual(send.call_count, 2)
        messages = [call.args[2] for call in send.call_args_list]
        self.assertIn("ورود فعال شد", messages[0])
        self.assertIn("حد ضرر خورد", messages[1])
        self.assertTrue(all(
            state["status"] == "sent"
            for state in record["notification_state"].values()
        ))


if __name__ == "__main__":
    unittest.main()
