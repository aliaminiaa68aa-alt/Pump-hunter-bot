import unittest
from unittest.mock import patch

from oi_engine.signal_engine import MarketDataError
from oi_engine.watchlist_engine import (
    scan_watchlist, format_confirmed_signal, signal_price_geometry_rejection,
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

    def test_mismatched_coinalyze_candle_is_rejected(self):
        metrics = {
            **make_metrics("LONG"),
            "derivatives_candle_open_time_ms": 28 * 900000,
        }
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value=make_snapshot("LONG")):
            found, updated = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(metrics), now=1000
            )
        self.assertEqual(found, [])
        self.assertEqual(updated["ABCUSDT"]["status"], "خطای دریافت داده")
        self.assertIn("زمان کندل OI/جریان", updated["ABCUSDT"]["reason"])

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

    def test_signal_levels_have_correct_long_and_short_geometry(self):
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value=make_snapshot("LONG")):
            long_found, _ = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(make_metrics("LONG")), now=1000
            )
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   return_value=make_snapshot("SHORT")):
            short_found, _ = scan_watchlist(
                {"ABCUSDT": {}}, FakeClient(make_metrics("SHORT")), now=1000
            )
        self.assertIsNone(signal_price_geometry_rejection(long_found[0][0], 0.0))
        self.assertIsNone(signal_price_geometry_rejection(short_found[0][0], 0.0))
        self.assertAlmostEqual(
            (long_found[0][0].take_profit_1 - long_found[0][0].entry)
            / (long_found[0][0].entry - long_found[0][0].stop_loss), 1.0
        )

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
        self.assertIn("حد ضرر:", message)
        self.assertIn("هدف ۱:", message)
        self.assertIn("هدف ۲:", message)
        self.assertIn("OI ۱۵دقیقه:", message)
        self.assertIn("جریان", message)
        self.assertIn("شکار نقدینگی", message)


if __name__ == "__main__":
    unittest.main()
