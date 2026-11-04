import math
import unittest

from research.research_features import (
    candle_volume_pressure,
    ema_series,
    ema20_stop_candidate,
)


class ResearchFeatureTests(unittest.TestCase):
    def test_ema_uses_sma_seed_and_closed_values(self):
        result = ema_series([1, 2, 3, 4, 5], period=3)
        self.assertEqual(result[:2], [None, None])
        self.assertAlmostEqual(result[2], 2.0)
        self.assertAlmostEqual(result[3], 3.0)
        self.assertAlmostEqual(result[4], 4.0)

    def test_red_volume_and_taker_flow_identify_sell_pressure(self):
        candles = [
            {"open": 10, "close": 11, "quote_volume": 100, "taker_buy_quote": 60},
            {"open": 11, "close": 10, "quote_volume": 250, "taker_buy_quote": 70},
            {"open": 10, "close": 9, "quote_volume": 200, "taker_buy_quote": 60},
        ]
        result = candle_volume_pressure(candles)
        self.assertEqual(result["pressure"], "SELL")
        self.assertGreater(result["red_quote_volume"], result["green_quote_volume"])
        self.assertGreater(result["taker_sell_quote"], result["taker_buy_quote"])

    def test_single_red_candle_without_flow_confirmation_is_mixed(self):
        candles = [
            {"open": 10, "close": 11, "quote_volume": 100, "taker_buy_quote": 70},
            {"open": 11, "close": 10, "quote_volume": 130, "taker_buy_quote": 90},
        ]
        self.assertEqual(candle_volume_pressure(candles)["pressure"], "MIXED")

    def test_invalid_taker_volume_is_rejected(self):
        with self.assertRaises(ValueError):
            candle_volume_pressure([
                {"open": 1, "close": 2, "quote_volume": 10, "taker_buy_quote": 11}
            ])

    def test_ema_stop_combines_ema_and_swing_for_both_sides(self):
        long_stop = ema20_stop_candidate("LONG", 100, 98, 2, 97, 101)
        short_stop = ema20_stop_candidate("SHORT", 100, 102, 2, 99, 103)
        self.assertAlmostEqual(long_stop, 96.7)
        self.assertAlmostEqual(short_stop, 103.3)

    def test_bad_ema_period_is_rejected(self):
        with self.assertRaises(ValueError):
            ema_series([1, 2], 0)


if __name__ == "__main__":
    unittest.main()
