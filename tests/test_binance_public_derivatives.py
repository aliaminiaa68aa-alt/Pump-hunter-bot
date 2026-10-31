import unittest
from unittest.mock import Mock, patch

import Bot


class BinancePublicDerivativesTests(unittest.TestCase):
    def response(self, payload):
        response = Mock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return response

    @patch.object(Bot.SESSION, "get")
    def test_calculates_oi_change_and_reads_funding(self, mock_get):
        mock_get.side_effect = [
            self.response([
                {"timestamp": 1000, "sumOpenInterest": "100"},
                {"timestamp": 2000, "sumOpenInterest": "100.6"},
            ]),
            self.response({"lastFundingRate": "-0.0001"}),
        ]
        result = Bot.binance_public_derivatives_check("ABC_USDT")
        self.assertTrue(result["valid"])
        self.assertAlmostEqual(result["oi_change"], 0.6)
        self.assertAlmostEqual(result["funding_rate"], -0.0001)
        self.assertEqual(result["source"], "Binance Futures public")
        self.assertEqual(mock_get.call_count, 2)

    @patch.object(Bot.SESSION, "get")
    def test_returns_invalid_when_oi_history_is_insufficient(self, mock_get):
        mock_get.return_value = self.response([{"timestamp": 1000, "sumOpenInterest": "100"}])
        result = Bot.binance_public_derivatives_check("ABC_USDT")
        self.assertFalse(result["valid"])
        self.assertIn("OI", result["reason"])

    @patch.object(Bot.SESSION, "get")
    def test_handles_provider_error_without_crashing(self, mock_get):
        mock_get.side_effect = RuntimeError("blocked")
        result = Bot.binance_public_derivatives_check("ABC_USDT")
        self.assertFalse(result["valid"])
        self.assertIn("Binance", result["reason"])


if __name__ == "__main__":
    unittest.main()
