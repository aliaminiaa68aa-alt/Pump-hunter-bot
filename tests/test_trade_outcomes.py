import unittest

from Bot import (
    apply_trade_outcome_candles,
    register_trade_outcome,
    summarize_trade_outcomes,
)


def candle(t, high, low):
    return {"t": str(t), "h": str(high), "l": str(low)}


def long_record():
    return {
        "contract": "ABC_USDT",
        "strategy": "BOS",
        "direction": "LONG",
        "created_at": 1000,
        "last_candle_time": 100,
        "entry": 100.0,
        "stop": 98.0,
        "targets": {"TP1_1R": 102.0, "TP2_2R": 104.0},
        "targets_hit": {"TP1_1R": False, "TP2_2R": False},
        "stop_hit": False,
        "first_result": None,
        "max_favorable_pct": 0.0,
        "max_adverse_pct": 0.0,
        "completed": False,
    }


class ConfirmedTradeOutcomeTests(unittest.TestCase):
    def test_ignores_candles_at_or_before_registration_and_tracks_tp(self):
        row = long_record()
        apply_trade_outcome_candles(
            row, [candle(90, 150, 50), candle(120, 103, 99)], now=1100
        )
        self.assertEqual(row["first_result"], "TP1_1R")
        self.assertTrue(row["targets_hit"]["TP1_1R"])
        self.assertFalse(row["stop_hit"])
        self.assertAlmostEqual(row["max_favorable_pct"], 3.0)
        self.assertAlmostEqual(row["max_adverse_pct"], -1.0)
        self.assertEqual(row["last_candle_time"], 120)

    def test_same_candle_stop_and_target_is_conservatively_ambiguous(self):
        row = long_record()
        apply_trade_outcome_candles(row, [candle(120, 105, 97)], now=1100)
        self.assertEqual(row["first_result"], "ambiguous_same_candle_stop_first")
        self.assertTrue(row["stop_hit"])
        self.assertTrue(row["targets_hit"]["TP1_1R"])
        self.assertTrue(row["targets_hit"]["TP2_2R"])

    def test_tracks_short_direction(self):
        row = long_record()
        row.update({
            "direction": "SHORT", "entry": 100.0, "stop": 102.0,
            "targets": {"TP1_1R": 98.0}, "targets_hit": {"TP1_1R": False},
        })
        apply_trade_outcome_candles(row, [candle(120, 99, 97)], now=1100)
        self.assertEqual(row["first_result"], "TP1_1R")
        self.assertAlmostEqual(row["max_favorable_pct"], 3.0)

    def test_registers_valid_trade_and_rejects_invalid_levels(self):
        state = {}
        register_trade_outcome(
            state, "ABC_USDT", "BOS", 100, 98,
            {"TP1_1R": 102, "TP2_2R": 104}, created_at=1000,
        )
        self.assertEqual(len(state["trade_outcomes"]), 1)
        register_trade_outcome(
            state, "BAD_USDT", "BOS", 100, 101,
            {"TP1_1R": 102}, created_at=1000,
        )
        self.assertEqual(len(state["trade_outcomes"]), 1)

    def test_summary_counts_only_completed_records_per_strategy(self):
        completed = long_record()
        completed.update({
            "completed": True, "first_result": "TP1_1R",
            "targets_hit": {"TP1_1R": True, "TP2_2R": False},
            "max_favorable_pct": 4.0, "max_adverse_pct": -1.0,
        })
        pending = long_record()
        summary = summarize_trade_outcomes([completed, pending])
        self.assertEqual(summary["BOS"]["completed"], 1)
        self.assertEqual(summary["BOS"]["first_results"], {"TP1_1R": 1})
        self.assertEqual(summary["BOS"]["avg_mfe_pct"], 4.0)


if __name__ == "__main__":
    unittest.main()
