import unittest
from types import SimpleNamespace

from oi_engine.backtest import align_oi, simulate_trade, summarize


class BacktestTests(unittest.TestCase):
    def test_oi_alignment_never_uses_future_sample(self):
        candles = [{"close_time": 899999}, {"close_time": 1799999}, {"close_time": 2699999}]
        rows = [
            {"timestamp": 0, "sumOpenInterestValue": "100"},
            {"timestamp": 900000, "sumOpenInterestValue": "101"},
            {"timestamp": 1800000, "sumOpenInterestValue": "102"},
            {"timestamp": 2700000, "sumOpenInterestValue": "103"},
        ]
        aligned = align_oi(candles, rows)
        self.assertIsNone(aligned[0])
        self.assertIsNone(aligned[1])  # timestamp 1,800,000 is still in the future
        self.assertEqual(aligned[2], [100.0, 101.0, 102.0])

    def test_stop_wins_when_stop_and_target_touch_same_bar(self):
        signal = SimpleNamespace(
            symbol="XUSDT", side="LONG", entry=100.0, stop_loss=98.0,
            take_profit_1=102.0, take_profit_2=104.0, candle_close_time=1
        )
        future = [{"high": 105.0, "low": 97.0, "close": 101.0}]
        result = simulate_trade(signal, future, fee_bps=0, slippage_bps=0)
        self.assertEqual(result["gross_r"], -1.0)
        self.assertEqual(result["exit_reason"], "stop")

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

    def test_summary_reports_actual_metrics(self):
        metrics = summarize([{"net_r": 1.0, "side": "LONG"},
                             {"net_r": -1.0, "side": "SHORT"}])
        self.assertEqual(metrics["trades"], 2)
        self.assertEqual(metrics["win_rate_pct"], 50.0)
        self.assertEqual(metrics["expectancy_r"], 0.0)


if __name__ == "__main__":
    unittest.main()
