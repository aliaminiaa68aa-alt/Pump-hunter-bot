import unittest
from unittest.mock import patch

from oi_engine.signal_engine import evaluate_snapshot, scan, MarketDataError


def make_snapshot(side="LONG"):
    candles = []
    for index in range(22):
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
        "oi_timestamp": candles[-1]["close_time"],
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

    def test_no_signal_when_oi_sample_is_stale(self):
        snapshot = make_snapshot("LONG")
        snapshot["oi_timestamp"] -= 16 * 60 * 1000
        self.assertIsNone(evaluate_snapshot(snapshot))


    def test_scan_fails_if_every_market_request_fails(self):
        with patch("oi_engine.signal_engine.get_active_symbols", return_value=["AAAUSDT", "BBBUSDT"]), patch("oi_engine.signal_engine.fetch_snapshot", side_effect=MarketDataError("HTTP 451")):
            with self.assertRaisesRegex(MarketDataError, "All 2 market-data requests failed"):
                scan(max_symbols=2)

    def test_scan_keeps_valid_results_when_one_symbol_fails(self):
        good = make_snapshot("LONG")
        with patch("oi_engine.signal_engine.get_active_symbols", return_value=["BADUSDT", "GOODUSDT"]), patch("oi_engine.signal_engine.fetch_snapshot", side_effect=[MarketDataError("temporary"), good]), patch("oi_engine.signal_engine.time.sleep"):
            results = scan(max_symbols=2)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].side, "LONG")

if __name__ == "__main__":
    unittest.main()
