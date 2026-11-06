import unittest

from oi_engine.outcome_tracker import make_trade_record, monitor_journal, summarize_outcomes


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
        entry = [candle(900000, 100, 100.5, 99.5, 100.2)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: entry)
        self.assertEqual(journal["trades"][key]["status"], "OPEN")
        first = [candle(1800000, 100.2, 103, 99.5, 102.5)]
        monitor_journal(journal, now_ms=2700000, fetcher=lambda *_: first)
        self.assertEqual(journal["trades"][key]["status"], "TP1_HIT")
        self.assertEqual([e["event"] for e in journal["trades"][key]["events"]], ["ENTRY", "TP1"])
        second = [candle(2700000, 102.5, 105, 102, 104.5)]
        monitor_journal(journal, now_ms=3600000, fetcher=lambda *_: second)
        self.assertEqual(journal["trades"][key]["status"], "CLOSED_TP2")
        self.assertEqual([e["event"] for e in journal["trades"][key]["events"]], ["ENTRY", "TP1", "TP2"])

    def test_stop_and_target_on_entry_candle_are_marked_ambiguous(self):
        journal, key = self.journal()
        both = [candle(900000, 100, 103, 97, 101)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: both)
        record = journal["trades"][key]
        self.assertEqual(record["status"], "AMBIGUOUS_ENTRY_CANDLE")
        self.assertEqual([e["event"] for e in record["events"]], ["ENTRY", "AMBIGUOUS_ENTRY_CANDLE"])
        self.assertEqual(record["mfe_pct"], 0.0)
        self.assertEqual(record["mae_pct"], 0.0)

    def test_one_minute_intrabar_entry_with_stop_or_target_is_ambiguous(self):
        record = make_trade_record(signal(), {}, 900000)
        journal = {"trades": {record["trade_id"]: record}}
        # Price opens above the LONG limit, then spans entry, stop and target.
        # OHLC cannot prove whether the adverse extreme preceded the entry fill.
        one_minute = [candle(900000, 101, 103, 97, 101)]
        monitor_journal(
            journal, now_ms=960000, fetcher=lambda *_: [],
            micro_fetcher=lambda *_: one_minute,
        )
        result = journal["trades"][record["trade_id"]]
        self.assertEqual(result["status"], "AMBIGUOUS_ENTRY_CANDLE")
        self.assertTrue(result["entry_candle_ambiguous"])
        report = summarize_outcomes(journal)
        self.assertIn("ambiguous_entry_candle", report["groups"])
        self.assertEqual(report["sl_before_tp1"], 0)
        self.assertEqual(report["tp1_before_sl"], 0)

    def test_hybrid_replay_uses_15m_for_old_history_and_1m_for_recent(self):
        journal, key = self.journal()
        journal["trades"][key]["status"] = "OPEN"
        journal["trades"][key]["entry_fill_price"] = 100.0
        journal["trades"][key]["events"] = [{"event": "ENTRY", "time_ms": 900000, "fill": 100.0}]
        now = 100_000_000
        cutoff = ((now - 1400 * 60 * 1000) // 900_000) * 900_000
        calls = {"15m": [], "1m": []}

        def fetch_15m(symbol, start, end):
            calls["15m"].append((start, end))
            return [candle(900000, 101.1, 101.5, 101.0, 101.2)]

        def fetch_1m(symbol, start, end):
            calls["1m"].append((start, end))
            return [candle(cutoff, 101.1, 101.5, 101.0, 101.2)]

        monitor_journal(journal, now_ms=now, fetcher=fetch_15m,
                        micro_fetcher=fetch_1m)
        record = journal["trades"][key]
        self.assertEqual(len(calls["15m"]), 1)
        self.assertEqual(calls["15m"][0][1], cutoff)
        self.assertEqual(calls["1m"], [(cutoff, now)])
        self.assertEqual(record["last_checked_ms"], cutoff + 60_000)
        self.assertEqual(record["status"], "OPEN")

    def test_hybrid_replay_falls_back_when_minute_history_is_truncated_at_start(self):
        journal, key = self.journal()
        journal["trades"][key]["status"] = "OPEN"
        journal["trades"][key]["entry_fill_price"] = 100.0
        journal["trades"][key]["events"] = [{"event": "ENTRY", "time_ms": 900000, "fill": 100.0}]
        now = 100_000_000
        cutoff = ((now - 1400 * 60 * 1000) // 900_000) * 900_000
        fallback_calls = []

        def fetch_15m(symbol, start, end):
            fallback_calls.append((start, end))
            return [candle(start, 101.1, 101.5, 101.0, 101.2)]

        # The rows are internally contiguous, but the provider omitted the
        # first two minutes requested. Treat this as incomplete history.
        minute_rows = [
            candle(cutoff + 120_000, 101.1, 101.5, 101.0, 101.2),
            candle(cutoff + 180_000, 101.1, 101.5, 101.0, 101.2),
        ]
        monitor_journal(
            journal, now_ms=now, fetcher=fetch_15m,
            micro_fetcher=lambda *_: minute_rows,
        )
        self.assertEqual(fallback_calls, [(900000, cutoff), (cutoff, now)])
        self.assertEqual(journal["trades"][key]["status"], "OPEN")

    def test_trade_ids_are_unique_for_multiple_alerts_on_same_candle(self):
        first = make_trade_record(signal(), {}, 900000)
        second = make_trade_record(signal(), {}, 900001)
        self.assertNotEqual(first["trade_id"], second["trade_id"])

    def test_hybrid_replay_falls_back_when_minute_history_has_internal_gap(self):
        journal, key = self.journal()
        journal["trades"][key]["status"] = "OPEN"
        journal["trades"][key]["entry_fill_price"] = 100.0
        journal["trades"][key]["events"] = [{"event": "ENTRY", "time_ms": 900000, "fill": 100.0}]
        now = 100_000_000
        cutoff = ((now - 1400 * 60 * 1000) // 900_000) * 900_000
        fallback_calls = []

        def fetch_15m(symbol, start, end):
            fallback_calls.append((start, end))
            return [candle(start, 101.1, 101.5, 101.0, 101.2)]

        # The response is non-empty but skips one minute. It must not be
        # trusted as a continuous replay window.
        minute_rows = [
            candle(cutoff, 101.1, 101.5, 101.0, 101.2),
            candle(cutoff + 120_000, 101.1, 101.5, 101.0, 101.2),
        ]
        monitor_journal(
            journal, now_ms=now, fetcher=fetch_15m,
            micro_fetcher=lambda *_: minute_rows,
        )
        self.assertEqual(fallback_calls, [(900000, cutoff), (cutoff, now)])
        self.assertEqual(journal["trades"][key]["status"], "OPEN")
        self.assertEqual(journal["trades"][key]["last_checked_ms"], cutoff + 900_000)

    def test_hybrid_replay_falls_back_to_15m_when_minute_history_empty(self):
        journal, key = self.journal()
        journal["trades"][key]["status"] = "OPEN"
        journal["trades"][key]["entry_fill_price"] = 100.0
        journal["trades"][key]["events"] = [{"event": "ENTRY", "time_ms": 900000, "fill": 100.0}]
        now = 100_000_000
        cutoff = ((now - 1400 * 60 * 1000) // 900_000) * 900_000
        calls = []

        def fetch_15m(symbol, start, end):
            calls.append((start, end))
            return [candle(900000, 101.1, 101.5, 101.0, 101.2),
                    candle(cutoff, 101.1, 101.5, 101.0, 101.2)]

        monitor_journal(journal, now_ms=now, fetcher=fetch_15m,
                        micro_fetcher=lambda *_: [])
        self.assertEqual(calls, [(900000, cutoff), (cutoff, now)])
        self.assertEqual(journal["trades"][key]["status"], "OPEN")

    def test_long_moves_remaining_stop_to_entry_after_tp1(self):
        journal, key = self.journal("LONG")
        entry = [candle(900000, 100, 100.5, 99.5, 100.2)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: entry)
        first = [candle(1800000, 100.2, 103, 99.5, 102.5)]
        monitor_journal(journal, now_ms=2700000, fetcher=lambda *_: first)
        record = journal["trades"][key]
        self.assertEqual(record["status"], "TP1_HIT")
        self.assertTrue(record["breakeven_stop_active"])
        self.assertEqual(record["breakeven_stop_price"], 100.0)
        next_candle = [candle(2700000, 101, 101.5, 99.8, 100.2)]
        monitor_journal(journal, now_ms=3600000, fetcher=lambda *_: next_candle)
        record = journal["trades"][key]
        self.assertEqual(record["status"], "CLOSED_SL")
        stop_event = record["events"][-1]
        self.assertEqual(stop_event["event"], "SL")
        self.assertEqual(stop_event["fill"], 100.0)
        self.assertEqual(stop_event["r_multiple"], 0.0)

    def test_short_moves_remaining_stop_to_entry_after_tp1(self):
        journal, key = self.journal("SHORT")
        entry = [candle(900000, 100, 100.5, 99.5, 100.2)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: entry)
        first = [candle(1800000, 100, 100.5, 97, 97.5)]
        monitor_journal(journal, now_ms=2700000, fetcher=lambda *_: first)
        record = journal["trades"][key]
        self.assertEqual(record["status"], "TP1_HIT")
        self.assertTrue(record["breakeven_stop_active"])
        self.assertEqual(record["breakeven_stop_price"], 100.0)
        next_candle = [candle(2700000, 99, 100.2, 98.5, 99.8)]
        monitor_journal(journal, now_ms=3600000, fetcher=lambda *_: next_candle)
        record = journal["trades"][key]
        self.assertEqual(record["status"], "CLOSED_SL")
        stop_event = record["events"][-1]
        self.assertEqual(stop_event["event"], "SL")
        self.assertEqual(stop_event["fill"], 100.0)
        self.assertEqual(stop_event["r_multiple"], 0.0)

    def test_short_tp1_then_tp2(self):
        journal, key = self.journal("SHORT")
        entry = [candle(900000, 100, 100.5, 99.5, 100.2)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: entry)
        first = [candle(1800000, 100, 101, 97, 97.5)]
        monitor_journal(journal, now_ms=2700000, fetcher=lambda *_: first)
        self.assertEqual(journal["trades"][key]["status"], "TP1_HIT")
        second = [candle(2700000, 97, 98, 95, 95.5)]
        monitor_journal(journal, now_ms=3600000, fetcher=lambda *_: second)
        self.assertEqual(journal["trades"][key]["status"], "CLOSED_TP2")

    def test_stopped_trade_tracks_if_original_targets_are_reached_later(self):
        journal, key = self.journal("LONG")
        entry = [candle(900000, 100, 100.5, 99.5, 100)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: entry)
        stop_candle = [candle(1800000, 100, 101, 97, 98)]
        monitor_journal(journal, now_ms=2700000, fetcher=lambda *_: stop_candle)
        record = journal["trades"][key]
        self.assertEqual(record["status"], "CLOSED_SL")
        self.assertEqual(record["post_stop_tracking"], "ACTIVE")
        later = [candle(2700000, 99, 105, 98.5, 104)]
        monitor_journal(journal, now_ms=3600000, fetcher=lambda *_: later)
        self.assertEqual(record["status"], "CLOSED_SL")
        self.assertTrue(record["post_stop_tp1_reached"])
        self.assertTrue(record["post_stop_tp2_reached"])
        self.assertGreater(record["post_stop_mfe_pct"], 0)

    def test_post_stop_tracking_is_supported_for_short(self):
        journal, key = self.journal("SHORT")
        entry = [candle(900000, 100, 100.5, 99.5, 100)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: entry)
        stop_candle = [candle(1800000, 103, 103, 99, 102)]
        monitor_journal(journal, now_ms=2700000, fetcher=lambda *_: stop_candle)
        record = journal["trades"][key]
        later = [candle(2700000, 101, 101.5, 95, 96)]
        monitor_journal(journal, now_ms=3600000, fetcher=lambda *_: later)
        self.assertTrue(record["post_stop_tp1_reached"])
        self.assertTrue(record["post_stop_tp2_reached"])
        self.assertEqual(record["status"], "CLOSED_SL")

    def test_gap_through_stop_uses_open_price(self):
        journal, key = self.journal()
        entry_touch = [candle(900000, 100, 100.5, 99.5, 100)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: entry_touch)
        gap = [candle(1800000, 97, 99, 96, 98)]
        monitor_journal(journal, now_ms=2700000, fetcher=lambda *_: gap)
        event = journal["trades"][key]["events"][-1]
        self.assertEqual(event["event"], "SL")
        self.assertEqual(event["fill"], 97.0)

    def test_open_trade_skips_provider_calls_until_next_15m_candle_closes(self):
        journal, key = self.journal()
        calls = []
        fetcher = lambda *args: calls.append(args) or []
        monitor_journal(journal, now_ms=1799999, fetcher=fetcher)
        self.assertEqual(calls, [])
        self.assertEqual(journal["trades"][key]["status"], "WAITING_ENTRY")
        monitor_journal(journal, now_ms=1800000, fetcher=fetcher)
        self.assertEqual(len(calls), 1)

    def test_stopped_trade_skips_post_stop_calls_between_15m_closes(self):
        journal, key = self.journal()
        entry_candle = [candle(900000, 100, 100.5, 99.5, 100)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: entry_candle)
        stop_candle = [candle(1800000, 100, 101, 97, 98)]
        monitor_journal(journal, now_ms=2700000, fetcher=lambda *_: stop_candle)
        calls = []
        fetcher = lambda *args: calls.append(args) or []
        monitor_journal(journal, now_ms=2900000, fetcher=fetcher)
        self.assertEqual(calls, [])
        monitor_journal(journal, now_ms=3600000, fetcher=fetcher)
        self.assertEqual(len(calls), 1)

    def test_provider_error_is_retained_without_closing_trade(self):
        journal, key = self.journal()
        def fail(*_):
            raise RuntimeError("provider unavailable")
        monitor_journal(journal, now_ms=1800000, fetcher=fail)
        record = journal["trades"][key]
        self.assertEqual(record["status"], "WAITING_ENTRY")
        self.assertIn("provider unavailable", record["last_error"])

    def test_trade_report_exposes_entry_and_stop_diagnostics(self):
        source = signal()
        source.update({
            "movement_strength": 78, "entry_quality": 72,
            "atr_15m": 1.25, "stop_distance_atr": 1.1,
            "setup_level": 100.5, "pullback_extreme": 99.0,
            "ema20": 101.0, "ema50": 100.0,
        })
        record = make_trade_record(source, {}, 900000)
        record["status"] = "CLOSED_SL"
        record["entry_fill_price"] = 100.0
        record["events"] = [
            {"event": "ENTRY", "time_ms": 900000, "fill": 100.0},
            {"event": "SL", "time_ms": 1800000, "fill": 98.0},
        ]
        record["mae_before_tp1_pct"] = 2.0
        record["mfe_before_tp1_pct"] = 0.5
        report = summarize_outcomes({"trades": {record["trade_id"]: record}})
        detail = report["trade_details"][0]
        self.assertEqual(detail["side"], "LONG")
        self.assertEqual(detail["entry_quality"], 72)
        self.assertEqual(detail["stop_distance_atr"], 1.1)
        self.assertEqual(detail["pullback_extreme"], 99.0)
        self.assertEqual(detail["risk_pct"], 2.0)
        self.assertEqual(report["groups"]["sl_before_tp1"]["stop_distance_atr_avg"], 1.1)

    def test_signal_scores_are_preserved_and_reported_by_outcome(self):
        source = signal()
        source.update({"movement_strength": 78, "entry_quality": 42, "engine": "OI"})
        record = make_trade_record(source, {"oi_change_15m_window_pct": 0.4}, 900000)
        self.assertEqual(record["signal"]["movement_strength"], 78)
        self.assertEqual(record["signal"]["entry_quality"], 42)
        record["entry_fill_price"] = 100.0
        record["events"] = [
            {"event": "ENTRY", "time_ms": 999999, "fill": 100.0},
            {"event": "TP1", "time_ms": 1000000, "fill": 102.0},
        ]
        record["status"] = "TP1_HIT"
        report = summarize_outcomes({"trades": {record["trade_id"]: record}})
        self.assertEqual(report["tp1_before_sl"], 1)
        self.assertEqual(report["groups"]["tp1_before_sl"]["movement_strength_avg"], 78.0)
        self.assertEqual(report["groups"]["tp1_before_sl"]["entry_quality_avg"], 42.0)
        self.assertEqual(report["by_engine_side"]["OI:LONG"]["total"], 1)

    def test_same_candle_stop_target_is_explicitly_flagged(self):
        record = make_trade_record(signal(), {}, 900000)
        journal = {"trades": {record["trade_id"]: record}}
        both = [candle(900000, 100, 103, 97, 101)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: both)
        event = journal["trades"][record["trade_id"]]["events"][-1]
        self.assertTrue(event["ambiguous_same_candle"])
        self.assertEqual(event["same_candle_targets"], ["TP1"])
        report = summarize_outcomes(journal)
        self.assertEqual(report["ambiguous_stop_target"], 1)
        self.assertEqual(report["sl_before_tp1"], 0)

    def test_mae_before_tp1_excludes_ambiguous_target_candle(self):
        journal, key = self.journal("LONG")
        candles = [
            candle(900000, 100, 100.5, 99.5, 100.2),
            candle(1800000, 100.2, 100.5, 99.5, 100.1),
            candle(2700000, 100.1, 103, 98.5, 102.5),
        ]
        monitor_journal(journal, now_ms=3600000, fetcher=lambda *_: candles)
        record = journal["trades"][key]
        self.assertAlmostEqual(record["mae_before_tp1_pct"], 0.5)
        self.assertAlmostEqual(record["mfe_before_tp1_pct"], 0.5)
        self.assertTrue(record["tp1_candle_excursion_ambiguous"])
        self.assertEqual(record["status"], "TP1_HIT")

    def test_unfilled_entry_expires_after_one_hour(self):
        journal, key = self.journal()
        monitor_journal(journal, now_ms=900000 + 60 * 60 * 1000,
                        fetcher=lambda *_: [])
        self.assertEqual(journal["trades"][key]["status"], "EXPIRED_NO_ENTRY")


    def test_signal_waits_until_entry_is_reached(self):
        journal, key = self.journal()
        before_entry = [candle(900000, 101, 103, 100.5, 102)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: before_entry)
        record = journal["trades"][key]
        self.assertEqual(record["status"], "WAITING_ENTRY")
        self.assertFalse(any(e["event"] == "ENTRY" for e in record["events"]))
        self.assertEqual(record["mfe_pct"], 0.0)
        self.assertEqual(record["mae_pct"], 0.0)

    def test_intrabar_entry_candle_without_exit_does_not_count_pre_entry_excursions(self):
        journal, key = self.journal()
        # Opens above the LONG limit, then trades through entry without
        # touching either stop or target. Extremes before the fill are unknown.
        one_minute = [candle(900000, 101, 101.5, 99.5, 100.5)]
        monitor_journal(
            journal, now_ms=960000, fetcher=lambda *_: [],
            micro_fetcher=lambda *_: one_minute,
        )
        record = journal["trades"][key]
        self.assertEqual(record["status"], "OPEN")
        self.assertTrue(record["entry_candle_excursion_ambiguous"])
        self.assertEqual(record["mfe_pct"], 0.0)
        self.assertEqual(record["mae_pct"], 0.0)
        self.assertEqual(record["last_checked_ms"], 960000)

    def test_entry_is_recorded_only_when_candle_reaches_level(self):
        journal, key = self.journal()
        touched = [candle(900000, 101, 102, 99.5, 100.5)]
        monitor_journal(journal, now_ms=1800000, fetcher=lambda *_: touched)
        record = journal["trades"][key]
        self.assertEqual(record["status"], "AMBIGUOUS_ENTRY_CANDLE")
        self.assertEqual(record["entry_fill_price"], 100.0)
        self.assertEqual([event["event"] for event in record["events"]],
                         ["ENTRY", "AMBIGUOUS_ENTRY_CANDLE"])

    def test_summary_recognizes_legacy_records_without_entry_events(self):
        legacy_sl = make_trade_record(signal(), {}, 900000)
        legacy_sl["events"] = [{"event": "SL", "time_ms": 1000000, "fill": 98.0}]
        legacy_sl["status"] = "CLOSED_SL"

        legacy_tp1 = make_trade_record(
            {**signal(), "candle_close_time": 1800000}, {}, 1800000
        )
        legacy_tp1["events"] = [{"event": "TP1", "time_ms": 2000000, "fill": 102.0}]
        legacy_tp1["status"] = "TP1_HIT"

        waiting = make_trade_record(
            {**signal(), "candle_close_time": 2700000}, {}, 2700000
        )
        report = summarize_outcomes({"trades": {
            legacy_sl["trade_id"]: legacy_sl,
            legacy_tp1["trade_id"]: legacy_tp1,
            waiting["trade_id"]: waiting,
        }})

        # Historical outcome labels without an auditable ENTRY fill are
        # excluded rather than retroactively counted as real trades.
        self.assertEqual(report["entry_touched"], 0)
        self.assertEqual(report["not_entered"], 3)
        self.assertEqual(report["entry_touched_decided"], 0)
        self.assertEqual(report["sl_before_tp1"], 0)
        self.assertEqual(report["tp1_before_sl"], 0)
        self.assertIsNone(report["entry_touched_win_rate_pct"])

    def test_summary_win_rate_uses_only_entered_and_decided_trades(self):
        entered = make_trade_record(signal(), {}, 900000)
        entered["events"] = [
            {"event": "ENTRY", "time_ms": 900001, "fill": 100.0},
            {"event": "TP1", "time_ms": 1000000, "fill": 102.0},
        ]
        entered["entry_fill_price"] = 100.0
        entered["status"] = "TP1_HIT"
        waiting = make_trade_record({**signal(), "candle_close_time": 1800000}, {}, 1800000)
        report = summarize_outcomes({"trades": {
            entered["trade_id"]: entered, waiting["trade_id"]: waiting
        }})
        self.assertEqual(report["entry_touched"], 1)
        self.assertEqual(report["not_entered"], 1)
        self.assertEqual(report["entry_touched_decided"], 1)
        self.assertEqual(report["entry_touched_win_rate_pct"], 100.0)


    def test_price_touch_before_telegram_alert_is_not_counted_as_entry(self):
        sent_at_ms = 950000
        record = make_trade_record(signal(), {}, sent_at_ms)
        self.assertEqual(record["last_checked_ms"], sent_at_ms)
        journal = {"trades": {record["trade_id"]: record}}

        # The 15:00 minute touched the entry before the alert was sent.
        # The first complete minute after delivery did not touch the entry.
        minute_rows = [
            candle(900000, 100, 103, 97, 101),
            candle(960000, 101, 101.5, 100.2, 101.1),
        ]
        monitor_journal(
            journal,
            now_ms=1020000,
            fetcher=lambda *_: [],
            micro_fetcher=lambda *_: minute_rows,
        )
        result = journal["trades"][record["trade_id"]]
        self.assertEqual(result["status"], "WAITING_ENTRY")
        self.assertEqual(result["events"], [])


    def test_long_limit_entry_fills_when_candle_opens_below_entry(self):
        journal, key = self.journal("LONG")
        gap_candle = [900000, "99.0", "99.5", "98.5", "99.2", "1"]
        monitor_journal(
            journal, now_ms=960000, fetcher=lambda *_: [],
            micro_fetcher=lambda *_: [gap_candle],
        )
        record = journal["trades"][key]
        self.assertEqual(record["status"], "OPEN")
        self.assertEqual(record["entry_fill_price"], 99.0)
        self.assertEqual([event["event"] for event in record["events"]], ["ENTRY"])

    def test_short_limit_entry_fills_when_candle_opens_above_entry(self):
        journal, key = self.journal("SHORT")
        gap_candle = [900000, "101.0", "101.5", "100.5", "100.8", "1"]
        monitor_journal(
            journal, now_ms=960000, fetcher=lambda *_: [],
            micro_fetcher=lambda *_: [gap_candle],
        )
        record = journal["trades"][key]
        self.assertEqual(record["status"], "OPEN")
        self.assertEqual(record["entry_fill_price"], 101.0)
        self.assertEqual([event["event"] for event in record["events"]], ["ENTRY"])


if __name__ == "__main__":
    unittest.main()
