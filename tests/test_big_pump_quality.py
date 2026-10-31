import unittest

from Bot import detect_high_potential_pump


def make_candles(count, start=100.0, step=0.0, turnover=100.0):
    rows = []
    for i in range(count):
        close = start + i * step
        rows.append({
            "t": str(i),
            "o": close - 0.1,
            "h": close + 0.2,
            "l": close - 0.2,
            "c": close,
            "sum": turnover,
        })
    return rows


def valid_setup_inputs():
    candles_15m = []
    for i in range(21):
        candles_15m.append({
            "t": str(i), "o": 100.3, "h": 100.8, "l": 100.0,
            "c": 100.4, "sum": 100.0,
        })
    candles_15m.append({
        "t": "breakout", "o": 100.6, "h": 102.5, "l": 100.5,
        "c": 102.2, "sum": 300.0,
    })
    return candles_15m, make_candles(28, start=100, step=0.5), make_candles(28, start=100, step=0.5)


class BigPumpQualityTests(unittest.TestCase):
    def test_accepts_early_high_volume_breakout_with_higher_timeframe_trend(self):
        c15, c1h, c4h = valid_setup_inputs()
        result = detect_high_potential_pump(c15, c1h, c4h, 2_000_000)
        self.assertIsNotNone(result)
        self.assertEqual(result["candle_time"], "breakout")
        self.assertGreater(result["stretch20"], result["stretch10"])
        self.assertGreater(result["stop"], 0)
        self.assertLessEqual(result["risk_pct"], 2.5)

    def test_rejects_insufficient_24h_turnover(self):
        c15, c1h, c4h = valid_setup_inputs()
        self.assertIsNone(detect_high_potential_pump(c15, c1h, c4h, 999_999))

    def test_rejects_no_breakout(self):
        c15, c1h, c4h = valid_setup_inputs()
        c15[-1]["c"] = 100.7
        c15[-1]["h"] = 100.75
        self.assertIsNone(detect_high_potential_pump(c15, c1h, c4h, 2_000_000))

    def test_rejects_low_volume_expansion(self):
        c15, c1h, c4h = valid_setup_inputs()
        c15[-1]["sum"] = 150.0
        self.assertIsNone(detect_high_potential_pump(c15, c1h, c4h, 2_000_000))

    def test_rejects_flat_higher_timeframe_trend(self):
        c15, _, _ = valid_setup_inputs()
        flat = make_candles(28, start=100, step=0.0)
        self.assertIsNone(detect_high_potential_pump(c15, flat, flat, 2_000_000))


if __name__ == "__main__":
    unittest.main()
