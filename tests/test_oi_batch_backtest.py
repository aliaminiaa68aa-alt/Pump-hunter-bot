import csv
import tempfile
import unittest
from pathlib import Path

from oi_engine.batch_backtest import run_batch_backtest


class BatchBacktestTests(unittest.TestCase):
    def _write_pair(self, root: Path, symbol: str) -> None:
        candle_path = root / f"{symbol.lower()}_candles.csv"
        oi_path = root / f"{symbol.lower()}_oi.csv"
        candle_fields = [
            "open_time", "close_time", "open", "high", "low", "close",
            "volume", "quote_volume", "taker_buy_quote",
        ]
        with candle_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=candle_fields)
            writer.writeheader()
            for i in range(22):
                writer.writerow({
                    "open_time": i * 900000,
                    "close_time": (i + 1) * 900000 - 1,
                    "open": 100, "high": 101, "low": 99, "close": 100,
                    "volume": 10, "quote_volume": 1000, "taker_buy_quote": 500,
                })
        with oi_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["timestamp", "sumOpenInterest"])
            writer.writeheader()
            for i in range(22):
                writer.writerow({"timestamp": i * 900000, "sumOpenInterest": 1000 + i})

    def test_batch_runs_available_symbol_and_reports_missing_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_pair(root, "BTCUSDT")
            report = run_batch_backtest(["BTCUSDT", "ETHUSDT"], directory)
            self.assertEqual(report["symbols_succeeded"], ["BTCUSDT"])
            self.assertIn("ETHUSDT", report["symbols_failed"])
            self.assertEqual(report["per_symbol"]["BTCUSDT"]["metrics"]["trades"], 0)
            self.assertEqual(report["pooled_trade_count"], 0)
            self.assertIsNone(report["pooled_equal_risk_metrics"]["max_drawdown_r"])
            for side_metrics in report["pooled_equal_risk_metrics"]["by_side"].values():
                self.assertIsNone(side_metrics["max_drawdown_r"])
            self.assertEqual(
                report["per_symbol"]["BTCUSDT"]["sample_assessment"], "no_signals"
            )
            self.assertIn("omit pooled drawdown", report["pooled_metrics_note"])

    def test_invalid_symbol_is_reported_without_aborting(self):
        report = run_batch_backtest(["BTCUSD"])
        self.assertIn("BTCUSD", report["symbols_failed"])
        self.assertEqual(report["symbols_succeeded"], [])

    def test_rejects_negative_or_non_finite_cost_assumptions(self):
        for fee, slippage in [(-1, 2), (5, -0.1), (float("inf"), 2), (5, float("nan"))]:
            with self.subTest(fee=fee, slippage=slippage):
                with self.assertRaises(ValueError):
                    run_batch_backtest(["BTCUSDT"], fee_bps=fee, slippage_bps=slippage)

    def test_rejects_duplicate_symbols_to_prevent_double_counting(self):
        with self.assertRaisesRegex(ValueError, "Duplicate symbols"):
            run_batch_backtest(["BTCUSDT", "btcusdt"])


if __name__ == "__main__":
    unittest.main()
