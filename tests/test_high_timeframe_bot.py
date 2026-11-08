import unittest

from high_timeframe_bot import (
    Setup, analyze_candles, atr, ema, make_message, pivot_levels, signal_key,
)


def make_candles(side="LONG"):
    rows = []
    for i in range(80):
        if side == "LONG":
            close = 100.0 + i * 0.10
            candle = {"open_time": i * 14_400_000, "open": close - 0.05,
                      "high": close + 0.8, "low": close - 0.8,
                      "close": close, "volume": 100.0, "close_time": i * 14_400_000 + 1,
                      "quote_volume": 1000.0}
        else:
            close = 120.0 - i * 0.10
            candle = {"open_time": i * 14_400_000, "open": close + 0.05,
                      "high": close + 0.8, "low": close - 0.8,
                      "close": close, "volume": 100.0, "close_time": i * 14_400_000 + 1,
                      "quote_volume": 1000.0}
        rows.append(candle)

    # Confirmed historical swing level far enough away to serve as TP2.
    if side == "LONG":
        rows[50]["high"] = 120.0
        rows[49]["high"] = 108.0
        rows[51]["high"] = 108.0
        rows[52]["high"] = 108.0
        # Latest closed candle sweeps the prior 12-candle low and reclaims it.
        prior_low = min(x["low"] for x in rows[-13:-1])
        last = rows[-1]
        last.update({"open": 107.0, "low": prior_low - 0.5,
                     "close": prior_low + 1.0, "high": prior_low + 1.2})
    else:
        rows[50]["low"] = 95.0
        rows[49]["low"] = 112.0
        rows[51]["low"] = 112.0
        rows[52]["low"] = 112.0
        prior_high = max(x["high"] for x in rows[-13:-1])
        last = rows[-1]
        last.update({"open": 113.0, "high": prior_high + 0.5,
                     "close": prior_high - 1.0, "low": prior_high - 1.2})
    return rows


class HighTimeframeScannerTests(unittest.TestCase):
    def test_ema_and_atr_are_positive(self):
        values = [float(x) for x in range(1, 81)]
        self.assertGreater(ema(values, 20), 0)
        self.assertGreater(atr(make_candles(), 14), 0)

    def test_long_setup_requires_closed_candle_sweep(self):
        rows = make_candles("LONG")
        setup = analyze_candles("TESTUSDT", "4h", rows, rows[-1]["close"])
        self.assertIsNotNone(setup)
        self.assertEqual(setup.side, "LONG")
        self.assertGreaterEqual(setup.room_to_tp1_pct, 5.0)
        self.assertGreaterEqual(setup.room_to_tp2_pct, 8.0)
        self.assertLess(setup.stop, setup.entry)

    def test_short_setup_requires_closed_candle_sweep(self):
        rows = make_candles("SHORT")
        setup = analyze_candles("TESTUSDT", "1d", rows, rows[-1]["close"])
        self.assertIsNotNone(setup)
        self.assertEqual(setup.side, "SHORT")
        self.assertGreaterEqual(setup.room_to_tp1_pct, 5.0)
        self.assertGreaterEqual(setup.room_to_tp2_pct, 8.0)
        self.assertGreater(setup.stop, setup.entry)

    def test_stale_price_is_rejected(self):
        rows = make_candles("LONG")
        setup = analyze_candles("TESTUSDT", "4h", rows, rows[-1]["close"] * 1.10)
        self.assertIsNone(setup)

    def test_no_sweep_means_no_signal(self):
        rows = make_candles("LONG")
        rows[-1]["low"] = rows[-2]["low"] + 0.01
        setup = analyze_candles("TESTUSDT", "4h", rows, rows[-1]["close"])
        self.assertIsNone(setup)

    def test_message_labels_spot_and_short_correctly(self):
        setup = Setup("ABCUSDT", "4h", "SHORT", 100, 104, 95, 90,
                      4, 5, 10, 105, 123, 0.2, 0.01, "Binance,Bybit")
        text = make_message(setup)
        self.assertIn("مرجع قیمت: Binance Spot", text)
        self.assertIn("فقط فیوچرز/مارجین", text)
        self.assertIn("مرجع تحلیل: 4h", text)

    def test_signal_key_changes_with_new_sweep_candle(self):
        base = Setup("ABCUSDT", "1d", "LONG", 100, 95, 105, 110,
                     5, 5, 10, 95, 1000, 0.1, 0.01, "Binance,Bybit")
        newer = Setup("ABCUSDT", "1d", "LONG", 101, 96, 106, 111,
                      5, 5, 10, 96, 2000, 0.1, 0.01, "Binance,Bybit")
        self.assertNotEqual(signal_key(base), signal_key(newer))


if __name__ == "__main__":
    unittest.main()
