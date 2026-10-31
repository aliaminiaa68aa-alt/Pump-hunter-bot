import unittest

from Bot import pre_pump_oi_gate


class OIPrePumpGateTests(unittest.TestCase):
    def test_accepts_rising_oi_with_non_crowded_funding(self):
        self.assertTrue(pre_pump_oi_gate({
            "valid": True, "oi_change": 0.55, "oi_change4h": 0.8,
            "funding_rate": 0.0001,
        }))

    def test_rejects_flat_or_falling_short_term_oi(self):
        self.assertFalse(pre_pump_oi_gate({
            "valid": True, "oi_change": 0.29, "oi_change4h": 0.8,
            "funding_rate": 0.0,
        }))
        self.assertFalse(pre_pump_oi_gate({
            "valid": True, "oi_change": -0.4, "oi_change4h": 0.8,
            "funding_rate": 0.0,
        }))

    def test_rejects_crowded_positive_funding(self):
        self.assertFalse(pre_pump_oi_gate({
            "valid": True, "oi_change": 0.8, "oi_change4h": 0.5,
            "funding_rate": 0.0005,
        }))

    def test_rejects_negative_four_hour_oi_when_available(self):
        self.assertFalse(pre_pump_oi_gate({
            "valid": True, "oi_change": 0.8, "oi_change4h": -0.1,
            "funding_rate": 0.0,
        }))

    def test_rejects_missing_or_invalid_derivatives_data(self):
        self.assertFalse(pre_pump_oi_gate({"valid": False, "oi_change": 2, "funding_rate": 0}))
        self.assertFalse(pre_pump_oi_gate({"valid": True, "oi_change": None, "funding_rate": 0}))


if __name__ == "__main__":
    unittest.main()
