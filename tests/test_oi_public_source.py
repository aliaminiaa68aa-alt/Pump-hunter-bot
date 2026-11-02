import unittest
from unittest.mock import patch

from oi_engine.telegram_public_source import _ChannelParser, poll_public_channel_alerts


class PublicTelegramSourceTests(unittest.TestCase):
    def test_parser_extracts_public_channel_message(self):
        html = '''
        <div class="tgme_widget_message js-widget_message" data-post="oi_detector/123">
          <div class="tgme_widget_message_text">🟢 #TEST<br>OI [15m] +2.5%<br><code>TESTUSDT.P</code></div>
          <div class="tgme_widget_message_footer"><time datetime="2026-11-02T10:00:00+00:00"></time></div>
        </div>
        '''
        parser = _ChannelParser()
        parser.feed(html)
        self.assertEqual(len(parser.posts), 1)
        self.assertEqual(parser.posts[0]["post_key"], "oi_detector/123")
        self.assertIn("TESTUSDT.P", parser.posts[0]["text"])
        self.assertEqual(parser.posts[0]["date"], "2026-11-02T10:00:00+00:00")

    def test_new_public_post_is_added_and_cursor_moves(self):
        posts = [{
            "message_id": 20, "date": 1000, "text": "OI alert TESTUSDT.P +2%",
            "channel": "oi_detector",
        }]
        watchlist = {}
        with patch("oi_engine.telegram_public_source.fetch_public_channel_posts", return_value=posts):
            cursor, added = poll_public_channel_alerts("oi_detector", 19, watchlist, now=1000)
        self.assertEqual(cursor, 20)
        self.assertEqual(added, 1)
        self.assertIn("TESTUSDT", watchlist)
        self.assertEqual(watchlist["TESTUSDT"]["source"], "telegram_public_preview")

    def test_alert_is_added_even_when_watchlist_already_has_four_symbols(self):
        posts = [{
            "message_id": 20, "date": 1000, "text": "NEWCOINUSDT +2%",
            "channel": "oi_detector",
        }]
        watchlist = {f"COIN{i}USDT": {} for i in range(4)}
        with patch("oi_engine.telegram_public_source.fetch_public_channel_posts", return_value=posts):
            cursor, added = poll_public_channel_alerts("oi_detector", 19, watchlist, now=1000)
        self.assertEqual(cursor, 20)
        self.assertEqual(added, 1)
        self.assertIn("NEWCOINUSDT", watchlist)
        self.assertEqual(len(watchlist), 5)

    def test_unsupported_market_is_skipped_and_cursor_advances(self):
        posts = [{
            "message_id": 21, "date": 1000,
            "text": "🚨 #AMD $478.66 • OI [15m] • Price change +2%",
            "channel": "oi_detector",
        }]
        watchlist = {}
        with patch("oi_engine.telegram_public_source.fetch_public_channel_posts", return_value=posts):
            cursor, added = poll_public_channel_alerts(
                "oi_detector", 20, watchlist, now=1000,
                symbol_validator=lambda symbol: False,
            )
        self.assertEqual(cursor, 21)
        self.assertEqual(added, 0)
        self.assertEqual(watchlist, {})

    def test_old_posts_are_skipped(self):
        posts = [{
            "message_id": 20, "date": 1, "text": "OLDUSDT +2%",
            "channel": "oi_detector",
        }]
        with patch("oi_engine.telegram_public_source.fetch_public_channel_posts", return_value=posts):
            cursor, added = poll_public_channel_alerts("oi_detector", 19, {}, now=100000)
        self.assertEqual(cursor, 20)
        self.assertEqual(added, 0)


if __name__ == "__main__":
    unittest.main()
