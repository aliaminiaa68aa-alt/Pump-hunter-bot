import unittest

from oi_engine.outcome_tracker import make_trade_record, monitor_journal, summarize_outcomes


def _signal():
    return {
        "symbol": "TESTUSDT", "side": "LONG", "entry": 100.0,
        "stop_loss": 98.0, "take_profit_1": 102.0, "take_profit_2": 104.0,
        "candle_close_time": 899999,
    }


def _candle(open_ms, open_price, high, low, close):
    return [open_ms, str(open_price), str(high), str(low), str(close), "1"]


class VerifiedEntryOutcomeTests(unittest.TestCase):
    def _journal(self):
        record = make_trade_record(_signal(), {}, 900000)
        return {"trades": {record["trade_id"]: record}}, record["trade_id"]

    def test_open_without_entry_cannot_close_at_target(self):
        journal, key = self._journal()
        record = journal["trades"][key]
        record["status"] = "OPEN"
        # The candle reaches TP2 but never touches the planned entry at 100.
        candles = [_candle(900000, 101, 105, 100.5, 104.5)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: candles)
        self.assertEqual(record["status"], "INVALID_NO_ENTRY")
        self.assertEqual(record["events"], [])
        report = summarize_outcomes(journal)
        self.assertEqual(report["entry_touched"], 0)
        self.assertEqual(report["not_entered"], 1)
        self.assertEqual(report["tp2_reached"], 0)

    def test_entry_candle_stop_and_target_are_not_counted(self):
        journal, key = self._journal()
        record = journal["trades"][key]
        # The same 15m candle touches entry=100, SL=98 and TP1/TP2.
        # OHLC cannot establish which level was reached first.
        candles = [_candle(900000, 100, 105, 97, 101)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: candles)
        self.assertEqual(record["status"], "AMBIGUOUS_ENTRY_CANDLE")
        self.assertEqual([event["event"] for event in record["events"]],
                         ["ENTRY", "AMBIGUOUS_ENTRY_CANDLE"])
        self.assertTrue(record["entry_candle_excursion_ambiguous"])
        self.assertTrue(record["entry_candle_ambiguous"])
        self.assertEqual(record["last_checked_ms"], 1800000)

    def test_exit_monitoring_starts_on_candle_after_entry(self):
        journal, key = self._journal()
        record = journal["trades"][key]
        entry_candle = [_candle(900000, 100, 100.5, 99.5, 100.2)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: entry_candle)
        next_candle = [_candle(1800000, 101, 103, 99, 102.5)]
        monitor_journal(journal, now_ms=2700000, fetcher=lambda *_: next_candle)
        self.assertEqual(record["status"], "TP1_HIT")
        self.assertEqual(
            [event["event"] for event in record["events"]],
            ["ENTRY", "TP1"],
        )

    def test_historical_tp2_without_entry_is_not_counted(self):
        journal, key = self._journal()
        record = journal["trades"][key]
        record["status"] = "CLOSED_TP2"
        record["events"] = [{"event": "TP2", "time_ms": 1800000, "fill": 104.0}]
        report = summarize_outcomes(journal)
        self.assertEqual(report["entry_touched"], 0)
        self.assertEqual(report["not_entered"], 1)
        self.assertEqual(report["tp2_reached"], 0)


if __name__ == "__main__":
    unittest.main()
