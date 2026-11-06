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
    def test_missing_active_symbol_fails_closed(self, get):
        response = Mock()
        response.json.return_value = [{"symbol": "BTCUSDT", "price": "67000"}]
        get.return_value = response

        with self.assertRaisesRegex(RuntimeError, "ETHUSDT"):
            fetch_binance_last_prices({"BTCUSDT", "ETHUSDT"})

    def test_empty_symbol_set_does_not_call_network(self):
        with patch("oi_engine.service.requests.get") as get:
            self.assertEqual(fetch_binance_last_prices(set()), {})
            get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
