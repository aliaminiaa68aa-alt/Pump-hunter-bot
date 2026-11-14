import unittest
from unittest.mock import patch

from oi_engine.signal_engine import MarketDataError
from oi_engine.liquidity_sweep_engine import evaluate_liquidity_sweep
from oi_engine.watchlist_engine import (
    DATA_ERROR_RETRY_SECONDS, scan_watchlist, format_confirmed_signal,
    signal_price_geometry_rejection, coinalyze_rejection_reasons,
)


def make_candles(side="LONG"):
    rows = []
    for i in range(30):
        rows.append({
            "open_time": i * 900000,
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
            "volume": 100.0, "quote_volume": 100.0,
            "taker_buy_quote": 60.0, "close_time": (i + 1) * 900000 - 1,
        })
    if side == "LONG":
        rows[12]["high"] = 102.0
        rows[20]["high"] = 103.0
    else:
        rows[12]["low"] = 98.0
        rows[20]["low"] = 97.0
    last = rows[-1]
    if side == "LONG":
        last.update({"open": 99.5, "high": 101.0, "low": 98.0,
                     "close": 100.5, "quote_volume": 180.0,
                     "taker_buy_quote": 120.0})
    else:
        last.update({"open": 100.5, "high": 102.0, "low": 99.0,
                     "close": 99.5, "quote_volume": 180.0,
                     "taker_buy_quote": 60.0})
    return rows


def make_snapshot(side="LONG"):
    return {"symbol": "ABCUSDT", "candles": make_candles(side), "live_price": make_candles(side)[-1]["close"]}


def make_metrics(side="LONG"):
    return {
        "oi_change_15m_window_pct": 0.8,
        "taker_buy_ratio": 0.67 if side == "LONG" else 0.33,
        "funding_rate": 0.0001,
        "derivatives_candle_open_time_ms": 29 * 900000,
    }


class FakeClient:
    def __init__(self, metrics):
        self.metrics = metrics

    def fetch_metrics(self, symbol, now=None):
        return dict(self.metrics)


class LiquiditySweepWatchlistTests(unittest.TestCase):
    def setUp(self):
        self.trend_direction = "LONG"
        self._trend_patcher = patch(
            "oi_engine.watchlist_engine.analyze_trend_1h",
            side_effect=lambda symbol: {"direction": self.trend_direction},
        )
        self._higher_trend_patcher = patch(
            "oi_engine.watchlist_engine.analyze_trend_4h_1d",
            return_value={
                "4h": {"direction": "LONG"},
                "1d": {"direction": "LONG"},
            },
        )
        self._confirm_patcher = patch(
            "oi_engine.watchlist_engine.confirm_trend_15m", return_value=True
        )
        self._trend_patcher.start()
        self._higher_trend_patcher.start()
        self._confirm_patcher.start()
        self.addCleanup(self._trend_patcher.stop)
        self.addCleanup(self._higher_trend_patcher.stop)
        self.addCleanup(self._confirm_patcher.stop)

    def test_funding_is_context_only_and_does_not_veto_long_or_short(self):
        long_signal = type("SignalStub", (), {"side": "LONG"})()
        short_signal = type("SignalStub", (), {"side": "SHORT"})()
        long_metrics = {"oi_change_15m_window_pct": 0.8, "taker_buy_ratio": 0.67, "funding_rate": 0.02}
        short_metrics = {"oi_change_15m_window_pct": 0.8, "taker_buy_ratio": 0.33, "funding_rate": -0.02}
        self.assertEqual(coinalyze_rejection_reasons(long_signal, long_metrics), [])
        self.assertEqual(coinalyze_rejection_reasons(short_signal, short_metrics), [])

    def test_missing_funding_does_not_veto_valid_oi_and_flow(self):
        long_signal = type("SignalStub", (), {"side": "LONG"})()
        metrics = {"oi_change_15m_window_pct": 0.8, "taker_buy_ratio": 0.67}
        self.assertEqual(coinalyze_rejection_reasons(long_signal, metrics), [])

    def test_scan_watchlist_applies_minimum_oi_confirmation_to_long_sweep(self):
        from unittest.mock import Mock

        signal = type("SignalStub", (), {"side": "LONG"})()
        metrics = {**make_metrics("LONG"), "oi_change_15m_window_pct": 0.29}
        evaluator = Mock(return_value=(signal, "test sweep/reclaim"))
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value=make_snapshot("LONG")), \
             patch("oi_engine.watchlist_engine.evaluate_liquidity_sweep", evaluator):
            found, updated = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(metrics), now=1000
            )
        self.assertEqual(found, [])
        self.assertIn("حداقل +0.30٪", updated["ABCUSDT"]["reason"])
        evaluator.assert_called_once()

    def test_scan_watchlist_applies_minimum_sell_flow_to_short_sweep(self):
        from unittest.mock import Mock

        self.trend_direction = "SHORT"
        signal = type("SignalStub", (), {"side": "SHORT"})()
        metrics = {**make_metrics("SHORT"), "taker_buy_ratio": 0.46}
        evaluator = Mock(return_value=(signal, "test sweep/reclaim"))
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value=make_snapshot("SHORT")), \
             patch("oi_engine.watchlist_engine.evaluate_liquidity_sweep", evaluator):
            found, updated = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(metrics), now=1000
            )
        self.assertEqual(found, [])
        self.assertIn("حداقل 55٪", updated["ABCUSDT"]["reason"])
        evaluator.assert_called_once()

    def test_long_requires_sweep_reclaim_and_bullish_flow(self):
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value=make_snapshot("LONG")):
            found, updated = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(make_metrics("LONG")), now=1000
            )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0].side, "LONG")
        self.assertEqual(updated["ABCUSDT"]["status"], "سیگنال LONG آماده ارسال")
        self.assertIn("Liquidity Sweep LONG", updated["ABCUSDT"]["reason"])

    def test_short_requires_sweep_reclaim_and_bearish_flow(self):
        self.trend_direction = "SHORT"
        self._higher_trend_patcher.stop()
        self._higher_trend_patcher = patch(
            "oi_engine.watchlist_engine.analyze_trend_4h_1d",
            return_value={"4h": {"direction": "SHORT"}, "1d": {"direction": "SHORT"}},
        )
        self._higher_trend_patcher.start()
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value=make_snapshot("SHORT")):
            found, updated = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(make_metrics("SHORT")), now=1000
            )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0].side, "SHORT")
        self.assertEqual(updated["ABCUSDT"]["status"], "سیگنال SHORT آماده ارسال")

    def test_long_accepts_recent_sweep_with_next_candle_confirmation(self):
        candles = make_candles("LONG")
        candles[-2].update({
            "open": 99.5, "high": 101.0, "low": 98.0, "close": 100.5,
            "quote_volume": 180.0, "taker_buy_quote": 120.0,
        })
        candles[-1].update({
            "open": 100.5, "high": 101.8, "low": 100.2, "close": 101.4,
            "quote_volume": 160.0, "taker_buy_quote": 110.0,
        })
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value={"symbol": "ABCUSDT", "candles": candles, "live_price": candles[-1]["close"]}):
            found, updated = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(make_metrics("LONG")), now=1000
            )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0].side, "LONG")
        self.assertIn("1 کندل قبل", updated["ABCUSDT"]["reason"])

    def test_short_accepts_recent_sweep_with_next_candle_confirmation(self):
        self.trend_direction = "SHORT"
        self._higher_trend_patcher.stop()
        self._higher_trend_patcher = patch(
            "oi_engine.watchlist_engine.analyze_trend_4h_1d",
            return_value={"4h": {"direction": "SHORT"}, "1d": {"direction": "SHORT"}},
        )
        self._higher_trend_patcher.start()
        candles = make_candles("SHORT")
        candles[-2].update({
            "open": 100.5, "high": 102.0, "low": 99.0, "close": 99.5,
            "quote_volume": 180.0, "taker_buy_quote": 60.0,
        })
        candles[-1].update({
            "open": 99.5, "high": 99.8, "low": 98.2, "close": 98.7,
            "quote_volume": 160.0, "taker_buy_quote": 50.0,
        })
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value={"symbol": "ABCUSDT", "candles": candles, "live_price": candles[-1]["close"]}):
            found, updated = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(make_metrics("SHORT")), now=1000
            )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0].side, "SHORT")
        self.assertIn("1 کندل قبل", updated["ABCUSDT"]["reason"])

    def test_long_accepts_sweep_four_closed_candles_ago_with_latest_confirmation(self):
        candles = make_candles("LONG")
        candles[-4].update({
            "open": 99.5, "high": 101.0, "low": 98.0, "close": 100.5,
            "quote_volume": 180.0, "taker_buy_quote": 120.0,
        })
        candles[-1].update({
            "open": 100.5, "high": 101.8, "low": 100.2, "close": 101.4,
            "quote_volume": 160.0, "taker_buy_quote": 110.0,
        })
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value={"symbol": "ABCUSDT", "candles": candles, "live_price": candles[-1]["close"]}):
            found, updated = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(make_metrics("LONG")), now=1000
            )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0].side, "LONG")
        self.assertIn("3 کندل قبل", updated["ABCUSDT"]["reason"])

    def test_low_volume_sweep_is_not_sent_as_entry(self):
        for side in ("LONG", "SHORT"):
            self.trend_direction = side
            with self.subTest(side=side):
                snapshot = make_snapshot(side)
                snapshot["candles"][-1]["quote_volume"] = 40.0
                with patch("oi_engine.watchlist_engine.fetch_snapshot",
                           return_value=snapshot):
                    found, updated = scan_watchlist(
                        {"ABCUSDT": {}}, FakeClient(make_metrics(side)), now=1000
                    )
                self.assertEqual(found, [])
                self.assertEqual(updated["ABCUSDT"]["status"], "در حال رصد نقدینگی")
                self.assertIn("حجم کندل فقط", updated["ABCUSDT"]["reason"])

    def test_one_candle_coinalyze_lag_aligns_binance_price_history(self):
        from unittest.mock import Mock

        metrics = {
            **make_metrics("LONG"),
            "derivatives_candle_open_time_ms": 28 * 900000,
        }
        signal = type("SignalStub", (), {
            "side": "LONG",
            "entry": 100.0,
            "stop_loss": 99.0,
            "take_profit_1": 102.0,
            "take_profit_2": 103.0,
        })()
        evaluator = Mock(return_value=(signal, "test sweep/reclaim"))
        snapshot = make_snapshot("LONG")
        with patch("oi_engine.watchlist_engine.fetch_snapshot", return_value=snapshot):
            with patch("oi_engine.watchlist_engine.evaluate_liquidity_sweep", evaluator):
                with patch("oi_engine.watchlist_engine.confirm_trend_15m", return_value=True) as confirm:
                    found, updated = scan_watchlist(
                        {"ABCUSDT": {}}, FakeClient(metrics), now=1000
                    )

        self.assertEqual(len(found), 1)
        aligned_snapshot = evaluator.call_args.args[0]
        self.assertEqual(len(aligned_snapshot["candles"]), 29)
        self.assertEqual(aligned_snapshot["candles"][-1]["open_time"], 28 * 900000)
        confirm.assert_called_once_with(aligned_snapshot["candles"], "LONG")
        self.assertEqual(updated["ABCUSDT"]["price_derivatives_candle_lag_minutes"], 15)

    def test_two_candle_coinalyze_lag_is_rejected(self):
        from unittest.mock import Mock

        metrics = {
            **make_metrics("LONG"),
            "derivatives_candle_open_time_ms": 27 * 900000,
        }
        evaluator = Mock()
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value=make_snapshot("LONG")):
            with patch("oi_engine.watchlist_engine.evaluate_liquidity_sweep", evaluator):
                found, updated = scan_watchlist(
                    {"ABCUSDT": {}}, FakeClient(metrics), now=1000
                )

        self.assertEqual(found, [])
        self.assertEqual(updated["ABCUSDT"]["status"], "خطای دریافت داده")
        self.assertIn("بیش از یک کندل", updated["ABCUSDT"]["reason"])
        evaluator.assert_not_called()

    def test_no_sweep_means_no_entry(self):
        snapshot = make_snapshot("LONG")
        snapshot["candles"][-1].update({"low": 99.2, "close": 100.5})
        with patch("oi_engine.watchlist_engine.fetch_snapshot", return_value=snapshot):
            found, updated = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(make_metrics("LONG")), now=1000
            )
        self.assertEqual(found, [])
        self.assertEqual(updated["ABCUSDT"]["status"], "در حال رصد نقدینگی")

    def test_wrong_order_flow_blocks_sweep(self):
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value=make_snapshot("LONG")):
            found, updated = scan_watchlist(
                {"ABCUSDT": {}},
                FakeClient({**make_metrics("LONG"), "taker_buy_ratio": 0.48}),
                now=1000,
            )
        self.assertEqual(found, [])
        self.assertIn("جریان خرید", updated["ABCUSDT"]["reason"])

    def test_binance_price_failure_fails_closed_and_never_uses_coinalyze_snapshot(self):
        error = MarketDataError("Binance unavailable")
        fallback = {**make_metrics("LONG"), "market_snapshot": make_snapshot("LONG")}
        with patch("oi_engine.watchlist_engine.fetch_snapshot", side_effect=error):
            found, updated = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(fallback), now=1000
            )
        self.assertEqual(found, [])
        self.assertEqual(updated["ABCUSDT"]["status"], "خطای دریافت داده")
        self.assertNotIn("coinalyze", updated["ABCUSDT"])

    def test_data_errors_are_marked_checked_and_cooled_down_then_cleared(self):
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   side_effect=MarketDataError("temporary provider outage")):
            _, failed = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(make_metrics()), now=1000
            )
        failed_record = failed["ABCUSDT"]
        self.assertEqual(failed_record["last_attempted_at"], 1000)
        self.assertEqual(failed_record["last_checked"], 1000)
        self.assertEqual(
            failed_record["retry_after"], 1000 + DATA_ERROR_RETRY_SECONDS
        )

        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value=make_snapshot("LONG")):
            _, recovered = scan_watchlist(
                {"ABCUSDT": failed_record},
                FakeClient(make_metrics("LONG")),
                now=1000 + DATA_ERROR_RETRY_SECONDS,
            )
        self.assertEqual(
            recovered["ABCUSDT"]["last_checked"],
            1000 + DATA_ERROR_RETRY_SECONDS,
        )
        self.assertNotIn("retry_after", recovered["ABCUSDT"])

    def test_sweep_is_rejected_when_nearest_structural_target_is_below_two_r(self):
        candles = make_candles("LONG")
        candles[12]["high"] = 101.2
        candles[20]["high"] = 102.0
        signal, reason = evaluate_liquidity_sweep(
            {"symbol": "ABCUSDT", "candles": candles, "live_price": 100.5},
            make_metrics("LONG"),
        )
        self.assertIsNone(signal)
        self.assertIn("2.00R", reason)

    def test_signal_price_geometry_rejects_sub_two_r_for_both_sides(self):
        cases = (
            ("LONG", 100.0, 99.0, 101.0, 102.0),
            ("SHORT", 100.0, 101.0, 99.0, 98.0),
        )
        for side, entry, stop, tp1, tp2 in cases:
            with self.subTest(side=side):
                signal = type("SignalStub", (), {
                    "side": side, "entry": entry, "stop_loss": stop,
                    "take_profit_1": tp1, "take_profit_2": tp2,
                })()
                reason = signal_price_geometry_rejection(signal, min_tp1_distance_pct=0.0)
                self.assertIn("2.00R", reason)

    def test_oi_alert_long_never_emits_short_even_when_short_sweep_is_present(self):
        snapshot = make_snapshot("SHORT")
        signal, reason = evaluate_liquidity_sweep(
            snapshot, make_metrics("SHORT"), required_side="LONG"
        )
        self.assertIsNone(signal)
        self.assertIn("جهت هشدار منبع LONG", reason)

    def test_oi_alert_short_never_emits_long_even_when_long_sweep_is_present(self):
        snapshot = make_snapshot("LONG")
        signal, reason = evaluate_liquidity_sweep(
            snapshot, make_metrics("LONG"), required_side="SHORT"
        )
        self.assertIsNone(signal)
        self.assertIn("جهت هشدار منبع SHORT", reason)

    def test_oi_channel_alert_without_parsed_side_fails_closed(self):
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value=make_snapshot("LONG")):
            found, updated = scan_watchlist(
                {"ABCUSDT": {"source_type": "oi_channel", "source_side": None}},
                FakeClient(make_metrics("LONG")), now=1000
            )
        self.assertEqual(found, [])
        self.assertEqual(updated["ABCUSDT"]["status"], "هشدار بدون جهت معتبر؛ سیگنال صادر نشد")

    def test_both_higher_timeframes_opposing_signal_block_entry(self):
        self._higher_trend_patcher.stop()
        self._higher_trend_patcher = patch(
            "oi_engine.watchlist_engine.analyze_trend_4h_1d",
            return_value={"4h": {"direction": "SHORT"}, "1d": {"direction": "SHORT"}},
        )
        self._higher_trend_patcher.start()
        with patch("oi_engine.watchlist_engine.fetch_snapshot", return_value=make_snapshot("LONG")):
            found, updated = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(make_metrics("LONG")), now=1000
            )
        self.assertEqual(found, [])
        self.assertIn("هر دو تایم‌فریم بالاتر", updated["ABCUSDT"]["reason"])

    def test_signal_levels_have_correct_long_and_short_geometry(self):
        self.trend_direction = "LONG"
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value=make_snapshot("LONG")):
            long_found, _ = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(make_metrics("LONG")), now=1000
            )
        self.trend_direction = "SHORT"
        self._higher_trend_patcher.stop()
        self._higher_trend_patcher = patch(
            "oi_engine.watchlist_engine.analyze_trend_4h_1d",
            return_value={"4h": {"direction": "SHORT"}, "1d": {"direction": "SHORT"}},
        )
        self._higher_trend_patcher.start()
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value=make_snapshot("SHORT")):
            short_found, _ = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(make_metrics("SHORT")), now=1000
            )
        self.assertIsNone(signal_price_geometry_rejection(long_found[0][0], 0.0))
        self.assertIsNone(signal_price_geometry_rejection(short_found[0][0], 0.0))
        long_rr = (
            (long_found[0][0].take_profit_1 - long_found[0][0].entry)
            / (long_found[0][0].entry - long_found[0][0].stop_loss)
        )
        short_rr = (
            (short_found[0][0].entry - short_found[0][0].take_profit_1)
            / (short_found[0][0].stop_loss - short_found[0][0].entry)
        )
        self.assertGreaterEqual(long_rr, 2.0)
        self.assertGreaterEqual(short_rr, 2.0)

    def test_telegram_card_contains_strategy_and_actionable_levels(self):
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value=make_snapshot("LONG")):
            found, _ = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(make_metrics("LONG")), now=1000
            )
        message = format_confirmed_signal(found[0][0], found[0][1])
        self.assertIn("<b>LONG</b>", message)
        self.assertIn("ABCUSDT", message)
        self.assertIn("Coinalyze + Price Action + Liquidity Sweep", message)
        self.assertIn("ورود:", message)
        self.assertIn("قیمت فعلی", message)
        self.assertIn("100.5", message)
        self.assertIn("فاصله تا ورود:", message)
        self.assertIn("ساعت ایران:", message)
        self.assertIn("حد ضرر:", message)
        self.assertIn("هدف ۱:", message)
        self.assertIn("هدف ۲:", message)
        self.assertIn("OI ۱۵دقیقه:", message)
        self.assertIn("Funding API خام:", message)
        self.assertIn("0.0001", message)
        self.assertIn("جریان", message)
        self.assertIn("شکار نقدینگی", message)


if __name__ == "__main__":
    unittest.main()
