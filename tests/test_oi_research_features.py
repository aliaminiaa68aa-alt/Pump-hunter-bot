import unittest

from oi_engine.research_features import candle_volume_pressure, ema, stop_candidates


class ResearchFeatureTests(unittest.TestCase):
    def test_ema_uses_recursive_closed_series(self):
        self.assertAlmostEqual(ema([100.0] * 20, 20), 100.0)

    def test_red_volume_dominance_requires_taker_sell_confirmation(self):
        candles = [
            {"open": 100, "close": 101, "quote_volume": 100, "taker_buy_quote": 60},
            {"open": 101, "close": 100, "quote_volume": 180, "taker_buy_quote": 70},
        ]
        result = candle_volume_pressure(candles, window=2)
        self.assertEqual(result["pressure"], "SELL")
        self.assertGreater(result["red_to_green_volume_ratio"], 1.25)

    def test_large_red_candle_alone_is_not_sell_pressure(self):
        candles = [
            {"open": 100, "close": 101, "quote_volume": 100, "taker_buy_quote": 60},
            {"open": 101, "close": 100, "quote_volume": 180, "taker_buy_quote": 110},
        ]
        self.assertEqual(candle_volume_pressure(candles, 2)["pressure"], "MIXED")

    def test_green_volume_and_taker_buy_confirm_buy_pressure(self):
        candles = [
            {"open": 100, "close": 101, "quote_volume": 180, "taker_buy_quote": 120},
            {"open": 101, "close": 100, "quote_volume": 100, "taker_buy_quote": 40},
        ]
        self.assertEqual(candle_volume_pressure(candles, 2)["pressure"], "BUY")

    def test_stop_candidates_are_directional_and_capped(self):
        candles = []
        for i in range(24):
            close = 100 + i * 0.2
            candles.append({"open": close - 0.1, "high": close + 0.4,
                            "low": close - 0.4, "close": close})
        long = stop_candidates(candles, "LONG", 105.0, 1.0)
        short = stop_candidates(candles, "SHORT", 105.0, 1.0)
        self.assertLess(long["baseline_atr_stop"], 105.0)
        self.assertGreater(short["baseline_atr_stop"], 105.0)
        self.assertIsNotNone(long["ema20_atr_structure_stop"])
        self.assertIsNotNone(short["ema20_atr_structure_stop"])

    def test_invalid_taker_volume_is_rejected(self):
        candles = [{"open": 1, "close": 2, "quote_volume": 10,
                    "taker_buy_quote": 11}]
        with self.assertRaises(ValueError):
            candle_volume_pressure(candles)


if __name__ == "__main__":
    unittest.main()
