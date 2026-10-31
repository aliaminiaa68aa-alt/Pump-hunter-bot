import unittest

from Bot import detect_spot_pump_entry


def candle(t, op, high, low, close, turnover):
    return {
        "t": str(t), "o": str(op), "h": str(high), "l": str(low),
        "c": str(close), "sum": str(turnover),
    }


def valid_setup():
    five = [
        candle(i, 99.0, 100.0, 98.0, 99.5, 100.0)
        for i in range(22)
    ]
    five[-1] = candle(22, 100.5, 102.5, 100.4, 102.0, 300.0)
    fifteen = [
        candle(i, 79.5 + i, 80.5 + i, 79.0 + i, 80.0 + i, 500.0)
        for i in range(20)
    ]
    return five, fifteen


class SpotPumpEntryTests(unittest.TestCase):
    def test_confirms_breakout_and_returns_risk_levels(self):
        five, fifteen = valid_setup()
        setup = detect_spot_pump_entry(five, fifteen)
        self.assertIsNotNone(setup)
        self.assertGreater(setup["entry"], setup["breakout_level"])
        self.assertLess(setup["stop"], setup["entry"])
        self.assertAlmostEqual(setup["tp2r"], setup["entry"] + 2 * (setup["entry"] - setup["stop"]))
        self.assertAlmostEqual(setup["stretch10"], setup["entry"] * 1.10)
        self.assertGreaterEqual(setup["risk_pct"], 0.4)
        self.assertLessEqual(setup["risk_pct"], 3.0)

    def test_rejects_breakout_without_turnover_confirmation(self):
        five, fifteen = valid_setup()
        five[-1]["sum"] = "110"
        self.assertIsNone(detect_spot_pump_entry(five, fifteen))

    def test_rejects_price_that_did_not_break_resistance(self):
        five, fifteen = valid_setup()
        five[-1]["c"] = "99.9"
        self.assertIsNone(detect_spot_pump_entry(five, fifteen))

    def test_rejects_overextended_breakout_candle(self):
        five, fifteen = valid_setup()
        five[-1] = candle(22, 100.5, 106.0, 100.4, 105.5, 300.0)
        self.assertIsNone(detect_spot_pump_entry(five, fifteen))

    def test_rejects_weak_15m_trend(self):
        five, fifteen = valid_setup()
        for idx, row in enumerate(fifteen):
            row["o"] = str(120 - idx)
            row["c"] = str(119 - idx)
            row["h"] = str(121 - idx)
            row["l"] = str(118 - idx)
        self.assertIsNone(detect_spot_pump_entry(five, fifteen))


if __name__ == "__main__":
    unittest.main()
