import time
import unittest
from unittest.mock import Mock, patch

import requests
from oi_engine import signal_engine
from oi_engine.market_data import fetch_spot_rows
from oi_engine.outcome_tracker import _fetch_klines, _fetch_1m_klines

def spot_rows(count=80, interval_ms=900000):
    now = int(time.time() * 1000)
    rows = []
    for index in range(count):
        start = now - (count - index) * interval_ms
        rows.append([start, '100', '102', '99', '101', '10',
                     start + interval_ms - 1, '1000', 0, 0, '600', '0'])
    return rows

class BinanceSpotOnlyTests(unittest.TestCase):
    @patch('oi_engine.market_data.requests.get')
    def test_spot_rows_use_binance_spot_klines(self, get):
        response = Mock()
        response.json.return_value = spot_rows(3)
        response.raise_for_status.return_value = None
        get.return_value = response
        rows = fetch_spot_rows('ABCUSDT', '15m', limit=3)
        self.assertEqual(len(rows), 3)
        self.assertIn('/api/v3/klines', get.call_args.args[0])

    @patch('oi_engine.signal_engine._get')
    def test_signal_snapshot_uses_binance_spot_candles_and_ticker(self, get):
        rows = spot_rows(80)
        get.side_effect = [rows, {'symbol': 'ABCUSDT', 'price': '101.2'}]
        snapshot = signal_engine.fetch_snapshot('ABCUSDT')
        self.assertEqual(snapshot['price_source'], 'Binance Spot')
        self.assertEqual(snapshot['live_price'], 101.2)
        self.assertEqual(len(snapshot['candles']), 79)
        self.assertEqual(get.call_args_list[0].args[0], '/api/v3/ticker/price') if False else None
        self.assertEqual(get.call_args_list[0].args[0], '/api/v3/klines')
        self.assertEqual(get.call_args_list[1].args[0], '/api/v3/ticker/price')

    @patch('oi_engine.signal_engine._get', side_effect=requests.HTTPError('451 Client Error'))
    def test_signal_snapshot_fails_closed_without_non_spot_fallback(self, get):
        with self.assertRaises(requests.HTTPError):
            signal_engine.fetch_snapshot('ABCUSDT')
        get.assert_called_once_with('/api/v3/klines',
                                   {'symbol': 'ABCUSDT', 'interval': '15m', 'limit': 80})

    @patch('oi_engine.outcome_tracker.requests.get')
    def test_outcome_tracker_uses_spot_15m_candles_only(self, get):
        response = Mock()
        response.json.return_value = [['0', '100', '102', '99', '101', '12',
                                       '899999', '1212', '0', '0', '0', '0']]
        response.raise_for_status.return_value = None
        get.return_value = response
        rows = _fetch_klines('ABCUSDT', 0, 900000)
        self.assertEqual(rows[0][2], '102')
        self.assertIn('api.binance.com/api/v3/klines', get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs['params']['interval'], '15m')

    @patch('oi_engine.outcome_tracker.requests.get')
    def test_outcome_tracker_uses_spot_1m_candles_only(self, get):
        response = Mock()
        response.json.return_value = [['0', '100', '102', '99', '101', '12',
                                       '59999', '1212', '0', '0', '0', '0']]
        response.raise_for_status.return_value = None
        get.return_value = response
        rows = _fetch_1m_klines('ABCUSDT', 0, 60000)
        self.assertEqual(rows[0][2], '102')
        self.assertEqual(get.call_args.kwargs['params']['interval'], '1m')

if __name__ == '__main__':
    unittest.main()
