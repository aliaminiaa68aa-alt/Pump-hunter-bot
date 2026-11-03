import unittest

from oi_engine.outcome_tracker import make_trade_record, monitor_journal


def signal(side="LONG"):
    return {
        "symbol": "TESTUSDT",
        "side": side,
        "entry": 100.0,
        "stop_loss": 98.0 if side == "LONG" else 102.0,
        "take_profit_1": 102.0 if side == "LONG" else 98.0,
        "take_profit_2": 104.0 if side == "LONG" else 96.0,
        "candle_close_time": 899999,
    }


def candle(open_ms, open_price, high, low, close):
    return [open_ms, str(open_price), str(high), str(low), str(close), "1"]


class OutcomeTrackerTests(unittest.TestCase):
    def journal(self, side="LONG"):
        record = make_trade_record(signal(side), {"oi_change_15m_window_pct": 0.4,
                                                  "taker_buy_ratio": 0.6}, 900000)
        return {"trades": {record["trade_id"]: record}}, record["trade_id"]

    def test_tp1_then_tp2_are_recorded_in_order(self):
        journal, key = self.journal()
        first = [candle(900000, 100, 103, 99, 102.5)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: first)
        self.assertEqual(journal["trades"][key]["status"], "TP1_HIT")
        self.assertEqual([e["event"] for e in journal["trades"][key]["events"]], ["TP1"])

        second = [candle(1800000, 102.5, 105, 102, 104.5)]
        monitor_journal(journal, now_ms=2700000, fetcher=lambda *_: second)
        self.assertEqual(journal["trades"][key]["status"], "CLOSED_TP2")
        self.assertEqual([e["event"] for e in journal["trades"][key]["events"]], ["TP1", "TP2"])

    def test_stop_wins_if_stop_and_target_are_in_same_candle(self):
        journal, key = self.journal()
        both = [candle(900000, 100, 103, 97, 101)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: both)
        record = journal["trades"][key]
        self.assertEqual(record["status"], "CLOSED_SL")
        self.assertEqual([e["event"] for e in record["events"]], ["SL"])

    def test_short_tp1_then_tp2(self):
        journal, key = self.journal("SHORT")
        first = [candle(900000, 100, 101, 97, 97.5)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: first)
        self.assertEqual(journal["trades"][key]["status"], "TP1_HIT")
        second = [candle(1800000, 97, 98, 95, 95.5)]
        monitor_journal(journal, now_ms=2700000, fetcher=lambda *_: second)
        self.assertEqual(journal["trades"][key]["status"], "CLOSED_TP2")

    def test_gap_through_stop_uses_open_price(self):
        journal, key = self.journal()
        gap = [candle(900000, 97, 99, 96, 98)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: gap)
        event = journal["trades"][key]["events"][0]
        self.assertEqual(event["event"], "SL")
        self.assertEqual(event["fill"], 97.0)

    def test_provider_error_is_retained_without_closing_trade(self):
        journal, key = self.journal()
        def fail(*_):
            raise RuntimeError("provider unavailable")
        monitor_journal(journal, now_ms=1800000, fetcher=fail)
        record = journal["trades"][key]
        self.assertEqual(record["status"], "OPEN")
        self.assertIn("provider unavailable", record["last_error"])

    def test_trade_times_out_after_four_days(self):
        journal, key = self.journal()
        monitor_journal(journal, now_ms=900000 + 96 * 60 * 60 * 1000,
                        fetcher=lambda *_: [])
        self.assertEqual(journal["trades"][key]["status"], "TIMED_OUT")


if __name__ == "__main__":
    unittest.main()
