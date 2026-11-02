import unittest
from unittest.mock import patch
import csv
import tempfile
from pathlib import Path
from types import SimpleNamespace

from oi_engine.backtest import align_oi, download_history, load_csv_history, reprice_signal_for_next_open, simulate_trade, summarize
from oi_engine.signal_engine import MarketDataError


class BacktestTests(unittest.TestCase):
    def test_oi_alignment_never_uses_future_sample(self):
        candles = [{"close_time": 899999}, {"close_time": 1799999}, {"close_time": 2699999}]
        rows = [
            {"timestamp": 0, "sumOpenInterest": "100"},
            {"timestamp": 900000, "sumOpenInterest": "101"},
            {"timestamp": 1800000, "sumOpenInterest": "102"},
            {"timestamp": 2700000, "sumOpenInterest": "103"},
        ]
        aligned = align_oi(candles, rows)
        self.assertIsNone(aligned[0])
        self.assertEqual(aligned[1], [100.0, 101.0, 102.0])
        self.assertEqual(aligned[2], [101.0, 102.0, 103.0])

    def test_oi_alignment_rejects_missing_15m_sample(self):
        candle = [{"close_time": 3599999}]
        rows = [
            {"timestamp": 0, "sumOpenInterest": "100"},
            {"timestamp": 900000, "sumOpenInterest": "101"},
            # Missing the 15m sample at 1,800,000.
            {"timestamp": 2700000, "sumOpenInterest": "103"},
        ]
        self.assertIsNone(align_oi(candle, rows)[0])

    def test_oi_alignment_rejects_irregular_short_intervals(self):
        candle = [{"close_time": 2699999}]
        rows = [
            {"timestamp": 0, "sumOpenInterest": "100"},
            {"timestamp": 600000, "sumOpenInterest": "101"},
            {"timestamp": 1200000, "sumOpenInterest": "102"},
        ]
        self.assertIsNone(align_oi(candle, rows)[0])

    def test_entry_is_repriced_to_next_candle_open_preserving_risk(self):
        signal = SimpleNamespace(
            symbol="XUSDT", side="LONG", entry=100.0, stop_loss=98.0,
            take_profit_1=102.0, take_profit_2=104.0, candle_close_time=1
        )
        executable = reprice_signal_for_next_open(signal, 101.0)
        self.assertEqual(executable.entry, 101.0)
        self.assertEqual(executable.stop_loss, 99.0)
        self.assertEqual(executable.take_profit_1, 103.0)
        self.assertEqual(executable.take_profit_2, 105.0)
        self.assertEqual(signal.entry, 100.0)

    def test_next_open_repricing_rejects_invalid_price(self):
        signal = SimpleNamespace(
            symbol="XUSDT", side="LONG", entry=100.0, stop_loss=98.0,
            take_profit_1=102.0, take_profit_2=104.0, candle_close_time=1
        )
        with self.assertRaisesRegex(MarketDataError, "positive"):
            reprice_signal_for_next_open(signal, 0.0)

    def test_stop_wins_when_stop_and_target_touch_same_bar(self):
        signal = SimpleNamespace(
            symbol="XUSDT", side="LONG", entry=100.0, stop_loss=98.0,
            take_profit_1=102.0, take_profit_2=104.0, candle_close_time=1
        )
        future = [{"high": 105.0, "low": 97.0, "close": 101.0}]
        result = simulate_trade(signal, future, fee_bps=0, slippage_bps=0)
        self.assertEqual(result["gross_r"], -1.0)
        self.assertEqual(result["exit_reason"], "stop")
        self.assertEqual(result["bars_held"], 1)

    def test_long_stop_gap_fills_at_worse_open(self):
        signal = SimpleNamespace(
            symbol="XUSDT", side="LONG", entry=100.0, stop_loss=98.0,
            take_profit_1=102.0, take_profit_2=104.0, candle_close_time=1
        )
        result = simulate_trade(
            signal, [{"open": 96.0, "high": 97.0, "low": 95.0, "close": 96.5}],
            fee_bps=0, slippage_bps=0
        )
        self.assertEqual(result["exit_reason"], "gap_stop")
        self.assertEqual(result["exit_price"], 96.0)
        self.assertEqual(result["gross_r"], -2.0)

    def test_short_stop_gap_fills_at_worse_open(self):
        signal = SimpleNamespace(
            symbol="XUSDT", side="SHORT", entry=100.0, stop_loss=102.0,
            take_profit_1=98.0, take_profit_2=96.0, candle_close_time=1
        )
        result = simulate_trade(
            signal, [{"open": 104.0, "high": 105.0, "low": 103.0, "close": 104.5}],
            fee_bps=0, slippage_bps=0
        )
        self.assertEqual(result["exit_reason"], "gap_stop")
        self.assertEqual(result["exit_price"], 104.0)
        self.assertEqual(result["gross_r"], -2.0)

    def test_gap_through_tp2_gets_opening_price_improvement(self):
        signal = SimpleNamespace(
            symbol="XUSDT", side="LONG", entry=100.0, stop_loss=98.0,
            take_profit_1=102.0, take_profit_2=104.0, candle_close_time=1
        )
        result = simulate_trade(
            signal, [{"open": 105.0, "high": 106.0, "low": 104.5, "close": 105.5}],
            fee_bps=0, slippage_bps=0
        )
        self.assertEqual(result["exit_reason"], "gap_tp2")
        self.assertEqual(result["exit_price"], 105.0)
        self.assertEqual(result["gross_r"], 2.5)

    def test_gap_stop_after_tp1_marks_remaining_half_at_gap_open(self):
        signal = SimpleNamespace(
            symbol="XUSDT", side="LONG", entry=100.0, stop_loss=98.0,
            take_profit_1=102.0, take_profit_2=104.0, candle_close_time=1
        )
        future = [
            {"open": 100.0, "high": 102.5, "low": 99.5, "close": 102.0},
            {"open": 95.0, "high": 96.0, "low": 94.0, "close": 95.5},
        ]
        result = simulate_trade(signal, future, fee_bps=0, slippage_bps=0)
        self.assertEqual(result["exit_reason"], "gap_stop_after_tp1")
        self.assertEqual(result["exit_price"], 95.0)
        self.assertEqual(result["gross_r"], -0.75)
        self.assertEqual(result["bars_held"], 2)

    def test_tp1_then_stop_is_zero_gross_r(self):
        signal = SimpleNamespace(
            symbol="XUSDT", side="LONG", entry=100.0, stop_loss=98.0,
            take_profit_1=102.0, take_profit_2=104.0, candle_close_time=1
        )
        future = [
            {"high": 102.5, "low": 99.5, "close": 102.0},
            {"high": 102.0, "low": 97.5, "close": 99.0},
        ]
        result = simulate_trade(signal, future, fee_bps=0, slippage_bps=0)
        self.assertEqual(result["gross_r"], 0.0)
        self.assertEqual(result["exit_reason"], "stop_after_tp1")

    def test_summary_reports_long_and_short_metrics_separately(self):
        metrics = summarize([
            {"net_r": 1.0, "side": "LONG"},
            {"net_r": -1.0, "side": "LONG"},
            {"net_r": 2.0, "side": "SHORT"},
        ])
        self.assertEqual(metrics["by_side"]["LONG"]["trades"], 2)
        self.assertEqual(metrics["by_side"]["LONG"]["win_rate_pct"], 50.0)
        self.assertEqual(metrics["by_side"]["SHORT"]["trades"], 1)
        self.assertEqual(metrics["by_side"]["SHORT"]["win_rate_pct"], 100.0)
        self.assertEqual(metrics["by_side"]["SHORT"]["expectancy_r"], 2.0)

    def test_summary_reports_actual_metrics(self):
        metrics = summarize([{"net_r": 1.0, "side": "LONG"},
                             {"net_r": -1.0, "side": "SHORT"}])
        self.assertEqual(metrics["trades"], 2)
        self.assertEqual(metrics["win_rate_pct"], 50.0)
        self.assertEqual(metrics["expectancy_r"], 0.0)



    def test_load_authorized_csv_history(self):
        with tempfile.TemporaryDirectory() as directory:
            candles_path = Path(directory) / "candles.csv"
            oi_path = Path(directory) / "oi.csv"
            fields = ["open_time", "close_time", "open", "high", "low", "close",
                      "volume", "quote_volume", "taker_buy_quote"]
            with candles_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for index in range(22):
                    writer.writerow({
                        "open_time": index * 900000,
                        "close_time": (index + 1) * 900000 - 1,
                        "open": 100, "high": 101, "low": 99, "close": 100,
                        "volume": 10, "quote_volume": 1000, "taker_buy_quote": 520,
                    })
            with oi_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["timestamp", "sumOpenInterest"])
                writer.writeheader()
                for index in range(22):
                    writer.writerow({"timestamp": index * 900000, "sumOpenInterest": 1000 + index})
            candles, oi_rows = load_csv_history(str(candles_path), str(oi_path))
            self.assertEqual(len(candles), 22)
            self.assertEqual(len(oi_rows), 22)
            self.assertEqual(candles[-1]["taker_buy_quote"], 520.0)

    def test_csv_loader_rejects_missing_15m_candle_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            candles_path = Path(directory) / "candles.csv"
            oi_path = Path(directory) / "oi.csv"
            fields = ["open_time", "close_time", "open", "high", "low", "close",
                      "volume", "quote_volume", "taker_buy_quote"]
            with candles_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for index in range(22):
                    # Leave a 30-minute hole at candle 10.
                    actual_index = index + (1 if index >= 10 else 0)
                    writer.writerow({
                        "open_time": actual_index * 900000,
                        "close_time": (actual_index + 1) * 900000 - 1,
                        "open": 100, "high": 101, "low": 99, "close": 100,
                        "volume": 10, "quote_volume": 1000, "taker_buy_quote": 520,
                    })
            with oi_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["timestamp", "sumOpenInterest"])
                writer.writeheader()
                for index in range(22):
                    writer.writerow({"timestamp": index * 900000, "sumOpenInterest": 1000 + index})
            with self.assertRaisesRegex(MarketDataError, "missing or irregular 15m"):
                load_csv_history(str(candles_path), str(oi_path))

    def test_csv_loader_rejects_invalid_ohlc_relationship(self):
        with tempfile.TemporaryDirectory() as directory:
            candles_path = Path(directory) / "candles.csv"
            oi_path = Path(directory) / "oi.csv"
            fields = ["open_time", "close_time", "open", "high", "low", "close",
                      "volume", "quote_volume", "taker_buy_quote"]
            with candles_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for index in range(22):
                    writer.writerow({
                        "open_time": index * 900000,
                        "close_time": (index + 1) * 900000 - 1,
                        "open": 100, "high": 99, "low": 98, "close": 100,
                        "volume": 10, "quote_volume": 1000, "taker_buy_quote": 520,
                    })
            with oi_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["timestamp", "sumOpenInterest"])
                writer.writeheader()
                for index in range(22):
                    writer.writerow({"timestamp": index * 900000, "sumOpenInterest": 1000 + index})
            with self.assertRaisesRegex(MarketDataError, "OHLC"):
                load_csv_history(str(candles_path), str(oi_path))

    def test_csv_loader_rejects_missing_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            candles_path = Path(directory) / "candles.csv"
            oi_path = Path(directory) / "oi.csv"
            candles_path.write_text("open_time,close_time,close\\n1,2,3\\n", encoding="utf-8")
            oi_path.write_text("timestamp,sumOpenInterest\\n1,100\\n", encoding="utf-8")
            with self.assertRaises(MarketDataError):
                load_csv_history(str(candles_path), str(oi_path))

    def test_csv_loader_rejects_out_of_order_candles_and_oi(self):
        with tempfile.TemporaryDirectory() as directory:
            candles_path = Path(directory) / "candles.csv"
            oi_path = Path(directory) / "oi.csv"
            candle_fields = ["open_time", "close_time", "open", "high", "low", "close",
                             "volume", "quote_volume", "taker_buy_quote"]
            candle_rows = [{
                "open_time": i * 900000, "close_time": (i + 1) * 900000 - 1,
                "open": 100, "high": 101, "low": 99, "close": 100,
                "volume": 10, "quote_volume": 1000, "taker_buy_quote": 520,
            } for i in range(22)]
            candle_rows[0], candle_rows[1] = candle_rows[1], candle_rows[0]
            with candles_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=candle_fields)
                writer.writeheader()
                writer.writerows(candle_rows)
            with oi_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["timestamp", "sumOpenInterest"])
                writer.writeheader()
                for i in range(22):
                    writer.writerow({"timestamp": i * 900000, "sumOpenInterest": 1000 + i})
            with self.assertRaisesRegex(MarketDataError, "chronological order"):
                load_csv_history(str(candles_path), str(oi_path))

            # Restore candles and independently verify OI ordering.
            candle_rows.reverse()
            with candles_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=candle_fields)
                writer.writeheader()
                writer.writerows(candle_rows)
            oi_rows = [{"timestamp": i * 900000, "sumOpenInterest": 1000 + i}
                       for i in range(22)]
            oi_rows[0], oi_rows[1] = oi_rows[1], oi_rows[0]
            with oi_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["timestamp", "sumOpenInterest"])
                writer.writeheader()
                writer.writerows(oi_rows)
            with self.assertRaisesRegex(MarketDataError, "chronological order"):
                load_csv_history(str(candles_path), str(oi_path))


    def test_download_history_pages_klines_and_oi(self):
        interval = 900000
        now_ms = 1_800_000_000_000
        start = now_ms - 29 * 24 * 60 * 60 * 1000
        start -= start % interval

        def kline(open_time):
            return [open_time, "100", "101", "99", "100", "10",
                    open_time + interval - 1, "1000", 10, "5", "520", "0"]

        def oi(timestamp):
            return {"timestamp": timestamp, "sumOpenInterest": "1000"}

        first_klines = [kline(start + i * interval) for i in range(1500)]
        second_klines = [kline(start + i * interval) for i in range(1500, 1502)]
        first_oi = [oi(start + (i + 1) * interval) for i in range(500)]
        second_oi = [oi(start + (i + 1) * interval) for i in range(500, 502)]

        class Response:
            status_code = 200
            def __init__(self, payload):
                self.payload = payload
            def raise_for_status(self):
                return None
            def json(self):
                return self.payload

        class Session:
            def __init__(self):
                self.headers = {}
                self.calls = []
                self.responses = [
                    Response(first_klines), Response(second_klines),
                    Response(first_oi), Response(second_oi),
                ]
            def get(self, url, params, timeout):
                self.calls.append((url, dict(params)))
                return self.responses.pop(0)

        session = Session()
        with patch("oi_engine.backtest.requests.Session", return_value=session), \
             patch("time.time", return_value=now_ms / 1000):
            candles, oi_rows = download_history("BTCUSDT")

        self.assertEqual(len(candles), 1502)
        self.assertEqual(len(oi_rows), 502)
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(session.calls[1][1]["startTime"],
                         start + 1500 * interval)
        self.assertEqual(session.calls[3][1]["startTime"],
                         start + 500 * interval + 1)


if __name__ == "__main__":
    unittest.main()
