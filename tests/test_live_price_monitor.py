import unittest

from oi_engine.live_price_monitor import apply_live_prices


def make_journal(side="LONG"):
    entry = 100.0
    signal = {
        "symbol": "TESTUSDT",
        "side": side,
        "entry": entry,
        "stop_loss": 98.0 if side == "LONG" else 102.0,
        "take_profit_1": 102.0 if side == "LONG" else 98.0,
        "take_profit_2": 104.0 if side == "LONG" else 96.0,
    }
    record = {
        "trade_id": "test",
        "signal": signal,
        "sent_at_ms": 1_000,
        "status": "WAITING_ENTRY",
        "events": [],
    }
    return {"trades": {"test": record}}, record


class LivePriceMonitorTests(unittest.TestCase):
    def test_first_snapshot_is_baseline_and_entry_crossing_is_detected(self):
        journal, record = make_journal("LONG")
        apply_live_prices(journal, {"TESTUSDT": 101.0}, now_ms=2_000)
        self.assertEqual(record["status"], "WAITING_ENTRY")
        apply_live_prices(journal, {"TESTUSDT": 99.9}, now_ms=3_000)
        self.assertEqual(record["status"], "OPEN")
        self.assertEqual(record["entry_fill_price"], 100.0)
        self.assertEqual([e["event"] for e in record["events"]], ["ENTRY"])

    def test_short_entry_crossing_is_detected(self):
        journal, record = make_journal("SHORT")
        apply_live_prices(journal, {"TESTUSDT": 99.0}, now_ms=2_000)
        apply_live_prices(journal, {"TESTUSDT": 100.1}, now_ms=3_000)
        self.assertEqual(record["status"], "OPEN")
        self.assertEqual([e["event"] for e in record["events"]], ["ENTRY"])

    def test_first_snapshot_beyond_entry_is_missed_not_falsely_filled(self):
        journal, record = make_journal("LONG")
        apply_live_prices(journal, {"TESTUSDT": 99.0}, now_ms=2_000)
        self.assertEqual(record["status"], "MISSED_ENTRY")
        self.assertEqual(record["events"], [])

    def test_long_setup_expires_if_tp1_is_reached_before_entry(self):
        journal, record = make_journal("LONG")
        apply_live_prices(journal, {"TESTUSDT": 101.0}, now_ms=2_000)
        apply_live_prices(journal, {"TESTUSDT": 102.1}, now_ms=3_000)
        self.assertEqual(record["status"], "EXPIRED_NO_ENTRY")
        self.assertEqual(record["events"], [])
        apply_live_prices(journal, {"TESTUSDT": 99.9}, now_ms=4_000)
        self.assertEqual(record["status"], "EXPIRED_NO_ENTRY")
        self.assertEqual(record["events"], [])

    def test_short_setup_expires_if_tp1_is_reached_before_entry(self):
        journal, record = make_journal("SHORT")
        apply_live_prices(journal, {"TESTUSDT": 99.0}, now_ms=2_000)
        apply_live_prices(journal, {"TESTUSDT": 97.9}, now_ms=3_000)
        self.assertEqual(record["status"], "EXPIRED_NO_ENTRY")
        self.assertEqual(record["events"], [])

    def test_waiting_entry_expires_after_one_hour(self):
        journal, record = make_journal("LONG")
        apply_live_prices(journal, {}, now_ms=3_601_000)
        self.assertEqual(record["status"], "EXPIRED_NO_ENTRY")
        self.assertEqual(record["events"], [])

    def test_target_one_then_breakeven_stop(self):
        journal, record = make_journal("LONG")
        apply_live_prices(journal, {"TESTUSDT": 101.0}, now_ms=2_000)
        apply_live_prices(journal, {"TESTUSDT": 99.9}, now_ms=3_000)
        apply_live_prices(journal, {"TESTUSDT": 102.1}, now_ms=4_000)
        self.assertEqual(record["status"], "TP1_HIT")
        self.assertEqual([e["event"] for e in record["events"]], ["ENTRY", "TP1"])
        apply_live_prices(journal, {"TESTUSDT": 99.9}, now_ms=5_000)
        self.assertEqual(record["status"], "CLOSED_SL")
        self.assertEqual([e["event"] for e in record["events"]], ["ENTRY", "TP1", "SL"])

    def test_target_two_records_both_targets_once(self):
        journal, record = make_journal("LONG")
        apply_live_prices(journal, {"TESTUSDT": 101.0}, now_ms=2_000)
        apply_live_prices(journal, {"TESTUSDT": 99.9}, now_ms=3_000)
        apply_live_prices(journal, {"TESTUSDT": 104.2}, now_ms=4_000)
        self.assertEqual(record["status"], "CLOSED_TP2")
        self.assertEqual([e["event"] for e in record["events"]], ["ENTRY", "TP1", "TP2"])
        apply_live_prices(journal, {"TESTUSDT": 105.0}, now_ms=5_000)
        self.assertEqual(len(record["events"]), 3)

    def test_stop_loss_is_detected_for_short(self):
        journal, record = make_journal("SHORT")
        apply_live_prices(journal, {"TESTUSDT": 99.0}, now_ms=2_000)
        apply_live_prices(journal, {"TESTUSDT": 100.1}, now_ms=3_000)
        apply_live_prices(journal, {"TESTUSDT": 102.1}, now_ms=4_000)
        self.assertEqual(record["status"], "CLOSED_SL")
        self.assertEqual([e["event"] for e in record["events"]], ["ENTRY", "SL"])

    def test_invalid_prices_are_ignored(self):
        journal, record = make_journal()
        apply_live_prices(journal, {"TESTUSDT": float("nan")}, now_ms=2_000)
        self.assertEqual(record["status"], "WAITING_ENTRY")
        self.assertNotIn("live_last_price", record)


if __name__ == "__main__":
    unittest.main()
