import unittest

from oi_engine.pullback_engine import advance_pullback_setup


def snapshot(side="LONG"):
    candles = []
    for i in range(60):
        candles.append({
            "open_time": i * 900000, "open": 99.8, "high": 100.5,
            "low": 99.5, "close": 100.0, "volume": 1000,
            "quote_volume": 100000, "taker_buy_quote": 50000,
            "close_time": (i + 1) * 900000 - 1,
        })
    last = candles[-1]
    if side == "LONG":
        last.update({"open":101, "high":104, "low":100.8, "close":103,
                     "quote_volume":200000, "taker_buy_quote":120000})
    else:
        last.update({"open":99, "high":99.2, "low":96, "close":97,
                     "quote_volume":200000, "taker_buy_quote":80000})
    candles[-2]["close"] = 100
    if side == "LONG":
        # Give the fixture a credible multi-candle bullish flow before breakout.
        for candle in candles[-4:-1]:
            candle.update({"open":100.0, "high":100.5, "low":99.9,
                           "close":100.2, "quote_volume":100000,
                           "taker_buy_quote":60000})
    return {"symbol":"TESTUSDT", "candles":candles,
            "oi_values":[100,100.2,101],
            "oi_timestamp":candles[-1]["close_time"], "funding_rate":0.0001}


def add_retest(data, side):
    candles = data["candles"]
    prev_close_time = candles[-1]["close_time"]
    if side == "LONG":
        row = {"open_time":prev_close_time+1,"open":100.5,"high":100.9,"low":100.4,
               "close":100.85,"volume":1500,"quote_volume":200000,
               "taker_buy_quote":120000,"close_time":prev_close_time+900000}
    else:
        row = {"open_time":prev_close_time+1,"open":99.9,"high":100.0,"low":99.3,
               "close":99.3,"volume":1500,"quote_volume":200000,
               "taker_buy_quote":80000,"close_time":prev_close_time+900000}
    data["candles"].append(row)
    data["oi_values"] = [100.2,101.0,101.8]
    data["oi_timestamp"] = row["close_time"]
    return data


class PullbackEngineTests(unittest.TestCase):
    def test_channel_direction_mismatch_blocks_breakout(self):
        data = snapshot("LONG")
        signal, pending, reason = advance_pullback_setup(data, required_side="SHORT")
        self.assertIsNone(signal)
        self.assertIsNone(pending)
        self.assertIn("با جهت کانال", reason)

    def test_channel_direction_match_allows_pending_breakout(self):
        data = snapshot("SHORT")
        signal, pending, _ = advance_pullback_setup(data, required_side="SHORT")
        self.assertIsNone(signal)
        self.assertEqual(pending["side"], "SHORT")

    def test_breakout_creates_pending_setup_not_signal(self):
        data = snapshot("LONG")
        signal, pending, reason = advance_pullback_setup(data)
        self.assertIsNone(signal)
        self.assertEqual(pending["side"], "LONG")
        self.assertIn("منتظر برگشت", reason)

    def test_long_only_signals_after_retest_confirmation(self):
        data = snapshot("LONG")
        _, pending, _ = advance_pullback_setup(data)
        add_retest(data, "LONG")
        signal, next_pending, reason = advance_pullback_setup(data, pending)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "LONG")
        self.assertIsNone(next_pending)
        self.assertLess(signal.stop_loss, signal.entry)
        self.assertGreater(signal.take_profit_2, signal.take_profit_1)
        # Entry is a resting limit near the broken structure, not the
        # confirmation candle close (which would usually fill next-bar market).
        self.assertLess(signal.entry, data["candles"][-1]["close"])
        self.assertAlmostEqual(
            signal.entry,
            signal.setup_level + signal.atr_15m * 0.02,
        )
        self.assertGreater(signal.atr_15m, 0)
        self.assertGreater(signal.pullback_extreme, 0)
        self.assertGreater(signal.setup_level, 0)
        self.assertGreater(signal.ema20, signal.ema50)
        self.assertGreater(signal.stop_distance_atr, 0)
        self.assertIn("تأیید شدند", reason)

    def test_short_only_signals_after_retest_confirmation(self):
        data = snapshot("SHORT")
        _, pending, _ = advance_pullback_setup(data)
        add_retest(data, "SHORT")
        signal, next_pending, _ = advance_pullback_setup(data, pending)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "SHORT")
        self.assertIsNone(next_pending)
        self.assertGreater(signal.stop_loss, signal.entry)
        self.assertLess(signal.take_profit_2, signal.take_profit_1)
        self.assertGreater(signal.entry, data["candles"][-1]["close"])
        self.assertAlmostEqual(
            signal.entry,
            signal.setup_level - signal.atr_15m * 0.02,
        )

    def test_long_retest_extreme_is_recovered_when_intermediate_candle_was_not_scanned(self):
        data = snapshot("LONG")
        _, pending, _ = advance_pullback_setup(data)
        # Two closed candles pass between scans. The first sweeps and confirms
        # the rejection; the latest candle is indecisive and must not erase it.
        first_time = data["candles"][-1]["close_time"]
        data["candles"].extend([
            {"open_time": first_time + 1, "open":100.6, "high":100.95,
             "low":100.35, "close":100.9, "volume":1200,
             "quote_volume":150000, "taker_buy_quote":90000,
             "close_time":first_time + 900000},
            # The latest candle is deliberately indecisive: the valid rejection
            # happened in the previous candle while this symbol was not scanned.
            {"open_time": first_time + 900001, "open":100.94, "high":101.0,
             "low":100.75, "close":100.98, "volume":1500,
             "quote_volume":200000, "taker_buy_quote":100000,
             "close_time":first_time + 1800000},
        ])
        data["oi_values"] = [100.2, 101.0, 101.8]
        signal, next_pending, reason = advance_pullback_setup(data, pending)
        self.assertIsNotNone(signal, reason)
        self.assertIsNone(next_pending)
        self.assertAlmostEqual(signal.pullback_extreme, 100.35)

    def test_long_two_candle_follow_through_confirms_moderate_first_rejection(self):
        data = snapshot("LONG")
        _, pending, _ = advance_pullback_setup(data)
        add_retest(data, "LONG")
        first = data["candles"][-1]
        first.update({"open":100.5, "high":100.7, "low":100.4, "close":100.56,
                      "quote_volume":180000, "taker_buy_quote":108000})
        previous = first["close_time"]
        data["candles"].append({
            "open_time":previous+1, "open":100.68, "high":100.9,
            "low":100.4, "close":100.75, "volume":1500,
            "quote_volume":200000, "taker_buy_quote":125000,
            "close_time":previous+900000,
        })
        data["oi_values"] = [100.2,101.0,101.8]
        data["oi_timestamp"] = data["candles"][-1]["close_time"]
        signal, next_pending, reason = advance_pullback_setup(data, pending)
        self.assertIsNotNone(signal, reason)
        self.assertEqual(signal.side, "LONG")
        self.assertIsNone(next_pending)

    def test_short_two_candle_follow_through_confirms_moderate_first_rejection(self):
        data = snapshot("SHORT")
        _, pending, _ = advance_pullback_setup(data)
        add_retest(data, "SHORT")
        first = data["candles"][-1]
        first.update({"open":99.55, "high":99.9, "low":99.3, "close":99.45,
                      "quote_volume":180000, "taker_buy_quote":72000})
        previous = first["close_time"]
        data["candles"].append({
            "open_time":previous+1, "open":99.34, "high":99.9,
            "low":98.9, "close":99.2, "volume":1500,
            "quote_volume":200000, "taker_buy_quote":75000,
            "close_time":previous+900000,
        })
        data["oi_values"] = [100.2,101.0,101.8]
        data["oi_timestamp"] = data["candles"][-1]["close_time"]
        signal, next_pending, reason = advance_pullback_setup(data, pending)
        self.assertIsNotNone(signal, reason)
        self.assertEqual(signal.side, "SHORT")
        self.assertIsNone(next_pending)

    def test_long_limit_entry_must_be_below_confirmation_close(self):
        data = snapshot("LONG")
        _, pending, _ = advance_pullback_setup(data)
        add_retest(data, "LONG")
        data["candles"][-1].update({
            "open":100.505, "high":100.525, "low":100.5,
            "close":100.52, "quote_volume":200000,
            "taker_buy_quote":120000,
        })
        signal, next_pending, reason = advance_pullback_setup(data, pending)
        self.assertIsNone(signal)
        self.assertIsNotNone(next_pending)
        self.assertIn("ورود محدود", reason)

    def test_short_limit_entry_must_be_above_confirmation_close(self):
        data = snapshot("SHORT")
        _, pending, _ = advance_pullback_setup(data)
        add_retest(data, "SHORT")
        data["candles"][-1].update({
            "open":99.495, "high":99.5, "low":99.47,
            "close":99.479, "quote_volume":200000,
            "taker_buy_quote":80000,
        })
        signal, next_pending, reason = advance_pullback_setup(data, pending)
        self.assertIsNone(signal)
        self.assertIsNotNone(next_pending)
        self.assertIn("ورود محدود", reason)

    def test_weak_order_flow_keeps_setup_alive_for_next_retest(self):
        data = snapshot("LONG")
        _, pending, _ = advance_pullback_setup(data)
        add_retest(data, "LONG")
        data["candles"][-1]["taker_buy_quote"] = 94000
        signal, next_pending, reason = advance_pullback_setup(data, pending)
        self.assertIsNone(signal)
        self.assertEqual({k: v for k, v in next_pending.items() if k != "pullback_extreme"}, pending)
        self.assertAlmostEqual(next_pending["pullback_extreme"], 100.4)
        self.assertIn("setup", reason)

    def test_short_is_blocked_when_recent_closed_candles_show_buy_pressure(self):
        data = snapshot("SHORT")
        _, pending, _ = advance_pullback_setup(data)
        # After the bearish breakout, buyers dominate the next three closed
        # candles. The final candle alone must not override that pressure.
        rows = [
            {"open":97.0,"high":99.9,"low":96.8,"close":99.7,
             "quote_volume":300000,"taker_buy_quote":210000},
            {"open":99.7,"high":100.0,"low":99.5,"close":100.0,
             "quote_volume":250000,"taker_buy_quote":175000},
            {"open":100.0,"high":99.8,"low":99.2,"close":99.3,
             "quote_volume":200000,"taker_buy_quote":90000},
        ]
        for row in rows:
            previous = data["candles"][-1]["close_time"]
            data["candles"].append({
                **row, "open_time":previous+1,
                "volume":1500, "close_time":previous+900000,
            })
        data["oi_values"] = [100.2,101.0,101.8]
        data["oi_timestamp"] = data["candles"][-1]["close_time"]
        signal, next_pending, reason = advance_pullback_setup(data, pending)
        self.assertIsNone(signal)
        self.assertIsNotNone(next_pending)
        self.assertIn("جریان تجمیعی", reason)

    def test_long_is_blocked_when_recent_closed_candles_show_sell_pressure(self):
        data = snapshot("LONG")
        _, pending, _ = advance_pullback_setup(data)
        rows = [
            {"open":103.0,"high":103.2,"low":100.0,"close":100.4,
             "quote_volume":300000,"taker_buy_quote":90000},
            {"open":100.4,"high":100.6,"low":99.95,"close":100.0,
             "quote_volume":250000,"taker_buy_quote":75000},
            {"open":100.0,"high":100.8,"low":99.95,"close":100.7,
             "quote_volume":200000,"taker_buy_quote":120000},
        ]
        for row in rows:
            previous = data["candles"][-1]["close_time"]
            data["candles"].append({
                **row, "open_time":previous+1,
                "volume":1500, "close_time":previous+900000,
            })
        data["oi_values"] = [100.2,101.0,101.8]
        data["oi_timestamp"] = data["candles"][-1]["close_time"]
        signal, next_pending, reason = advance_pullback_setup(data, pending)
        self.assertIsNone(signal)
        self.assertIsNotNone(next_pending)
        self.assertIn("جریان تجمیعی", reason)

    def test_weak_oi_keeps_setup_alive_for_next_retest(self):
        data = snapshot("LONG")
        _, pending, _ = advance_pullback_setup(data)
        add_retest(data, "LONG")
        data["oi_values"] = [100.0, 100.0, 100.04]
        signal, next_pending, reason = advance_pullback_setup(data, pending)
        self.assertIsNone(signal)
        self.assertEqual({k: v for k, v in next_pending.items() if k != "pullback_extreme"}, pending)
        self.assertAlmostEqual(next_pending["pullback_extreme"], 100.4)
        self.assertIn("حفظ شد", reason)

    def test_weak_retest_volume_keeps_setup_alive_for_next_retest(self):
        data = snapshot("LONG")
        _, pending, _ = advance_pullback_setup(data)
        add_retest(data, "LONG")
        data["candles"][-1]["quote_volume"] = 80000
        data["candles"][-1]["taker_buy_quote"] = 60000
        signal, next_pending, reason = advance_pullback_setup(data, pending)
        self.assertIsNone(signal)
        self.assertEqual({k: v for k, v in next_pending.items() if k != "pullback_extreme"}, pending)
        self.assertAlmostEqual(next_pending["pullback_extreme"], 100.4)
        self.assertIn("حفظ شد", reason)


    def test_long_stop_uses_lowest_low_across_pullback_candles(self):
        data = snapshot("LONG")
        _, pending, _ = advance_pullback_setup(data)
        add_retest(data, "LONG")
        first = data["candles"][-1]
        first.update({"low":99.95, "close":100.7, "open":100.6,
                      "taker_buy_quote":100000})
        _, pending, _ = advance_pullback_setup(data, pending)
        self.assertAlmostEqual(pending["pullback_extreme"], 99.95)
        add_retest(data, "LONG")
        data["candles"][-1].update({"low":100.35, "high":100.9, "open":100.5, "close":100.85,
                                    "taker_buy_quote":120000})
        signal, next_pending, _ = advance_pullback_setup(data, pending)
        self.assertIsNotNone(signal)
        self.assertIsNone(next_pending)
        self.assertAlmostEqual(signal.stop_loss, 99.95 - signal.atr_15m * 0.35)

    def test_short_stop_uses_highest_high_across_pullback_candles(self):
        data = snapshot("SHORT")
        _, pending, _ = advance_pullback_setup(data)
        add_retest(data, "SHORT")
        first = data["candles"][-1]
        first.update({"high":100.05, "open":99.9, "close":99.4,
                      "taker_buy_quote":100000})
        _, pending, _ = advance_pullback_setup(data, pending)
        self.assertAlmostEqual(pending["pullback_extreme"], 100.05)
        add_retest(data, "SHORT")
        data["candles"][-1].update({"high":99.7, "open":99.9, "close":99.3,
                                    "taker_buy_quote":80000})
        signal, next_pending, _ = advance_pullback_setup(data, pending)
        self.assertIsNotNone(signal)
        self.assertIsNone(next_pending)
        self.assertAlmostEqual(signal.stop_loss, 100.05 + signal.atr_15m * 0.35)

    def test_setup_expires_after_four_15m_candles(self):
        data = snapshot("LONG")
        _, pending, _ = advance_pullback_setup(data)
        data["candles"].append(dict(data["candles"][-1],
            open_time=data["candles"][-1]["close_time"]+1,
            close_time=pending["expires_at_close_time"]+1))
        signal, next_pending, reason = advance_pullback_setup(data, pending)
        self.assertIsNone(signal)
        self.assertIsNone(next_pending)
        self.assertIn("منقضی", reason)


if __name__ == "__main__":
    unittest.main()
