import unittest

from oi_engine.pullback_engine import advance_pullback_setup


def snapshot(side="LONG"):
    candles = []
    for i in range(22):
        candles.append({
            "open_time": i * 900000, "open": 99.8, "high": 100.5,
            "low": 99.5, "close": 100.0, "volume": 1000,
            "quote_volume": 100000, "taker_buy_quote": 50000,
            "close_time": (i + 1) * 900000 - 1,
        })
    last = candles[-1]
    if side == "LONG":
        last.update({"open":101, "high":104, "low":100.8, "close":103,
                     "quote_volume":200000, "taker_buy_quote":120000})
    else:
        last.update({"open":99, "high":99.2, "low":96, "close":97,
                     "quote_volume":200000, "taker_buy_quote":80000})
    candles[-2]["close"] = 100
    return {"symbol":"TESTUSDT", "candles":candles,
            "oi_values":[100,100.2,101],
            "oi_timestamp":candles[-1]["close_time"], "funding_rate":0.0001}


def add_retest(data, side):
    candles = data["candles"]
    prev_close_time = candles[-1]["close_time"]
    if side == "LONG":
        row = {"open_time":prev_close_time+1,"open":100.6,"high":101.0,"low":100.4,
               "close":100.8,"volume":1500,"quote_volume":200000,
               "taker_buy_quote":120000,"close_time":prev_close_time+900000}
    else:
        row = {"open_time":prev_close_time+1,"open":99.9,"high":100.0,"low":99.3,
               "close":99.7,"volume":1500,"quote_volume":200000,
               "taker_buy_quote":80000,"close_time":prev_close_time+900000}
    data["candles"].append(row)
    data["oi_values"] = [100.2,101.0,101.8]
    data["oi_timestamp"] = row["close_time"]
    return data


class PullbackEngineTests(unittest.TestCase):
    def test_breakout_creates_pending_setup_not_signal(self):
        data = snapshot("LONG")
        signal, pending, reason = advance_pullback_setup(data)
        self.assertIsNone(signal)
        self.assertEqual(pending["side"], "LONG")
        self.assertIn("منتظر برگشت", reason)

    def test_long_only_signals_after_retest_confirmation(self):
        data = snapshot("LONG")
        _, pending, _ = advance_pullback_setup(data)
        add_retest(data, "LONG")
        signal, next_pending, reason = advance_pullback_setup(data, pending)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "LONG")
        self.assertIsNone(next_pending)
        self.assertLess(signal.stop_loss, signal.entry)
        self.assertGreater(signal.take_profit_2, signal.take_profit_1)
        self.assertIn("تأیید شدند", reason)

    def test_short_only_signals_after_retest_confirmation(self):
        data = snapshot("SHORT")
        _, pending, _ = advance_pullback_setup(data)
        add_retest(data, "SHORT")
        signal, next_pending, _ = advance_pullback_setup(data, pending)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "SHORT")
        self.assertIsNone(next_pending)
        self.assertGreater(signal.stop_loss, signal.entry)
        self.assertLess(signal.take_profit_2, signal.take_profit_1)

    def test_setup_expires_after_four_15m_candles(self):
        data = snapshot("LONG")
        _, pending, _ = advance_pullback_setup(data)
        data["candles"].append(dict(data["candles"][-1],
            open_time=data["candles"][-1]["close_time"]+1,
            close_time=pending["expires_at_close_time"]+1))
        signal, next_pending, reason = advance_pullback_setup(data, pending)
        self.assertIsNone(signal)
        self.assertIsNone(next_pending)
        self.assertIn("منقضی", reason)


if __name__ == "__main__":
    unittest.main()
