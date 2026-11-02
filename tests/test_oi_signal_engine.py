import unittest
from unittest.mock import patch

from oi_engine.signal_engine import diagnose_snapshot, evaluate_snapshot, scan, MarketDataError, validate_oi_cadence


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
    def test_diagnostics_name_failed_filters_without_changing_signal(self):
        snapshot = make_snapshot("LONG")
        snapshot["oi_values"] = [100.0, 100.0, 100.1]
        snapshot["candles"][-1]["quote_volume"] = 105000.0
        snapshot["candles"][-1]["taker_buy_quote"] = 55000.0
        reasons = diagnose_snapshot(snapshot)
        self.assertTrue(any("رشد OI کم است" in reason for reason in reasons))
        self.assertTrue(any("حجم پایین است" in reason for reason in reasons))
        self.assertIsNone(evaluate_snapshot(snapshot))

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

    def test_live_oi_cadence_requires_exact_15m_intervals(self):
        valid = [
            {"timestamp": 0},
            {"timestamp": 900000},
            {"timestamp": 1800000},
        ]
        validate_oi_cadence(valid)
        irregular = [
            {"timestamp": 0},
            {"timestamp": 600000},
            {"timestamp": 1200000},
        ]
        with self.assertRaisesRegex(MarketDataError, "exact 15m cadence"):
            validate_oi_cadence(irregular)

    def test_oi_change_uses_one_15m_interval_not_30m(self):
        snapshot = make_snapshot("LONG")
        snapshot["oi_values"] = [100.0, 100.5, 100.5]
        # The 30m change is +0.5%, but the latest 15m change is 0%.
        # A valid 15m signal must therefore be rejected.
        self.assertIsNone(evaluate_snapshot(snapshot))
        reasons = diagnose_snapshot(snapshot)
        self.assertTrue(any("رشد OI کم است" in reason for reason in reasons))

    def test_rejects_taker_buy_ratio_above_one(self):
        snapshot = make_snapshot("LONG")
        snapshot["candles"][-1]["taker_buy_quote"] = (
            snapshot["candles"][-1]["quote_volume"] * 1.2
        )
        self.assertIsNone(evaluate_snapshot(snapshot))
        self.assertTrue(any("جریان خرید نامعتبر" in reason
                            for reason in diagnose_snapshot(snapshot)))

    def test_rejects_negative_taker_buy_volume(self):
        snapshot = make_snapshot("SHORT")
        snapshot["candles"][-1]["taker_buy_quote"] = -1.0
        self.assertIsNone(evaluate_snapshot(snapshot))
        self.assertTrue(any("جریان خرید نامعتبر" in reason
                            for reason in diagnose_snapshot(snapshot)))

    def test_rejects_zero_last_candle_quote_volume(self):
        snapshot = make_snapshot("LONG")
        snapshot["candles"][-1]["quote_volume"] = 0.0
        self.assertIsNone(evaluate_snapshot(snapshot))
        self.assertTrue(any("جریان خرید نامعتبر" in reason
                            for reason in diagnose_snapshot(snapshot)))

    def test_no_signal_without_oi_growth(self):
        snapshot = make_snapshot("LONG")
        snapshot["oi_values"] = [100.0, 100.0, 100.0]
        self.assertIsNone(evaluate_snapshot(snapshot))

    def test_no_signal_when_oi_sample_is_stale(self):
        snapshot = make_snapshot("LONG")
        snapshot["oi_timestamp"] -= 16 * 60 * 1000
        self.assertIsNone(evaluate_snapshot(snapshot))

    def test_oi_period_end_at_next_boundary_is_accepted(self):
        snapshot = make_snapshot("LONG")
        snapshot["oi_timestamp"] = snapshot["candles"][-1]["close_time"] + 1
        signal = evaluate_snapshot(snapshot)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "LONG")

    def test_oi_timestamp_after_next_boundary_is_rejected(self):
        snapshot = make_snapshot("LONG")
        snapshot["oi_timestamp"] = snapshot["candles"][-1]["close_time"] + 2
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
