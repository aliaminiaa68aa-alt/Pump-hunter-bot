import unittest
from unittest.mock import patch

import Bot


class GateIOPublicDerivativesTests(unittest.TestCase):
    @patch("Bot.get_json")
    def test_calculates_oi_change_and_reads_contract_funding(self, mock_get_json):
        mock_get_json.side_effect = [
            [
                {"time": 1000, "open_interest": "100"},
                {"time": 1300, "open_interest": "100.4"},
            ],
            {"funding_rate": "-0.0002"},
        ]
        result = Bot.gateio_public_derivatives_check("ABC_USDT")
        self.assertTrue(result["valid"])
        self.assertAlmostEqual(result["oi_change"], 0.4)
        self.assertAlmostEqual(result["funding_rate"], -0.0002)
        self.assertEqual(result["source"], "Gate.io public")
        self.assertEqual(mock_get_json.call_count, 2)

    @patch("Bot.get_json")
    def test_rejects_insufficient_oi_history(self, mock_get_json):
        mock_get_json.return_value = [{"time": 1000, "open_interest": "100"}]
        result = Bot.gateio_public_derivatives_check("ABC_USDT")
        self.assertFalse(result["valid"])
        self.assertIn("OI", result["reason"])

    @patch("Bot.get_json")
    def test_rejects_missing_funding(self, mock_get_json):
        mock_get_json.side_effect = [
            [
                {"time": 1000, "open_interest": "100"},
                {"time": 1300, "open_interest": "100.4"},
            ],
            {},
        ]
        result = Bot.gateio_public_derivatives_check("ABC_USDT")
        self.assertFalse(result["valid"])
        self.assertIn("Funding", result["reason"])


if __name__ == "__main__":
    unittest.main()
