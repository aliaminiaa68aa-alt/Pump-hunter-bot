import unittest

import Bot


class BosBreakDetectionTests(unittest.TestCase):
    def candle(self, t, o, h, l, c):
        return {"t": str(t), "o": str(o), "h": str(h), "l": str(l), "c": str(c)}

    def test_detects_bos_on_intermediate_closed_candle(self):
        candles = [
            self.candle(0, 100, 101, 99, 100),
            self.candle(1, 100, 101, 99, 100),
            self.candle(2, 100, 102, 99, 101),
            self.candle(3, 101, 102, 100, 101),
            self.candle(4, 101, 103, 100, 102),
            self.candle(5, 102, 102.5, 101, 101.5),
            self.candle(6, 101.5, 102, 101, 101.7),
        ]
        result = Bot.find_closed_bos_break(candles, 3)
        self.assertEqual(result["bos_level"], 102.0)
        self.assertEqual(result["bos_time"], "4")
        self.assertEqual(result["bos_close"], 102.0)

    def test_ignores_bearish_break_above_high(self):
        candles = [
            self.candle(0, 100, 101, 99, 100),
            self.candle(1, 100, 101, 99, 100),
            self.candle(2, 100, 102, 99, 101),
            self.candle(3, 103, 104, 102, 102.5),
        ]
        self.assertIsNone(Bot.find_closed_bos_break(candles, 3))

    def test_requires_three_prior_candles(self):
        candles = [self.candle(0, 100, 101, 99, 100), self.candle(1, 100, 101, 99, 100)]
        self.assertIsNone(Bot.find_closed_bos_break(candles, 1))


if __name__ == "__main__":
    unittest.main()