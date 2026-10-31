import unittest

from Bot import apply_oi_prepump_outcome_candles, summarize_oi_prepump_outcomes


def candle(t, high, low):
    return {"t": str(t), "h": str(high), "l": str(low)}


def record():
    return {
        "contract": "ABC_USDT",
        "created_at": 1000,
        "last_candle_time": 100,
        "entry": 100.0,
        "stop": 98.0,
        "tp1": 102.0,
        "tp2": 104.0,
        "max_favorable_pct": 0.0,
        "max_adverse_pct": 0.0,
        "tp1_hit": False,
        "tp2_hit": False,
        "stop_hit": False,
        "first_result": None,
        "completed": False,
    }


class OIPrePumpOutcomeTests(unittest.TestCase):
    def test_tracks_favorable_and_adverse_excursion_and_tp1(self):
        row = record()
        apply_oi_prepump_outcome_candles(
            row, [candle(90, 150, 50), candle(120, 103, 99)], now=1100
        )
        self.assertAlmostEqual(row["max_favorable_pct"], 3.0)
        self.assertAlmostEqual(row["max_adverse_pct"], -1.0)
        self.assertTrue(row["tp1_hit"])
        self.assertFalse(row["stop_hit"])
        self.assertEqual(row["first_result"], "tp1")
        self.assertEqual(row["last_candle_time"], 120)

    def test_uses_conservative_stop_first_when_both_levels_hit_same_candle(self):
        row = record()
        apply_oi_prepump_outcome_candles(row, [candle(120, 105, 97)], now=1100)
        self.assertTrue(row["stop_hit"])
        self.assertTrue(row["tp1_hit"])
        self.assertTrue(row["tp2_hit"])
        self.assertEqual(row["first_result"], "ambiguous_same_candle_stop_first")

    def test_summary_uses_completed_records_only_and_counts_touch_events(self):
        completed_tp1 = record()
        completed_tp1.update({
            "completed": True, "first_result": "tp1",
            "tp1_hit": True, "tp2_hit": False, "stop_hit": False,
        })
        completed_ambiguous = record()
        completed_ambiguous.update({
            "completed": True,
            "first_result": "ambiguous_same_candle_stop_first",
            "tp1_hit": True, "tp2_hit": True, "stop_hit": True,
        })
        completed_none = record()
        completed_none.update({"completed": True, "first_result": None})
        still_tracking = record()
        summary = summarize_oi_prepump_outcomes([
            completed_tp1, completed_ambiguous, completed_none, still_tracking
        ])
        self.assertEqual(summary["completed"], 3)
        self.assertEqual(summary["first_result_counts"]["tp1"], 1)
        self.assertEqual(summary["first_result_counts"]["ambiguous_same_candle_stop_first"], 1)
        self.assertEqual(summary["tp1_hit"], 2)
        self.assertEqual(summary["tp2_hit"], 1)
        self.assertEqual(summary["stop_hit"], 1)
        self.assertEqual(summary["no_level_hit"], 1)

    def test_marks_record_complete_after_24_hours(self):
        row = record()
        apply_oi_prepump_outcome_candles(row, [], now=1000 + 24 * 3600)
        self.assertTrue(row["completed"])
        self.assertEqual(row["completed_at"], 1000 + 24 * 3600)


if __name__ == "__main__":
    unittest.main()
