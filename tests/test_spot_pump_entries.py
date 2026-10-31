import unittest

from Bot import (
    apply_spot_pump_outcome_candles,
    detect_spot_pump_entry,
    summarize_spot_pump_outcomes,
)


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

    def test_reports_first_failed_gate_for_diagnostics(self):
        five, fifteen = valid_setup()
        five[-1]["sum"] = "110"
        diagnostics = {}
        self.assertIsNone(detect_spot_pump_entry(five, fifteen, diagnostics))
        self.assertEqual(diagnostics, {"low_turnover_ratio": 1})

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


    def test_spot_outcome_tracks_forward_excursion_and_targets(self):
        record = {
            "created_at": 1000, "last_candle_time": 1000,
            "entry": 100.0, "stop": 97.0, "tp2r": 106.0, "tp3r": 109.0,
            "stretch10": 110.0, "stretch20": 120.0,
            "max_favorable_pct": 0.0, "max_adverse_pct": 0.0,
            "targets_hit": {"tp2r": False, "tp3r": False, "stretch10": False, "stretch20": False},
            "first_result": None, "completed": False,
        }
        candles = [
            {"t": "900", "h": "130", "l": "90"},
            {"t": "1060", "h": "111", "l": "99"},
        ]
        result = apply_spot_pump_outcome_candles(record, candles, now=2000)
        self.assertEqual(result["first_result"], "tp2r")
        self.assertTrue(result["targets_hit"]["tp2r"])
        self.assertTrue(result["targets_hit"]["tp3r"])
        self.assertTrue(result["targets_hit"]["stretch10"])
        self.assertFalse(result["targets_hit"]["stretch20"])
        self.assertAlmostEqual(result["max_favorable_pct"], 11.0)
        self.assertAlmostEqual(result["max_adverse_pct"], -1.0)

    def test_spot_outcome_assumes_stop_first_if_stop_and_target_same_candle(self):
        record = {
            "created_at": 1000, "last_candle_time": 1000,
            "entry": 100.0, "stop": 97.0, "tp2r": 106.0, "tp3r": 109.0,
            "stretch10": 110.0, "stretch20": 120.0,
            "max_favorable_pct": 0.0, "max_adverse_pct": 0.0,
            "targets_hit": {"tp2r": False, "tp3r": False, "stretch10": False, "stretch20": False},
            "first_result": None, "completed": False,
        }
        result = apply_spot_pump_outcome_candles(
            record, [{"t": "1060", "h": "107", "l": "96"}], now=2000
        )
        self.assertEqual(result["first_result"], "ambiguous_same_candle_stop_first")
        self.assertTrue(result["targets_hit"]["tp2r"])
        self.assertTrue(result["stop_hit"])

    def test_spot_outcome_summary_only_counts_completed_records(self):
        records = [
            {"completed": True, "first_result": "tp2r",
             "targets_hit": {"tp2r": True, "tp3r": False, "stretch10": False, "stretch20": False}},
            {"completed": False, "first_result": "stop", "targets_hit": {}},
        ]
        summary = summarize_spot_pump_outcomes(records)
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["first_result_counts"]["tp2r"], 1)
        self.assertEqual(summary["target_touches"]["tp2r"], 1)


if __name__ == "__main__":
    unittest.main()
