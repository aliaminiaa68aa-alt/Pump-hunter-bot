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
