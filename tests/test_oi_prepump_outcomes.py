import unittest

from Bot import apply_oi_prepump_outcome_candles


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

    def test_marks_record_complete_after_24_hours(self):
        row = record()
        apply_oi_prepump_outcome_candles(row, [], now=1000 + 24 * 3600)
        self.assertTrue(row["completed"])
        self.assertEqual(row["completed_at"], 1000 + 24 * 3600)


if __name__ == "__main__":
    unittest.main()
