import unittest

from Bot import detect_fib_liquidity_reversal


def candle(t, o, h, l, c):
    return {"t": t, "o": str(o), "h": str(h), "l": str(l), "c": str(c)}


class FibLiquidityReversalTests(unittest.TestCase):
    def setUp(self):
        self.candidate = {
            "start_time": 1000,
            "end_time": 1900,
            "low": 100.0,
            "high": 120.0,
            "fib50": 110.0,
            "fib66": 106.8,
        }

    def test_confirms_bullish_sweep_and_reclaim(self):
        candles = [
            candle(1000, 100, 102, 99, 101),
            candle(1900, 101, 120, 100, 118),
            candle(2800, 110, 112, 108, 109),
            candle(3700, 109, 111, 107.5, 108),
            candle(4600, 107, 110, 106.5, 108),
        ]
        result = detect_fib_liquidity_reversal(candles, self.candidate)
        self.assertIsNotNone(result)
        self.assertEqual(result["reclaim_level_name"], "0.66")
        self.assertEqual(result["sweep_low"], 106.5)
        self.assertGreater(result["entry_price"], self.candidate["fib66"])

    def test_rejects_bullish_candle_without_liquidity_sweep(self):
        candles = [
            candle(1000, 100, 102, 99, 101),
            candle(1900, 101, 120, 100, 118),
            candle(2800, 110, 112, 108, 109),
            candle(3700, 109, 111, 107.5, 108),
            candle(4600, 107, 110, 108.5, 109),
        ]
        self.assertIsNone(detect_fib_liquidity_reversal(candles, self.candidate))

    def test_requires_three_closed_candles_after_impulse(self):
        candles = [
            candle(1000, 100, 102, 99, 101),
            candle(1900, 101, 120, 100, 118),
            candle(2800, 110, 112, 108, 109),
            candle(3700, 109, 111, 107.5, 108),
        ]
        self.assertIsNone(detect_fib_liquidity_reversal(candles, self.candidate))


if __name__ == "__main__":
    unittest.main()
