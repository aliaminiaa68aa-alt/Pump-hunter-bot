import unittest
from oi_engine.alert_ingest import parse_forwarded_oi_alert, parse_oi_alert_direction


class AlertIngestTests(unittest.TestCase):
    def test_channel_green_marker_means_long(self):
        self.assertEqual(parse_oi_alert_direction({"channel_post": {"text": "🔥🔥🔥 🟢 #GRVT $0.24 • OI [15m]"}}), "LONG")

    def test_channel_red_marker_means_short(self):
        self.assertEqual(parse_oi_alert_direction({"channel_post": {"text": "🔴 #ONDS $5.84 • OI [15m]"}}), "SHORT")

    def test_missing_direction_marker_is_not_guessed(self):
        self.assertIsNone(parse_oi_alert_direction({"channel_post": {"text": "#TEST $1.00 • OI [15m]"}}))

    def test_forwarded_channel_symbol(self):
        self.assertEqual(parse_forwarded_oi_alert({"message": {"forward_origin": {"type": "channel"}, "text": "OI: JCT/USDT"}}), "JCTUSDT")

    def test_legacy_forwarded_symbol(self):
        self.assertEqual(parse_forwarded_oi_alert({"message": {"forward_from_chat": {"id": -1}, "caption": "BTCUSDT"}}), "BTCUSDT")

    def test_non_forwarded_is_ignored(self):
        self.assertIsNone(parse_forwarded_oi_alert({"message": {"text": "OI Alert BTCUSDT"}}))

    def test_public_oi_detector_hashtag_price_format(self):
        update = {"channel_post": {
            "text": "🚨 Crypto Open Interest #ICNT $0.1897 • OI [15m] • Price change +4.85%"
        }}
        self.assertEqual(parse_forwarded_oi_alert(update), "ICNTUSDT")

    def test_hashtag_without_oi_context_is_ignored(self):
        update = {"channel_post": {"text": "New listing #ICNT $0.1897"}}
        self.assertIsNone(parse_forwarded_oi_alert(update))

    def test_unsupported_symbol_is_ignored(self):
        self.assertIsNone(parse_forwarded_oi_alert({"message": {"forward_date": 1, "text": "OI Alert BTC"}}))

    def test_equity_and_etf_symbols_are_rejected(self):
        for ticker in ("AAPL", "AMD", "COST", "BYD", "XLE", "UVXY", "HIMS", "HK1810", "RIVN", "CSOPSKHYNIX2L"):
            with self.subTest(ticker=ticker):
                update = {"channel_post": {
                    "text": f"🚨 Crypto Open Interest #{ticker} $10.00 • OI [15m]"
                }}
                self.assertIsNone(parse_forwarded_oi_alert(update))


if __name__ == "__main__":
    unittest.main()
