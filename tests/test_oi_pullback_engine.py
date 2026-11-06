import unittest
from oi_engine.pullback_engine import advance_pullback_setup


def make_snapshot(side="LONG"):
    candles = []
    for i in range(60):
        if side == "LONG":
            close = 100.0 + i * 0.05
        else:
            close = 103.0 - i * 0.05
        candles.append({
            "open_time": i * 900000, "open": close - 0.02,
            "high": close + 0.35, "low": close - 0.35, "close": close,
            "volume": 1000, "quote_volume": 100000,
            "taker_buy_quote": 50000,
            "close_time": (i + 1) * 900000 - 1,
        })
    if side == "LONG":
        candles[-2]["high"] = 103.90
        candles[-1].update({
            "open": 103.75, "high": 104.20, "low": 103.65, "close": 104.05,
            "quote_volume": 200000, "taker_buy_quote": 140000,
        })
    else:
        candles[-2]["low"] = 99.10
        candles[-1].update({
            "open": 99.25, "high": 99.35, "low": 98.80, "close": 98.90,
            "quote_volume": 200000, "taker_buy_quote": 60000,
        })
    return {
        "symbol": "TESTUSDT", "candles": candles,
        "oi_values": [100, 100.3, 102.0],
        "oi_timestamp": candles[-1]["close_time"], "funding_rate": 0.0001,
    }

class PullbackEngineTests(unittest.TestCase):
    def test_long_breakout_sends_limit_before_retest(self):
        signal, pending, reason = advance_pullback_setup(make_snapshot("LONG"))
        self.assertIsNotNone(signal, reason)
        self.assertEqual(signal.side, "LONG")
        self.assertEqual(signal.execution_mode, "LIMIT")
        self.assertEqual(signal.entry, pending["level"])
        self.assertEqual(signal.setup_level, pending["level"])
        self.assertLess(signal.stop_loss, signal.entry)
        self.assertGreater(signal.take_profit_1, signal.entry)
        self.assertGreater(signal.take_profit_2, signal.take_profit_1)
        self.assertTrue(pending["entry_signal_sent"])
        self.assertIn("پیش از پولبک", reason)

    def test_short_breakout_sends_limit_before_retest(self):
        signal, pending, reason = advance_pullback_setup(make_snapshot("SHORT"))
        self.assertIsNotNone(signal, reason)
        self.assertEqual(signal.side, "SHORT")
        self.assertEqual(signal.execution_mode, "LIMIT")
        self.assertEqual(signal.entry, pending["level"])
        self.assertGreater(signal.stop_loss, signal.entry)
        self.assertLess(signal.take_profit_1, signal.entry)

    def test_channel_direction_mismatch_blocks_breakout(self):
        signal, pending, reason = advance_pullback_setup(make_snapshot("LONG"), required_side="SHORT")
        self.assertIsNone(signal)
        self.assertIsNone(pending)
        self.assertIn("با جهت کانال", reason)


if __name__ == "__main__":
    unittest.main()
