import unittest
from unittest.mock import patch

import Bot


def candle(t, op, high, low, close, turnover=100.0):
    return {
        "t": str(t), "o": str(op), "h": str(high), "l": str(low),
        "c": str(close), "sum": str(turnover),
    }


class PrePumpEntryTests(unittest.TestCase):
    def make_valid_setup(self):
        one_minute = []
        for i in range(21):
            base = 100.0 + (i % 3) * 0.01
            one_minute.append(candle(i, base, base + 0.08, base - 0.10, base + 0.02, 100))
        one_minute.append(candle(21, 100.10, 100.48, 100.08, 100.40, 220))

        five_minute = []
        for i in range(13):
            five_minute.append(candle(100 + i, 100.0, 100.25, 99.85, 100.05, 100))
        for i in range(5):
            base = 100.02 + i * 0.005
            five_minute.append(candle(113 + i, base, 100.15, 99.92, 100.08, 110))
        five_minute.append(candle(118, 100.10, 100.48, 100.08, 100.40, 220))
        return one_minute, five_minute

    def test_detects_early_liquidity_buildup_before_price_extension(self):
        one_minute = []
        for i in range(21):
            base = 100.0 + (i % 2) * 0.01
            turnover = 220 if i >= 18 else 100
            one_minute.append(candle(i, base, base + 0.10, base - 0.10, base + 0.05, turnover))

        five_minute = []
        for i in range(18):
            turnover = 130 if i >= 16 else 100
            five_minute.append(candle(100 + i, 100.0, 100.30, 99.80, 100.20, turnover))

        result = Bot.detect_liquidity_buildup(one_minute, five_minute)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["score"], 4)
        self.assertGreaterEqual(result["turnover_1m_ratio"], 1.8)
        self.assertLessEqual(result["price_change_3m_pct"], 1.8)

    def test_liquidity_radar_rejects_already_extended_price(self):
        one_minute = []
        for i in range(21):
            base = 100.0
            turnover = 220 if i >= 18 else 100
            one_minute.append(candle(i, base, base + 0.10, base - 0.10, base + 0.05, turnover))
        one_minute[-1] = candle(20, 101.9, 102.1, 101.8, 102.0, 500)

        five_minute = []
        for i in range(18):
            turnover = 130 if i >= 16 else 100
            five_minute.append(candle(100 + i, 100.0, 100.30, 99.80, 100.20, turnover))

        self.assertIsNone(Bot.detect_liquidity_buildup(one_minute, five_minute))

    def test_returns_entry_stop_and_targets_for_valid_breakout(self):
        one_minute, five_minute = self.make_valid_setup()
        result = Bot.detect_prepump_entry(one_minute, five_minute)
        self.assertIsNotNone(result)
        self.assertGreater(result["entry"], result["breakout_level"])
        self.assertLess(result["stop"], result["entry"])
        self.assertGreater(result["tp1"], result["entry"])
        self.assertGreater(result["tp2"], result["tp1"])
        self.assertGreaterEqual(result["risk_pct"], 0.20)
        self.assertLessEqual(result["risk_pct"], 1.50)

    def test_rejects_candle_without_breakout(self):
        one_minute, five_minute = self.make_valid_setup()
        five_minute[-1]["c"] = "100.10"
        five_minute[-1]["h"] = "100.20"
        self.assertIsNone(Bot.detect_prepump_entry(one_minute, five_minute))

    def test_diagnostics_prioritize_missing_breakout_over_low_volume(self):
        one_minute, five_minute = self.make_valid_setup()
        five_minute[-1]["c"] = "100.10"
        five_minute[-1]["h"] = "100.20"
        five_minute[-1]["sum"] = "10"
        diagnostics = {}
        result = Bot.detect_prepump_entry(one_minute, five_minute, diagnostics)
        self.assertIsNone(result)
        self.assertEqual(diagnostics.get("no_5m_breakout"), 1)
        self.assertNotIn("low_5m_breakout_turnover", diagnostics)

    def test_rejects_excessive_price_extension(self):
        one_minute, five_minute = self.make_valid_setup()
        five_minute[-1]["c"] = "103.00"
        five_minute[-1]["h"] = "103.10"
        five_minute[-1]["sum"] = "1000"
        self.assertIsNone(Bot.detect_prepump_entry(one_minute, five_minute))

    def test_refreshes_entry_and_targets_at_latest_close(self):
        one_minute, five_minute = self.make_valid_setup()
        setup = Bot.detect_prepump_entry(one_minute, five_minute)
        latest_close = setup["entry"] * 1.002
        refreshed = Bot.refresh_prepump_entry(setup, latest_close)
        self.assertIsNotNone(refreshed)
        self.assertAlmostEqual(refreshed["entry"], latest_close)
        self.assertAlmostEqual(refreshed["tp1"], refreshed["entry"] + 2.0 * (refreshed["entry"] - refreshed["stop"]))
        self.assertAlmostEqual(refreshed["tp2"], refreshed["entry"] + 3.0 * (refreshed["entry"] - refreshed["stop"]))

    def test_rejects_stale_or_chased_breakout(self):
        one_minute, five_minute = self.make_valid_setup()
        setup = Bot.detect_prepump_entry(one_minute, five_minute)
        diagnostics = {}
        self.assertIsNone(Bot.refresh_prepump_entry(setup, setup["entry"] * 1.01, diagnostics))
        self.assertEqual(diagnostics.get("price_chased_before_alert"), 1)
        diagnostics = {}
        self.assertIsNone(Bot.refresh_prepump_entry(setup, setup["breakout_level"] * 0.999, diagnostics))
        self.assertEqual(diagnostics.get("breakout_lost_before_alert"), 1)

    def test_allows_normal_5m_turnover_when_1m_breakout_is_strong(self):
        one_minute, five_minute = self.make_valid_setup()
        five_minute[-1]["sum"] = "125"
        result = Bot.detect_prepump_entry(one_minute, five_minute)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["turnover_ratio_1m"], 1.5)
        self.assertGreaterEqual(result["turnover_ratio_5m"], 1.20)

    def test_rejects_setup_with_very_low_5m_turnover(self):
        one_minute, five_minute = self.make_valid_setup()
        five_minute[-1]["sum"] = "50"
        diagnostics = {}
        result = Bot.detect_prepump_entry(one_minute, five_minute, diagnostics)
        self.assertIsNone(result)
        self.assertEqual(diagnostics.get("low_5m_breakout_turnover"), 1)

    def test_finds_breakout_in_recent_closed_candles(self):
        one_minute, five_minute = self.make_valid_setup()
        # The 5m breakout occurred four candles ago; newer 5m candles no longer break out.
        for i in range(4):
            base = 100.12 + i * 0.005
            five_minute.append(candle(119 + i, base, base + 0.04, base - 0.05, base + 0.01, 100))
        current_only = Bot.detect_prepump_entry(one_minute, five_minute)
        self.assertIsNone(current_only)
        diagnostics = {}
        result = Bot.detect_recent_prepump_entry(one_minute, five_minute, lookback=5, diagnostics=diagnostics)
        self.assertIsNotNone(result)
        self.assertEqual(result["minutes_since_breakout_candle"], 20)


    def test_high_quality_radar_accepts_only_converging_signals(self):
        strong = {
            "score": 5,
            "turnover_1m_ratio": 2.4,
            "turnover_5m_ratio": 1.8,
            "compression_pct": 1.2,
            "price_change_3m_pct": 0.15,
            "close_position": 0.82,
        }
        self.assertTrue(Bot.is_high_quality_liquidity_candidate(strong))

    def test_high_quality_radar_rejects_four_of_five_and_weak_turnover(self):
        weak = {
            "score": 4,
            "turnover_1m_ratio": 2.5,
            "turnover_5m_ratio": 3.4,
            "compression_pct": 0.5,
            "price_change_3m_pct": -0.05,
            "close_position": 0.80,
        }
        self.assertFalse(Bot.is_high_quality_liquidity_candidate(weak))
        weak["score"] = 5
        weak["turnover_5m_ratio"] = 1.05
        self.assertFalse(Bot.is_high_quality_liquidity_candidate(weak))

    def test_high_quality_radar_rejects_chased_or_weak_close(self):
        weak = {
            "score": 5,
            "turnover_1m_ratio": 2.5,
            "turnover_5m_ratio": 1.8,
            "compression_pct": 1.0,
            "price_change_3m_pct": 1.2,
            "close_position": 0.80,
        }
        self.assertFalse(Bot.is_high_quality_liquidity_candidate(weak))
        weak["price_change_3m_pct"] = 0.2
        weak["close_position"] = 0.52
        self.assertFalse(Bot.is_high_quality_liquidity_candidate(weak))


    def test_radar_only_candidate_never_sends_telegram_message(self):
        candles = [candle(i, 100.0, 100.2, 99.8, 100.05, 100) for i in range(31)]
        radar = {
            "candle_time": "123456", "score": 5, "turnover_1m_ratio": 2.1,
            "turnover_5m_ratio": 1.4, "compression_pct": 1.0,
            "price_change_3m_pct": 0.2, "close_position": 0.8,
        }
        tickers = [{"contract": "AIN_USDT", "volume_24h_quote": "1000000"}]
        with patch.object(Bot, "is_crypto_contract", return_value=True), patch.object(Bot, "closed_candles", return_value=candles), patch.object(Bot, "detect_liquidity_buildup", return_value=radar), patch.object(Bot, "is_high_quality_liquidity_candidate", return_value=True), patch.object(Bot, "detect_recent_prepump_entry", return_value=None), patch.object(Bot, "send_message") as send_message:
            Bot.scan_prepump_entries(tickers, {}, confirmed_entries=True, max_symbols=60)
        send_message.assert_not_called()


    def test_reports_same_candle_duplicate_suppression(self):
        setup = {"candle_time": "100"}
        diagnostics = {}
        reason = Bot.prepump_suppression_reason(
            {"candle_time": "100", "sent_at": 1}, setup, now=2000, diagnostics=diagnostics
        )
        self.assertEqual(reason, "duplicate_candle_suppressed")
        self.assertEqual(diagnostics.get("duplicate_candle_suppressed"), 1)

    def test_reports_recent_setup_cooldown_suppression(self):
        setup = {"candle_time": "200"}
        diagnostics = {}
        reason = Bot.prepump_suppression_reason(
            {"candle_time": "100", "sent_at": 1500}, setup, now=2000, diagnostics=diagnostics
        )
        self.assertEqual(reason, "cooldown_suppressed")
        self.assertEqual(diagnostics.get("cooldown_suppressed"), 1)

    def test_allows_new_setup_after_cooldown(self):
        setup = {"candle_time": "200"}
        reason = Bot.prepump_suppression_reason(
            {"candle_time": "100", "sent_at": 100}, setup, now=3000
        )
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
