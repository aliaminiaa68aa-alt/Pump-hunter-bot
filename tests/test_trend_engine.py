import unittest
from unittest.mock import patch

from oi_engine.trend_engine import _structure_direction, confirm_trend_15m, analyze_trend_timeframe


def make_candles(highs, lows, closes=None):
    closes = closes or [(high + low) / 2 for high, low in zip(highs, lows)]
    return [
        {"open": close, "high": high, "low": low, "close": close}
        for high, low, close in zip(highs, lows, closes)
    ]


class TrendStructureTests(unittest.TestCase):
    def test_higher_highs_and_higher_lows_are_bullish(self):
        highs = [10, 9, 11, 10, 12, 11, 13, 12, 14, 13, 15, 14]
        lows = [8, 7, 9, 8, 10, 9, 11, 10, 12, 11, 13, 12]
        candles = make_candles(highs, lows)
        self.assertEqual(_structure_direction(candles), "LONG")

    def test_lower_highs_and_lower_lows_are_bearish(self):
        highs = [15, 16, 14, 15, 13, 14, 12, 13, 11, 12, 10, 11]
        lows = [13, 14, 12, 13, 11, 12, 10, 11, 9, 10, 8, 9]
        candles = make_candles(highs, lows)
        self.assertEqual(_structure_direction(candles), "SHORT")

    def test_mixed_structure_is_neutral(self):
        highs = [10, 9, 11, 10, 12, 11, 11, 10, 13, 12, 12, 11]
        lows = [8, 7, 9, 8, 10, 9, 8, 7, 10, 9, 8, 7]
        self.assertEqual(_structure_direction(make_candles(highs, lows)), "NEUTRAL")

    def test_15m_confirmation_rejects_neutral_direction(self):
        self.assertFalse(confirm_trend_15m([], "NEUTRAL"))

    def test_only_supported_higher_timeframes_are_accepted(self):
        with self.assertRaises(ValueError):
            analyze_trend_timeframe("ABCUSDT", "15m")

    def test_timeframe_analysis_returns_named_direction_context(self):
        candles = [
            {"open": 100 + i, "high": 102 + i, "low": 99 + i, "close": 101 + i}
            for i in range(240)
        ]
        with patch("oi_engine.trend_engine._fetch_timeframe", return_value=candles):
            result = analyze_trend_timeframe("ABCUSDT", "4h")
        self.assertEqual(result["timeframe"], "4h")
        self.assertIn(result["direction"], {"LONG", "SHORT", "NEUTRAL"})
        self.assertEqual(result["close"], candles[-1]["close"])


if __name__ == "__main__":
    unittest.main()
