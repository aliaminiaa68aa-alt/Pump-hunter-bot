import unittest
from unittest.mock import Mock

import requests

from oi_engine.entry_guard import (
    EntryPriceError,
    evaluate_entry_freshness,
    fetch_gate_last_trade,
    gate_contract_for_symbol,
)
from oi_engine.signal_engine import Signal


def make_signal(side="LONG", entry=100.0, stop=98.0):
    return Signal("ABCUSDT", side, entry, stop, 102.0, 104.0,
                  0.8, 0.3, 0.56, 1.4, 0.0001, 82, 900000)


class EntryGuardTests(unittest.TestCase):
    def test_gate_contract_mapping(self):
        self.assertEqual(gate_contract_for_symbol("WLDUSDT"), "WLD_USDT")
        self.assertEqual(gate_contract_for_symbol("BTCUSDT"), "BTC_USDT")
        with self.assertRaises(EntryPriceError):
            gate_contract_for_symbol("BTCUSD")

    def test_fresh_gate_trade_is_accepted_and_parsed(self):
        response = Mock()
        response.json.return_value = [{
            "contract": "ABC_USDT", "price": "100.1", "create_time": 1000
        }]
        session = Mock()
        session.get.return_value = response
        result = fetch_gate_last_trade("ABCUSDT", session=session, now=1010)
        self.assertEqual(result["price"], 100.1)
        self.assertEqual(result["age_seconds"], 10.0)
        session.get.assert_called_once()

    def test_selects_newest_trade_even_if_response_is_not_ordered(self):
        response = Mock()
        response.json.return_value = [
            {"contract": "ABC_USDT", "price": "99.0", "create_time": 990},
            {"contract": "ABC_USDT", "price": "100.1", "create_time": 1000},
        ]
        session = Mock()
        session.get.return_value = response
        result = fetch_gate_last_trade("ABCUSDT", session=session, now=1010)
        self.assertEqual(result["price"], 100.1)
        self.assertEqual(result["trade_time"], 1000)

    def test_stale_gate_trade_fails_closed(self):
        response = Mock()
        response.json.return_value = [{
            "contract": "ABC_USDT", "price": "100.1", "create_time": 900
        }]
        session = Mock()
        session.get.return_value = response
        with self.assertRaisesRegex(EntryPriceError, "stale"):
            fetch_gate_last_trade("ABCUSDT", session=session, now=1000)

    def test_missing_or_wrong_contract_fails_closed(self):
        for payload in ([], [{"contract": "XYZ_USDT", "price": "100", "create_time": 1000}]):
            response = Mock()
            response.json.return_value = payload
            session = Mock()
            session.get.return_value = response
            with self.assertRaises(EntryPriceError):
                fetch_gate_last_trade("ABCUSDT", session=session, now=1000)

    def test_http_failure_fails_closed(self):
        session = Mock()
        session.get.side_effect = requests.Timeout("timeout")
        with self.assertRaises(EntryPriceError):
            fetch_gate_last_trade("ABCUSDT", session=session, now=1000)

    def test_future_trade_timestamp_fails_closed(self):
        response = Mock()
        response.json.return_value = [{
            "contract": "ABC_USDT", "price": "100", "create_time": 1010
        }]
        session = Mock()
        session.get.return_value = response
        with self.assertRaisesRegex(EntryPriceError, "future"):
            fetch_gate_last_trade("ABCUSDT", session=session, now=1000)

    def test_long_rejects_adverse_and_favorable_chasing_outside_tolerance(self):
        signal = make_signal("LONG", 100.0, 98.0)  # 2% risk; 0.4% tolerance
        self.assertTrue(evaluate_entry_freshness(signal, 100.2)["accepted"])
        self.assertFalse(evaluate_entry_freshness(signal, 100.5)["accepted"])
        self.assertFalse(evaluate_entry_freshness(signal, 99.5)["accepted"])

    def test_short_rejects_adverse_and_favorable_chasing_outside_tolerance(self):
        signal = make_signal("SHORT", 100.0, 102.0)
        self.assertTrue(evaluate_entry_freshness(signal, 99.8)["accepted"])
        self.assertFalse(evaluate_entry_freshness(signal, 100.5)["accepted"])
        self.assertFalse(evaluate_entry_freshness(signal, 99.5)["accepted"])

    def test_tolerance_is_twenty_percent_of_risk_capped_at_half_percent(self):
        signal = make_signal(entry=100.0, stop=90.0)
        result = evaluate_entry_freshness(signal, 100.4)
        self.assertAlmostEqual(result["tolerance_pct"], 0.5)
        self.assertTrue(result["accepted"])

    def test_invalid_price_fails_closed(self):
        for price in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(EntryPriceError):
                evaluate_entry_freshness(make_signal(), price)


if __name__ == "__main__":
    unittest.main()
