import unittest

from oi_engine.signal_engine import evaluate_snapshot


def make_snapshot(side="LONG"):
    candles = []
    for index in range(21):
        close = 100.0
        candles.append({
            "open_time": index * 900000,
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1000.0,
            "quote_volume": 100000.0,
            "taker_buy_quote": 50000.0,
            "close_time": (index + 1) * 900000 - 1,
        })

    last = candles[-1]
    if side == "LONG":
        last.update({
            "open": 101.0, "high": 104.0, "low": 100.8, "close": 103.0,
            "quote_volume": 200000.0, "taker_buy_quote": 120000.0,
        })
        candles[-2]["close"] = 100.0
    else:
        last.update({
            "open": 99.0, "high": 99.2, "low": 96.0, "close": 97.0,
            "quote_volume": 200000.0, "taker_buy_quote": 80000.0,
        })
        candles[-2]["close"] = 100.0

    return {
        "symbol": "TESTUSDT",
        "candles": candles,
        "oi_values": [100.0, 100.2, 101.0],
        "funding_rate": 0.0001,
    }


class SignalEngineTests(unittest.TestCase):
    def test_confirmed_long(self):
        signal = evaluate_snapshot(make_snapshot("LONG"))
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "LONG")
        self.assertLess(signal.stop_loss, signal.entry)
        self.assertGreater(signal.take_profit_2, signal.take_profit_1)

    def test_confirmed_short(self):
        signal = evaluate_snapshot(make_snapshot("SHORT"))
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "SHORT")
        self.assertGreater(signal.stop_loss, signal.entry)
        self.assertLess(signal.take_profit_2, signal.take_profit_1)

    def test_no_signal_without_oi_growth(self):
        snapshot = make_snapshot("LONG")
        snapshot["oi_values"] = [100.0, 100.0, 100.0]
        self.assertIsNone(evaluate_snapshot(snapshot))


if __name__ == "__main__":
    unittest.main()
