import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oi_engine.export_history import export_symbol, export_symbols
from oi_engine.signal_engine import MarketDataError


class ExportHistoryTests(unittest.TestCase):
    def test_exports_exact_backtest_csv_schemas(self):
        candles = [{
            "open_time": i * 900000,
            "close_time": (i + 1) * 900000 - 1,
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
            "volume": 10.0, "quote_volume": 1000.0, "taker_buy_quote": 530.0,
        } for i in range(22)]
        oi_rows = [
            {"timestamp": i * 900000, "sumOpenInterest": str(1000 + i)}
            for i in range(22)
        ]
        with tempfile.TemporaryDirectory() as directory:
            with patch("oi_engine.export_history.download_history", return_value=(candles, oi_rows)):
                candle_path, oi_path = export_symbol("btcusdt", directory)
            with candle_path.open(newline="", encoding="utf-8") as handle:
                candle_reader = csv.DictReader(handle)
                self.assertEqual(candle_reader.fieldnames, [
                    "open_time", "close_time", "open", "high", "low", "close",
                    "volume", "quote_volume", "taker_buy_quote",
                ])
                self.assertEqual(len(list(candle_reader)), 22)
            with oi_path.open(newline="", encoding="utf-8") as handle:
                oi_reader = csv.DictReader(handle)
                self.assertEqual(oi_reader.fieldnames, ["timestamp", "sumOpenInterest"])
                self.assertEqual(len(list(oi_reader)), 22)

    def test_batch_export_continues_after_one_symbol_fails(self):
        candles = [{
            "open_time": i * 900000,
            "close_time": (i + 1) * 900000 - 1,
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
            "volume": 10.0, "quote_volume": 1000.0, "taker_buy_quote": 530.0,
        } for i in range(22)]
        oi_rows = [
            {"timestamp": i * 900000, "sumOpenInterest": str(1000 + i)}
            for i in range(22)
        ]
        def fake_download(symbol):
            if symbol == "ETHUSDT":
                raise MarketDataError("fixture failure")
            return candles, oi_rows
        with tempfile.TemporaryDirectory() as directory:
            with patch("oi_engine.export_history.download_history", side_effect=fake_download):
                result = export_symbols(["BTCUSDT", "ETHUSDT"], directory)
            self.assertEqual(result["BTCUSDT"], "success")
            self.assertIn("failed:", result["ETHUSDT"])
            self.assertTrue((Path(directory) / "btcusdt_candles.csv").exists())

    def test_rejects_non_usdt_symbol(self):
        with self.assertRaisesRegex(MarketDataError, "USDT perpetual"):
            export_symbol("BTCUSD")

    def test_rejects_short_candle_history(self):
        with patch("oi_engine.export_history.download_history", return_value=([], [])):
            with self.assertRaisesRegex(MarketDataError, "closed candles"):
                export_symbol("BTCUSDT")


if __name__ == "__main__":
    unittest.main()
