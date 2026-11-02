import unittest
from unittest.mock import patch

from Bot import register_early_watch_outcome, update_early_watch_outcomes


def candle(t, high, low, close):
    return {"t": str(t), "h": str(high), "l": str(low), "c": str(close)}


class EarlyWatchOutcomeTests(unittest.TestCase):
    def test_registration_stores_price_only_observation(self):
        state = {}
        register_early_watch_outcome(
            state, "FIL_USDT", "LIQUIDITY_INFLOW", 100, created_at=1_000
        )

        record = state["early_watch_outcomes"][0]
        self.assertEqual(record["contract"], "FIL_USDT")
        self.assertEqual(record["strategy"], "LIQUIDITY_INFLOW")
        self.assertEqual(record["created_at"], 1_000)
        self.assertEqual(record["entry"], 100.0)
        self.assertEqual(record["last_candle_time"], 0)
        self.assertEqual(record["checkpoints"], {})
        self.assertFalse(record["completed"])

    def test_tracks_excursions_checkpoints_and_completion_from_closed_candles(self):
        state = {}
        register_early_watch_outcome(
            state, "FIL_USDT", "LEG_SPIKE_STAGE1", 100, created_at=1_000
        )
        candles = [
            candle(999, 150, 50, 100),       # Before alert: must be ignored.
            candle(1_000, 103, 98, 101),     # Alert candle boundary is included.
            candle(4_600, 106, 97, 104),     # First candle at/after 1h.
            candle(15_400, 108, 96, 105),   # First candle at/after 4h.
            candle(87_400, 110, 95, 107),   # First candle at/after 24h.
        ]

        with patch("Bot.closed_candles", return_value=candles) as fetch:
            update_early_watch_outcomes(state, now=87_400)

        fetch.assert_called_once_with("FIL_USDT", "15m", 105)
        record = state["early_watch_outcomes"][0]
        self.assertAlmostEqual(record["max_favorable_pct"], 10.0)
        self.assertAlmostEqual(record["max_adverse_pct"], -5.0)
        self.assertEqual(record["checkpoints"]["1h"]["close"], 104.0)
        self.assertEqual(record["checkpoints"]["4h"]["close"], 105.0)
        self.assertEqual(record["checkpoints"]["24h"]["close"], 107.0)
        self.assertEqual(record["last_candle_time"], 87_400)
        self.assertTrue(record["completed"])

    def test_failed_candle_fetch_keeps_record_pending_for_retry(self):
        state = {}
        register_early_watch_outcome(
            state, "FIL_USDT", "LIQUIDITY_INFLOW", 100, created_at=1_000
        )

        with patch("Bot.closed_candles", side_effect=RuntimeError("temporary API error")):
            update_early_watch_outcomes(state, now=5_000)

        record = state["early_watch_outcomes"][0]
        self.assertEqual(record["last_candle_time"], 0)
        self.assertFalse(record["completed"])
        self.assertEqual(record["checkpoints"], {})


if __name__ == "__main__":
    unittest.main()
