import unittest

import Bot


def candle(t, op, high, low, close, turnover=100.0):
    return {
        "t": str(t), "o": str(op), "h": str(high), "l": str(low),
        "c": str(close), "sum": str(turnover),
    }


class PrePumpEntryTests(unittest.TestCase):
    def make_valid_setup(self):
        one_minute = []
        # Baseline candles: quiet price action and normal turnover.
        for i in range(20):
            base = 100.0 + (i % 3) * 0.01
            one_minute.append(candle(i, base, base + 0.08, base - 0.10, base + 0.02, 100))
        # Five prior candles define the local breakout high and nearby stop.
        for i in range(5):
            base = 100.02 + i * 0.005
            one_minute.append(candle(20 + i, base, 100.15, 99.92, 100.08, 110))
        one_minute.append(candle(25, 100.10, 100.48, 100.08, 100.40, 220))

        five_minute = []
        for i in range(13):
            five_minute.append(candle(100 + i, 100.0, 100.25, 99.85, 100.05, 100))
        for i in range(6):
            five_minute.append(candle(113 + i, 100.02, 100.35, 99.90, 100.20, 130))
        return one_minute, five_minute

    def test_returns_entry_stop_and_targets_for_valid_breakout(self):
        one_minute, five_minute = self.make_valid_setup()
        result = Bot.detect_prepump_entry(one_minute, five_minute)
        self.assertIsNotNone(result)
        self.assertGreater(result["entry"], result["breakout_level"])
        self.assertLess(result["stop"], result["entry"])
        self.assertGreater(result["tp1"], result["entry"])
        self.assertGreater(result["tp2"], result["tp1"])
        self.assertGreaterEqual(result["risk_pct"], 0.20)
        self.assertLessEqual(result["risk_pct"], 1.50)

    def test_rejects_candle_without_breakout(self):
        one_minute, five_minute = self.make_valid_setup()
        one_minute[-1]["c"] = "100.10"
        one_minute[-1]["h"] = "100.20"
        self.assertIsNone(Bot.detect_prepump_entry(one_minute, five_minute))

    def test_rejects_excessive_price_extension(self):
        one_minute, five_minute = self.make_valid_setup()
        one_minute[-1]["c"] = "103.00"
        one_minute[-1]["h"] = "103.10"
        one_minute[-1]["sum"] = "1000"
        self.assertIsNone(Bot.detect_prepump_entry(one_minute, five_minute))


if __name__ == "__main__":
    unittest.main()
