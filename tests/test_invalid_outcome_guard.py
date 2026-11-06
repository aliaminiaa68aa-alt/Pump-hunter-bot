import unittest

from oi_engine.signal_engine import Signal
from oi_engine.telegram_runner import blocked_by_active_symbol_trade


class InvalidOutcomeGuardTests(unittest.TestCase):
    def test_invalid_unfilled_record_does_not_block_new_signal(self):
        signal = Signal(
            "TESTUSDT", "LONG", 100.0, 98.0, 102.0, 104.0,
            0.8, 0.3, 0.56, 1.4, 0.0001, 82, 1800000
        )
        journal = {
            "trades": {
                "old-invalid": {
                    "status": "INVALID_NO_ENTRY",
                    "signal": {"symbol": "TESTUSDT", "side": "LONG"},
                    "events": [{"event": "TP2", "time_ms": 900000, "fill": 104.0}],
                    "entry_fill_price": None,
                }
            }
        }
        self.assertFalse(blocked_by_active_symbol_trade(signal, journal))


if __name__ == "__main__":
    unittest.main()
