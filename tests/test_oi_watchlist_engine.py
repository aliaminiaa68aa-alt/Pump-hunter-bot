import unittest
import requests
from unittest.mock import Mock
from unittest.mock import patch
from oi_engine.signal_engine import Signal
from oi_engine.watchlist_engine import coinalyze_confirms, coinalyze_rejection_reasons, scan_watchlist, format_confirmed_signal, source_oi_priority, signal_price_geometry_rejection

def signal(side="LONG"):
    return Signal("ABCUSDT",side,1,.98,1.02,1.04,.8,.3,.56,1.4,.0001,82,900000)

def metrics(taker=.60, oi=.4, complete=True):
    return {"oi_change_15m_window_pct":oi,"taker_buy_ratio":taker,"funding_rate":.0001,
            "predicted_funding_rate":.0002,"long_short_ratio":1.2,"long_liquidations":1200,
            "short_liquidations":900,"data_quality":{"complete":complete}}

class FakeClient:
    def __init__(self, value): self.value=value
    def fetch_metrics(self, symbol, now=None): return self.value

class WatchlistTests(unittest.TestCase):
    def test_source_oi_priority_parses_labeled_timeframe_rows_not_price_changes(self):
        source = (
            "OI / Price change:\n"
            "5m: OI +0.20%, price -0.80%\n"
            "15m: OI +5.30%, price +2.17%\n"
            "1h: OI +8.47%, price +4.92%"
        )
        result = source_oi_priority(source)
        self.assertEqual(result["source_oi_15m_pct"], 5.30)
        self.assertEqual(result["source_oi_1h_pct"], 8.47)
        self.assertAlmostEqual(result["oi_priority_score"], 11.44, places=2)

    def test_source_oi_priority_supports_legacy_compact_feed(self):
        result = source_oi_priority("🟩+5.30%    🟩+2.17% (15m)")
        self.assertEqual(result["source_oi_15m_pct"], 5.30)
        self.assertIsNone(result["source_oi_1h_pct"])

    def test_long_needs_oi_and_buy_flow(self):
        self.assertTrue(coinalyze_confirms(signal(),metrics()))
        self.assertFalse(coinalyze_confirms(signal(),metrics(taker=.47)))
        self.assertFalse(coinalyze_confirms(signal(),metrics(oi=.02)))
    def test_short_needs_sell_flow(self):
        self.assertTrue(coinalyze_confirms(signal("SHORT"),metrics(taker=.40)))
        self.assertFalse(coinalyze_confirms(signal("SHORT"),metrics(taker=.55)))

    def test_derivatives_rejection_reports_exact_failed_gates(self):
        rejected = metrics(taker=.47, oi=.12)
        reasons = coinalyze_rejection_reasons(signal(), rejected)
        self.assertTrue(any("رشد OI کافی نیست" in reason for reason in reasons))
        self.assertTrue(any("جریان خرید کافی نیست" in reason for reason in reasons))
        self.assertFalse(coinalyze_confirms(signal(), rejected))
    def test_missing_core_funding_still_fails_closed(self):
        partial = {"oi_change_15m_window_pct": 0.4, "taker_buy_ratio": 0.60,
                   "data_quality": {"complete": False}}
        self.assertFalse(coinalyze_confirms(signal(), partial))

    def test_missing_ancillary_lsr_and_liquidations_do_not_block_core_setup(self):
        core_only = {"oi_change_15m_window_pct": 0.4, "taker_buy_ratio": 0.60,
                     "funding_rate": 0.0001,
                     "data_quality": {"complete": False}}
        self.assertTrue(coinalyze_confirms(signal(), core_only))

    def test_invalid_supplied_long_short_ratio_is_rejected(self):
        malformed = metrics()
        malformed["long_short_ratio"] = float("nan")
        self.assertFalse(coinalyze_confirms(signal(), malformed))

    def test_missing_or_invalid_core_confirmation_fails_closed(self):
        self.assertFalse(coinalyze_confirms(signal(), {"oi_change_15m_window_pct": None, "taker_buy_ratio": 0.6}))
        self.assertFalse(coinalyze_confirms(signal(), {"oi_change_15m_window_pct": 0.4, "taker_buy_ratio": float("nan")}))
        self.assertFalse(coinalyze_confirms(signal(), {"oi_change_15m_window_pct": 0.4, "taker_buy_ratio": 1.2}))

    def test_confirmed_short_setup_is_published_and_both_directions_are_enabled(self):
        short = Signal("ABCUSDT", "SHORT", 1.0, 1.02, 0.99, 0.96,
                       .8, .3, .40, 1.4, .0001, 82, 900000)
        snapshot = {"symbol": "ABCUSDT", "candles": [], "oi_values": []}
        with patch("oi_engine.watchlist_engine.fetch_snapshot", return_value=snapshot):
            with patch("oi_engine.watchlist_engine.advance_pullback_setup",
                       return_value=(short, None, "پولبک SHORT تأیید شد")) as advance:
                found, updated = scan_watchlist(
                    {"ABCUSDT": {"status": "در حال رصد"}},
                    FakeClient(metrics(taker=.40)), now=1000
                )
        advance.assert_called_once_with(snapshot, None, None)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0].side, "SHORT")
        self.assertEqual(updated["ABCUSDT"]["status"], "سیگنال SHORT آماده ارسال")

    def test_binance_451_uses_coinalyze_market_snapshot(self):
        snapshot = {"symbol": "ABCUSDT", "candles": [], "oi_values": [1, 2, 3]}
        fallback_metrics = {**metrics(), "market_snapshot": snapshot}
        error = requests.HTTPError("451 Client Error")
        error.response = Mock(status_code=451)
        with patch("oi_engine.watchlist_engine.fetch_snapshot", side_effect=error), \
             patch("oi_engine.watchlist_engine.advance_pullback_setup", return_value=(signal(), None, "پولبک تأیید شد")):
            found, updated = scan_watchlist(
                {"ABCUSDT": {"status": "در حال رصد"}},
                FakeClient(fallback_metrics), now=1000
            )
        self.assertEqual(len(found), 1)
        self.assertEqual(updated["ABCUSDT"]["status"], "سیگنال LONG آماده ارسال")
        self.assertIn("Coinalyze 15m fallback", updated["ABCUSDT"]["data_source"])
        self.assertNotIn("market_snapshot", updated["ABCUSDT"]["coinalyze"])

    def test_failed_market_scan_does_not_consume_fresh_alert_priority(self):
        with patch("oi_engine.watchlist_engine.fetch_snapshot",
                   side_effect=__import__("oi_engine.signal_engine", fromlist=["MarketDataError"]).MarketDataError("temporary provider failure")):
            found, updated = scan_watchlist(
                {"ABCUSDT": {"last_alert_received_at": 900, "last_checked": 800}},
                FakeClient(metrics()), now=1000
            )
        self.assertEqual(found, [])
        self.assertEqual(updated["ABCUSDT"]["last_attempted_at"], 1000)
        self.assertEqual(updated["ABCUSDT"]["last_checked"], 800)
        self.assertEqual(updated["ABCUSDT"]["status"], "خطای دریافت داده")

    def test_tp1_below_one_percent_is_rejected_for_long_and_short(self):
        weak_long = Signal("ABCUSDT", "LONG", 1.0, .98, 1.005, 1.02, .8, .3, .56, 1.4, .0001, 82, 900000)
        self.assertIn("حداقل مجاز 1.00٪", signal_price_geometry_rejection(weak_long))
        short = Signal("ABCUSDT", "SHORT", 1.0, 1.02, 0.995, 0.98, .8, .3, .40, 1.4, .0001, 82, 900000)
        self.assertIn("حداقل مجاز 1.00٪", signal_price_geometry_rejection(short))

    def test_tp1_at_or_above_one_percent_passes_for_both_sides(self):
        long = Signal("ABCUSDT", "LONG", 1.0, .98, 1.01, 1.04, .8, .3, .56, 1.4, .0001, 82, 900000)
        short = Signal("ABCUSDT", "SHORT", 1.0, 1.02, .99, .96, .8, .3, .40, 1.4, .0001, 82, 900000)
        self.assertIsNone(signal_price_geometry_rejection(long))
        self.assertIsNone(signal_price_geometry_rejection(short))

    def test_scan_blocks_sub_one_percent_tp1_even_when_derivatives_confirm(self):
        weak_target = Signal("ABCUSDT", "LONG", 1.0, .98, 1.005, 1.02, .8, .3, .56, 1.4, .0001, 82, 900000)
        with patch("oi_engine.watchlist_engine.fetch_snapshot", return_value={}), \
             patch("oi_engine.watchlist_engine.evaluate_snapshot", return_value=None), \
             patch("oi_engine.watchlist_engine.advance_pullback_setup", return_value=(weak_target, None, "candidate")):
            found, updated = scan_watchlist({"ABCUSDT": {}}, FakeClient(metrics()), now=1000)
        self.assertEqual(found, [])
        self.assertEqual(updated["ABCUSDT"]["status"], "رد سیگنال به‌دلیل کیفیت قیمت‌ها")
        self.assertIn("فاصله هدف اول فقط 0.50٪", updated["ABCUSDT"]["reason"])

    def test_signal_message_is_compact_and_keeps_actionable_fields(self):
        message = format_confirmed_signal(signal(), metrics())
        self.assertIn("<b>LONG</b>", message)
        self.assertIn("ABCUSDT", message)
        self.assertIn("ورود:", message)
        self.assertIn("حد ضرر:", message)
        self.assertIn("هدف ۱:", message)
        self.assertIn("هدف ۲:", message)
        self.assertIn("OI ۱۵دقیقه:", message)
        self.assertIn("جریان 60%", message)
        self.assertIn("ریسک: <b>2.00%</b>", message)
        self.assertIn("فاصله: <b>2.00%</b>", message)
        self.assertIn("فاصله: <b>4.00%</b>", message)
        self.assertIn("نسبت سود به ریسک تا هدف ۱: <b>1.00R</b>", message)
        self.assertNotIn("Funding", message)
        self.assertNotIn("لیکویید", message)
        self.assertNotIn("نسبت حساب‌های", message)

    def test_coinalyze_rejection_blocks_pullback_signal(self):
        pending = {
            "side": "LONG", "level": 1.0,
            "breakout_close_time": 900000,
            "expires_at_close_time": 4500000,
            "breakout_atr": 0.02,
        }
        with patch("oi_engine.watchlist_engine.fetch_snapshot", return_value={}), \
             patch("oi_engine.watchlist_engine.evaluate_snapshot", return_value=None), \
             patch("oi_engine.watchlist_engine.advance_pullback_setup",
                   return_value=(signal(), None, "پولبک تأیید شد")):
            found, updated = scan_watchlist(
                {"ABCUSDT": {"status": "منتظر پولبک", "pending_setup": pending}},
                FakeClient(metrics(taker=0.47)), now=1000
            )
        self.assertEqual(found, [])
        self.assertEqual(updated["ABCUSDT"]["status"], "منتظر تأیید مشتقات")
        self.assertIn("جریان خرید کافی نیست", updated["ABCUSDT"]["reason"])
        self.assertEqual(updated["ABCUSDT"]["pending_setup"], pending)

    def test_bos_is_blocked_while_pullback_setup_is_pending(self):
        pending = {
            "side": "SHORT", "level": 1.0,
            "breakout_close_time": 900000,
            "expires_at_close_time": 4500000,
            "breakout_atr": 0.02,
        }
        with patch("oi_engine.watchlist_engine.fetch_snapshot", return_value={}), \
             patch("oi_engine.watchlist_engine.evaluate_snapshot", return_value=signal()), \
             patch("oi_engine.watchlist_engine.advance_pullback_setup",
                   return_value=(None, pending, "منتظر پولبک")):
            found, updated = scan_watchlist(
                {"ABCUSDT": {"status": "در حال رصد", "pending_setup": pending}},
                FakeClient(metrics()), now=1000
            )
        self.assertEqual(found, [])
        self.assertEqual(updated["ABCUSDT"]["pending_setup"], pending)
        self.assertEqual(updated["ABCUSDT"]["status"], "منتظر پولبک و تأیید")

    def test_bos_breakout_waits_for_pullback_instead_of_immediate_entry(self):
        newly_created = {
            "side": "LONG", "level": 1.0,
            "breakout_close_time": 900000,
            "expires_at_close_time": 4500000,
            "breakout_atr": 0.02,
        }
        with patch("oi_engine.watchlist_engine.fetch_snapshot", return_value={}), \
             patch("oi_engine.watchlist_engine.evaluate_snapshot", return_value=signal()), \
             patch("oi_engine.watchlist_engine.advance_pullback_setup",
                   return_value=(None, newly_created, "شکست LONG ثبت شد؛ منتظر پولبک")):
            found, updated = scan_watchlist(
                {"ABCUSDT": {"status": "در حال رصد"}},
                FakeClient(metrics()), now=1000
            )
        self.assertEqual(found, [])
        self.assertEqual(updated["ABCUSDT"]["status"], "منتظر پولبک و تأیید")
        self.assertEqual(updated["ABCUSDT"]["pending_setup"], newly_created)

    def test_no_entry_keeps_watch_status(self):
        with patch("oi_engine.watchlist_engine.fetch_snapshot",return_value={}), \
             patch("oi_engine.watchlist_engine.evaluate_snapshot",return_value=None), \
             patch("oi_engine.watchlist_engine.advance_pullback_setup",return_value=(None, None, "هنوز setup نداریم")):
            found, updated=scan_watchlist({"ABCUSDT":{"status":"در حال رصد"}},FakeClient(metrics()),now=1000)
        self.assertEqual(found,[])
        self.assertEqual(updated["ABCUSDT"]["status"],"در حال رصد")

    def test_breakout_rejection_reason_is_not_hidden_by_generic_status(self):
        with patch("oi_engine.watchlist_engine.fetch_snapshot", return_value={}), \
             patch("oi_engine.watchlist_engine.evaluate_snapshot", return_value=signal()), \
             patch("oi_engine.watchlist_engine.advance_pullback_setup",
                   return_value=(None, None, "حجم شکست کافی نیست (0.80x؛ حداقل 1.50x)")):
            found, updated = scan_watchlist(
                {"ABCUSDT": {"status": "در حال رصد"}},
                FakeClient(metrics()), now=1000
            )
        self.assertEqual(found, [])
        self.assertIn("حجم شکست کافی نیست", updated["ABCUSDT"]["reason"])
        self.assertNotIn("شتاب اولیه دیده شد", updated["ABCUSDT"]["reason"])


    def test_raw_funding_is_not_shown_in_compact_signal_card(self):
        extreme = metrics()
        extreme["funding_rate"] = -0.028806
        extreme["predicted_funding_rate"] = -0.089197
        message = format_confirmed_signal(signal(), extreme)
        self.assertNotIn("Funding", message)
        self.assertNotIn("واحد/بازه نامشخص", message)

if __name__ == "__main__":
    unittest.main()
