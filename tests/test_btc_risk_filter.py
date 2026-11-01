import unittest
from unittest.mock import patch

from Bot import btc_market_filter


def candle(open_price, close_price):
    return {
        "t": "1",
        "o": str(open_price),
        "h": str(max(open_price, close_price)),
        "l": str(min(open_price, close_price)),
        "c": str(close_price),
    }


class BtcRiskFilterTests(unittest.TestCase):
    @patch("Bot.closed_candles", return_value=[])
    def test_missing_closed_candles_pauses_final_entries(self, _mock):
        allowed, change = btc_market_filter()
        self.assertFalse(allowed)
        self.assertIsNone(change)

    @patch("Bot.closed_candles", side_effect=RuntimeError("temporary API failure"))
    def test_data_error_pauses_final_entries_without_crashing(self, _mock):
        allowed, change = btc_market_filter()
        self.assertFalse(allowed)
        self.assertIsNone(change)

    @patch("Bot.closed_candles", return_value=[candle(100, 100), candle(100, 97.9)])
    def test_severe_btc_selloff_pauses_final_entries(self, _mock):
        allowed, change = btc_market_filter()
        self.assertFalse(allowed)
        self.assertLessEqual(change, -2.0)

    @patch("Bot.closed_candles", return_value=[candle(100, 100), candle(100, 99.8)])
    def test_routine_btc_pullback_does_not_pause_final_entries(self, _mock):
        allowed, change = btc_market_filter()
        self.assertTrue(allowed)
        self.assertAlmostEqual(change, -0.2, places=6)

    @patch("Bot.closed_candles", return_value=[candle(100, 100), candle(0, 99)])
    def test_invalid_btc_prices_pause_final_entries(self, _mock):
        allowed, change = btc_market_filter()
        self.assertFalse(allowed)
        self.assertIsNone(change)


if __name__ == "__main__":
    unittest.main()
