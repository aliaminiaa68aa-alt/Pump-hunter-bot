import unittest
from oi_engine.alert_ingest import parse_forwarded_oi_alert

class AlertIngestTests(unittest.TestCase):
    def test_forwarded_channel_symbol(self):
        self.assertEqual(parse_forwarded_oi_alert({"message":{"forward_origin":{"type":"channel"},"text":"OI: JCT/USDT"}}),"JCTUSDT")
    def test_legacy_forwarded_symbol(self):
        self.assertEqual(parse_forwarded_oi_alert({"message":{"forward_from_chat":{"id":-1},"caption":"BTCUSDT"}}),"BTCUSDT")
    def test_non_forwarded_is_ignored(self):
        self.assertIsNone(parse_forwarded_oi_alert({"message":{"text":"OI Alert BTCUSDT"}}))
    def test_unsupported_symbol_is_ignored(self):
        self.assertIsNone(parse_forwarded_oi_alert({"message":{"forward_date":1,"text":"OI Alert BTC"}}))

if __name__ == "__main__":
    unittest.main()
