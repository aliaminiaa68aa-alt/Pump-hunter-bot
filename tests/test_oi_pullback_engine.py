import unittest
from oi_engine.pullback_engine import advance_pullback_setup


def make_snapshot(side="LONG"):
    candles = []
    for i in range(60):
        candles.append({
            "open_time": i * 900000, "open": 99.8, "high": 100.5,
            "low": 99.5, "close": 100.0, "volume": 1000,
            "quote_volume": 100000, "taker_buy_quote": 50000,
            "close_time": (i + 1) * 900000 - 1,
        })
    if side == "LONG":
        candles[-1].update({"open":101, "high":104, "low":100.8, "close":103,
                            "quote_volume":200000, "taker_buy_quote":120000})
        for c in candles[-4:-1]:
            c.update({"open":100.0, "high":100.5, "low":99.9, "close":100.2,
                      "quote_volume":100000, "taker_buy_quote":60000})
    else:
        candles[-1].update({"open":99, "high":99.2, "low":96, "close":97,
                            "quote_volume":200000, "taker_buy_quote":80000})
    return {"symbol":"TESTUSDT", "candles":candles, "oi_values":[100,100.2,101],
            "oi_timestamp":candles[-1]["close_time"], "funding_rate":0.0001}


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
