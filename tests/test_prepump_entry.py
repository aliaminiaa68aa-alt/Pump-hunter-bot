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
        # Baseline candles: quiet price action and normal turnover.
        for i in range(20):
            base = 100.0 + (i % 3) * 0.01
            one_minute.append(candle(i, base, base + 0.08, base - 0.10, base + 0.02, 100))
        # Five prior candles define the local breakout high and nearby stop.
        for i in range(5):
            base = 100.02 + i * 0.005
            one_minute.append(candle(20 + i, base, 100.15, 99.92, 100.08, 110))
        one_minute.append(candle(25, 100.10, 100.48, 100.08, 100.40, 220))

        five_minute = []
        for i in range(13):
            five_minute.append(candle(100 + i, 100.0, 100.25, 99.85, 100.05, 100))
        for i in range(6):
            five_minute.append(candle(113 + i, 100.02, 100.35, 99.90, 100.20, 130))
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
        one_minute[-1]["c"] = "100.10"
        one_minute[-1]["h"] = "100.20"
        self.assertIsNone(Bot.detect_prepump_entry(one_minute, five_minute))

    def test_diagnostics_prioritize_missing_breakout_over_low_volume(self):
        one_minute, five_minute = self.make_valid_setup()
        one_minute[-1]["c"] = "100.10"
        one_minute[-1]["h"] = "100.20"
        for candle_5m in five_minute[-6:]:
            candle_5m["sum"] = "10"
        diagnostics = {}
        result = Bot.detect_prepump_entry(one_minute, five_minute, diagnostics)
        self.assertIsNone(result)
        self.assertEqual(diagnostics.get("no_1m_breakout"), 1)
        self.assertNotIn("low_5m_turnover_below_0.80x", diagnostics)

    def test_rejects_excessive_price_extension(self):
        one_minute, five_minute = self.make_valid_setup()
        one_minute[-1]["c"] = "103.00"
        one_minute[-1]["h"] = "103.10"
        one_minute[-1]["sum"] = "1000"
        self.assertIsNone(Bot.detect_prepump_entry(one_minute, five_minute))

    def test_refreshes_entry_and_targets_at_latest_close(self):
        one_minute, five_minute = self.make_valid_setup()
        setup = Bot.detect_prepump_entry(one_minute, five_minute)
        latest_close = setup["entry"] * 1.002
        refreshed = Bot.refresh_prepump_entry(setup, latest_close)
        self.assertIsNotNone(refreshed)
        self.assertAlmostEqual(refreshed["entry"], latest_close)
        self.assertAlmostEqual(refreshed["tp1"], refreshed["entry"] + 1.5 * (refreshed["entry"] - refreshed["stop"]))
        self.assertAlmostEqual(refreshed["tp2"], refreshed["entry"] + 2.5 * (refreshed["entry"] - refreshed["stop"]))

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
        for candle_row in five_minute[-6:]:
            candle_row["sum"] = "85"
        result = Bot.detect_prepump_entry(one_minute, five_minute)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["turnover_ratio_1m"], 1.5)
        self.assertGreaterEqual(result["turnover_ratio_5m"], 0.80)

    def test_rejects_setup_with_very_low_5m_turnover(self):
        one_minute, five_minute = self.make_valid_setup()
        for candle_5m in five_minute[-6:]:
            candle_5m["sum"] = "50"
        diagnostics = {}
        result = Bot.detect_prepump_entry(one_minute, five_minute, diagnostics)
        self.assertIsNone(result)
        self.assertEqual(diagnostics.get("low_5m_turnover_below_0.80x"), 1)

    def test_finds_breakout_in_recent_closed_candles(self):
        one_minute, five_minute = self.make_valid_setup()
        # The breakout occurred four candles ago; newer candles no longer break out.
        for i in range(4):
            base = 100.12 + i * 0.005
            one_minute.append(candle(26 + i, base, base + 0.04, base - 0.05, base + 0.01, 100))
        current_only = Bot.detect_prepump_entry(one_minute, five_minute)
        self.assertIsNone(current_only)
        diagnostics = {}
        result = Bot.detect_recent_prepump_entry(one_minute, five_minute, lookback=5, diagnostics=diagnostics)
        self.assertIsNotNone(result)
        self.assertEqual(result["minutes_since_breakout_candle"], 4)


    def test_radar_only_candidate_never_sends_telegram_message(self):
        candles = [candle(i, 100.0, 100.2, 99.8, 100.05, 100) for i in range(31)]
        radar = {
            "candle_time": "123456", "score": 4, "turnover_1m_ratio": 2.1,
            "turnover_5m_ratio": 1.2, "compression_pct": 1.0,
            "price_change_3m_pct": 0.2,
        }
        tickers = [{"contract": "AIN_USDT", "volume_24h_quote": "1000000"}]
        with patch.object(Bot, "closed_candles", return_value=candles), \\
             patch.object(Bot, "detect_liquidity_buildup", return_value=radar), \\
             patch.object(Bot, "detect_recent_prepump_entry", return_value=None), \\
             patch.object(Bot, "send_message") as send_message:
            Bot.scan_prepump_entries(tickers, {}, confirmed_entries=True, max_symbols=60)
        send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
