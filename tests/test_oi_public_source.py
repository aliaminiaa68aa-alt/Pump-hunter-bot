import unittest
from unittest.mock import Mock, patch

from oi_engine.telegram_public_source import _ChannelParser, fetch_public_channel_posts, poll_public_channel_alerts


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

    def test_fetch_public_channel_posts_uses_before_pagination_parameter(self):
        response = Mock()
        response.text = '''
        <div class="tgme_widget_message js-widget_message" data-post="oi_detector/123">
          <div class="tgme_widget_message_text">TESTUSDT +2%</div>
          <div class="tgme_widget_message_footer"><time datetime="2026-11-02T10:00:00+00:00"></time></div>
        </div>
        '''
        response.raise_for_status = Mock()
        with patch("oi_engine.telegram_public_source.requests.get", return_value=response) as get:
            posts = fetch_public_channel_posts("oi_detector", before=124)
        self.assertEqual(posts[0]["message_id"], 123)
        self.assertEqual(get.call_args.kwargs["params"], {"before": 124})

    def test_backfills_unseen_posts_older_than_the_latest_preview_window(self):
        def make_post(message_id):
            return {
                "message_id": message_id,
                "date": 1000,
                "text": f"COIN{message_id}USDT +2%",
                "channel": "oi_detector",
            }
        latest = [make_post(i) for i in range(101, 121)]
        older = [make_post(i) for i in range(81, 101)]
        calls = []
        def fetch(channel, timeout=20, before=None):
            calls.append(before)
            return latest if before is None else older

        watchlist = {}
        with patch("oi_engine.telegram_public_source.fetch_public_channel_posts", side_effect=fetch):
            cursor, added = poll_public_channel_alerts("oi_detector", 90, watchlist, now=1000)
        self.assertEqual(calls, [None, 101])
        self.assertEqual(added, 30)
        self.assertEqual(cursor, 120)
        self.assertIn("COIN91USDT", watchlist)
        self.assertIn("COIN120USDT", watchlist)
        self.assertNotIn("COIN90USDT", watchlist)

    def test_first_startup_backfills_recent_history_before_setting_cursor(self):
        def make_post(message_id):
            return {
                "message_id": message_id,
                "date": 1000,
                "text": f"COIN{message_id}USDT +2%",
                "channel": "oi_detector",
            }
        latest = [make_post(i) for i in range(101, 121)]
        older = [make_post(i) for i in range(81, 101)]
        def fetch(channel, timeout=20, before=None):
            return latest if before is None else older
        watchlist = {}
        with patch("oi_engine.telegram_public_source.fetch_public_channel_posts", side_effect=fetch):
            cursor, added = poll_public_channel_alerts("oi_detector", 0, watchlist, now=1000)
        self.assertEqual(added, 40)
        self.assertEqual(cursor, 120)
        self.assertIn("COIN81USDT", watchlist)

    def test_new_public_post_is_added_and_cursor_moves(self):
        posts = [{
            "message_id": 20, "date": 1000, "text": "🚨 Crypto Open Interest | Pump/Dump Screener Bot 🔥 🔥 🟢 #TEST $0.123 • OI [15m]",
            "channel": "oi_detector",
        }]
        watchlist = {}
        with patch("oi_engine.telegram_public_source.fetch_public_channel_posts", return_value=posts):
            cursor, added = poll_public_channel_alerts("oi_detector", 19, watchlist, now=1000)
        self.assertEqual(cursor, 20)
        self.assertEqual(added, 1)
        self.assertIn("TESTUSDT", watchlist)
        self.assertEqual(watchlist["TESTUSDT"]["source"], "telegram_public_preview")
        self.assertEqual(watchlist["TESTUSDT"]["source_type"], "oi_channel")
        self.assertEqual(watchlist["TESTUSDT"]["source_side"], "LONG")

    def test_alert_is_added_even_when_watchlist_already_has_four_symbols(self):
        posts = [{
            "message_id": 20, "date": 1000, "text": "🔥 🟢 #NEWCOIN NEWCOINUSDT +2%",
            "channel": "oi_detector",
        }]
        watchlist = {f"COIN{i}USDT": {} for i in range(4)}
        with patch("oi_engine.telegram_public_source.fetch_public_channel_posts", return_value=posts):
            cursor, added = poll_public_channel_alerts("oi_detector", 19, watchlist, now=1000)
        self.assertEqual(cursor, 20)
        self.assertEqual(added, 1)
        self.assertIn("NEWCOINUSDT", watchlist)
        self.assertEqual(len(watchlist), 5)

    def test_alert_without_explicit_direction_is_retained_but_never_guesses_a_side(self):
        posts = [{
            "message_id": 23, "date": 1000, "text": "NEWCOINUSDT +2%",
            "channel": "oi_detector",
        }]
        watchlist = {}
        with patch("oi_engine.telegram_public_source.fetch_public_channel_posts", return_value=posts), \
             patch("builtins.print") as log:
            cursor, added = poll_public_channel_alerts("oi_detector", 22, watchlist, now=1000)
        self.assertEqual(cursor, 23)
        self.assertEqual(added, 1)
        self.assertIn("NEWCOINUSDT", watchlist)
        self.assertIsNone(watchlist["NEWCOINUSDT"]["source_side"])
        self.assertNotIn("both LONG and SHORT", " ".join(str(call) for call in log.call_args_list))

    def test_unparsed_post_is_logged_with_message_id_and_cursor_advances(self):
        posts = [{
            "message_id": 22, "date": 1000, "text": "OI alert without a recognizable symbol",
            "channel": "oi_detector",
        }]
        with patch("oi_engine.telegram_public_source.fetch_public_channel_posts", return_value=posts), \
             patch("builtins.print") as log:
            cursor, added = poll_public_channel_alerts("oi_detector", 21, {}, now=1000)
        self.assertEqual(cursor, 22)
        self.assertEqual(added, 0)
        self.assertIn("message_id=22", " ".join(str(call) for call in log.call_args_list))

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
