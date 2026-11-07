import unittest
import time
from unittest.mock import patch, Mock

from oi_engine import signal_engine
from oi_engine.market_data import fetch_bybit_rows, bybit_rows_to_snapshot, fetch_gateio_rows
from oi_engine.outcome_tracker import _fetch_klines, _fetch_1m_klines


def bybit_payload(interval_ms=900000, count=60):
    now = int(time.time() * 1000)
    rows = []
    for index in range(count):
        start = now - (count - index) * interval_ms
        rows.append([str(start), "100", "102", "99", "101", "12", "1212"])
    return {"retCode": 0, "result": {"list": list(reversed(rows))}}


class BybitPriceFallbackTests(unittest.TestCase):
    @patch("oi_engine.market_data.requests.get")
    def test_bybit_rows_are_sorted_and_converted_to_snapshot(self, get):
        response = Mock()
        response.json.return_value = bybit_payload()
        response.raise_for_status.return_value = None
        get.return_value = response
        rows = fetch_bybit_rows("ABCUSDT", "15", limit=60)
        self.assertEqual(len(rows), 60)
        self.assertLess(int(rows[0][0]), int(rows[-1][0]))
        snapshot = bybit_rows_to_snapshot(rows, "ABCUSDT")
        self.assertEqual(snapshot["price_source"], "Bybit linear perpetual")
        self.assertEqual(snapshot["candles"][-1]["quote_volume"], 1212.0)


    @patch("oi_engine.market_data.requests.get")
    def test_gateio_candles_are_normalized(self, get):
        response = Mock()
        now = int(time.time())
        response.json.return_value = [
            {"t": now - 1800, "v": "100", "sum": "1200",
             "c": "101", "h": "102", "l": "99", "o": "100"}
        ]
        response.raise_for_status.return_value = None
        get.return_value = response
        rows = fetch_gateio_rows("ABCUSDT", "15m", limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1:5], ["100", "102", "99", "101"])
        self.assertEqual(rows[0][7], "1200")
        self.assertIn("/api/v4/futures/usdt/candlesticks", get.call_args.args[0])

    @patch("oi_engine.signal_engine._get")
    @patch("oi_engine.market_data.fetch_bybit_rows")
    def test_signal_snapshot_fails_closed_when_binance_unavailable(self, bybit, get):
        import requests
        get.side_effect = requests.HTTPError("451 Client Error")
        with self.assertRaises(signal_engine.MarketDataError):
            signal_engine.fetch_snapshot("ABCUSDT")
        bybit.assert_not_called()

    @patch("oi_engine.signal_engine._get")
    def test_signal_snapshot_uses_binance_candles_and_live_price(self, get):
        now = int(time.time() * 1000)
        rows = []
        for index in range(80):
            start = now - (80 - index) * 900000
            rows.append([start, "100", "102", "99", "101", "10",
                         start + 899999, "1000", 0, 0, "600", "0"])
        get.side_effect = [rows, {"symbol": "ABCUSDT", "price": "101.2"}]
        snapshot = signal_engine.fetch_snapshot("ABCUSDT")
        self.assertEqual(snapshot["price_source"], "Binance USD-M Futures (USDT perpetual)")
        self.assertEqual(snapshot["live_price"], 101.2)
        self.assertEqual(len(snapshot["candles"]), 79)

    @patch("oi_engine.outcome_tracker.requests.get")
    @patch("oi_engine.market_data.fetch_bybit_rows")
    def test_outcome_tracker_falls_back_to_bybit_15m(self, bybit, get):
        import requests
        get.side_effect = requests.HTTPError("451")
        bybit.return_value = [[0, "100", "102", "99", "101", "12", 899999, "1212", "0", "0", "0", "0"]]
        rows = _fetch_klines("ABCUSDT", 0, 900000)
        self.assertEqual(rows[0][2], "102")
        self.assertEqual(bybit.call_args.args[:2], ("ABCUSDT", "15"))

    @patch("oi_engine.outcome_tracker.requests.get")
    @patch("oi_engine.market_data.fetch_bybit_rows")
    def test_outcome_tracker_falls_back_to_bybit_1m(self, bybit, get):
        import requests
        get.side_effect = requests.HTTPError("451")
        bybit.return_value = [[0, "100", "102", "99", "101", "12", 59999, "1212", "0", "0", "0", "0"]]
        rows = _fetch_1m_klines("ABCUSDT", 0, 60000)
        self.assertEqual(rows[0][2], "102")
        self.assertEqual(bybit.call_args.args[:2], ("ABCUSDT", "1"))


if __name__ == "__main__":
    unittest.main()
