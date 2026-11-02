import unittest
import requests
from unittest.mock import Mock
from unittest.mock import patch
from oi_engine.signal_engine import Signal
from oi_engine.watchlist_engine import coinalyze_confirms, scan_watchlist, format_confirmed_signal

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
    def test_long_needs_oi_and_buy_flow(self):
        self.assertTrue(coinalyze_confirms(signal(),metrics()))
        self.assertFalse(coinalyze_confirms(signal(),metrics(taker=.49)))
        self.assertFalse(coinalyze_confirms(signal(),metrics(oi=.02)))
    def test_short_needs_sell_flow(self):
        self.assertTrue(coinalyze_confirms(signal("SHORT"),metrics(taker=.40)))
        self.assertFalse(coinalyze_confirms(signal("SHORT"),metrics(taker=.55)))
    def test_optional_coinalyze_metrics_do_not_veto_signal(self):
        partial = {"oi_change_15m_window_pct": 0.4, "taker_buy_ratio": 0.60,
                   "data_quality": {"complete": False}}
        self.assertTrue(coinalyze_confirms(signal(), partial))

    def test_missing_or_invalid_core_confirmation_fails_closed(self):
        self.assertFalse(coinalyze_confirms(signal(), {"oi_change_15m_window_pct": None, "taker_buy_ratio": 0.6}))
        self.assertFalse(coinalyze_confirms(signal(), {"oi_change_15m_window_pct": 0.4, "taker_buy_ratio": float("nan")}))
        self.assertFalse(coinalyze_confirms(signal(), {"oi_change_15m_window_pct": 0.4, "taker_buy_ratio": 1.2}))

    def test_binance_451_uses_coinalyze_market_snapshot(self):
        snapshot = {"symbol": "ABCUSDT", "candles": [], "oi_values": [1, 2, 3]}
        fallback_metrics = {**metrics(), "market_snapshot": snapshot}
        error = requests.HTTPError("451 Client Error")
        error.response = Mock(status_code=451)
        with patch("oi_engine.watchlist_engine.fetch_snapshot", side_effect=error), \
             patch("oi_engine.watchlist_engine.evaluate_snapshot", return_value=signal()):
            found, updated = scan_watchlist(
                {"ABCUSDT": {"status": "در حال رصد"}},
                FakeClient(fallback_metrics), now=1000
            )
        self.assertEqual(len(found), 1)
        self.assertEqual(updated["ABCUSDT"]["status"], "تأیید LONG")
        self.assertIn("Coinalyze 15m fallback", updated["ABCUSDT"]["data_source"])
        self.assertNotIn("market_snapshot", updated["ABCUSDT"]["coinalyze"])

    def test_signal_labels_coinalyze_as_second_stage_not_always_independent(self):
        message = format_confirmed_signal(signal(), metrics())
        self.assertIn("تأیید مرحله دوم Coinalyze", message)
        self.assertNotIn("تأیید مستقل Coinalyze", message)

    def test_confirmed_status(self):
        with patch("oi_engine.watchlist_engine.fetch_snapshot",return_value={}), \
             patch("oi_engine.watchlist_engine.evaluate_snapshot",return_value=signal()):
            found, updated=scan_watchlist({"ABCUSDT":{"status":"در حال رصد"}},FakeClient(metrics()),now=1000)
        self.assertEqual(len(found),1)
        self.assertEqual(updated["ABCUSDT"]["status"],"تأیید LONG")
    def test_no_entry_keeps_watch_status(self):
        with patch("oi_engine.watchlist_engine.fetch_snapshot",return_value={}), \
             patch("oi_engine.watchlist_engine.evaluate_snapshot",return_value=None):
            found, updated=scan_watchlist({"ABCUSDT":{"status":"در حال رصد"}},FakeClient(metrics()),now=1000)
        self.assertEqual(found,[])
        self.assertEqual(updated["ABCUSDT"]["status"],"در حال رصد")


    def test_extreme_coinalyze_funding_is_labeled_and_warned(self):
        extreme = metrics()
        extreme["funding_rate"] = -0.028806
        extreme["predicted_funding_rate"] = -0.089197
        message = format_confirmed_signal(signal(), extreme)
        self.assertIn("Funding (snapshot اولیه):", message)
        self.assertIn("Funding (Coinalyze): -2.88060%", message)
        self.assertIn("Funding پیش‌بینی‌شده (Coinalyze): -8.91970%", message)
        self.assertIn("Funding گزارش‌شده بسیار غیرعادی است", message)

    def test_normal_funding_does_not_add_extreme_warning(self):
        message = format_confirmed_signal(signal(), metrics())
        self.assertNotIn("Funding گزارش‌شده بسیار غیرعادی است", message)

if __name__ == "__main__":
    unittest.main()
