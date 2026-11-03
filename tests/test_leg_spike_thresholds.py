import unittest

import Bot


class LegSpikeThresholdTests(unittest.TestCase):
    def test_volume_to_market_cap_conditional_floor_is_1_8x(self):
        self.assertEqual(Bot.FIB_MIN_VOLUME_TO_MARKET_CAP, 1.8)

    def test_full_volume_tier_starts_at_2x(self):
        self.assertEqual(Bot.FIB_FULL_VOLUME_TO_MARKET_CAP, 2.0)

    def test_open_interest_to_market_cap_is_not_a_hard_filter(self):
        self.assertFalse(hasattr(Bot, "FIB_MAX_OI_TO_MARKET_CAP"))

    def test_oi_surge_uses_24h_expansion(self):
        self.assertEqual(
            Bot.oi_surge_tier({"valid": True, "oi_change24h": 30.0, "oi_change4h": 0.0}),
            "24H",
        )

    def test_oi_surge_uses_4h_expansion_when_24h_is_below_threshold(self):
        self.assertEqual(
            Bot.oi_surge_tier({"valid": True, "oi_change24h": 12.0, "oi_change4h": 8.0}),
            "4H",
        )

    def test_oi_surge_does_not_classify_invalid_or_small_changes(self):
        self.assertIsNone(Bot.oi_surge_tier({"valid": False, "oi_change24h": 80.0}))
        self.assertIsNone(Bot.oi_surge_tier({"valid": True, "oi_change24h": 29.9, "oi_change4h": 7.9}))

    def test_optional_oi_history_formats_safely(self):
        self.assertEqual(Bot.format_oi_change(None), "ناموجود")
        self.assertEqual(Bot.format_oi_change(3.25), "+3.25%")

    def test_leg_spike_accepts_positive_short_term_oi_when_4h_is_negative(self):
        passed, reason = Bot.leg_spike_oi_growth_gate(0.069, -9.16)
        self.assertTrue(passed)
        self.assertIn("ranking", reason)

    def test_4h_oi_is_validated_context_not_a_directional_gate(self):
        self.assertTrue(Bot.leg_spike_final_oi_context_available(-5.87))
        self.assertTrue(Bot.leg_spike_final_oi_context_available(0.0))
        self.assertFalse(Bot.leg_spike_final_oi_context_available(None))
        self.assertFalse(Bot.leg_spike_final_oi_context_available(float("nan")))
        self.assertFalse(Bot.leg_spike_final_oi_context_available(float("inf")))

    def test_oi_growth_gate_rejects_non_finite_values_but_accepts_negative_4h(self):
        self.assertTrue(Bot.leg_spike_oi_growth_gate(0.069, -5.87)[0])
        self.assertFalse(Bot.leg_spike_oi_growth_gate(float("nan"), 1.0)[0])
        self.assertFalse(Bot.leg_spike_oi_growth_gate(float("inf"), 1.0)[0])
        self.assertFalse(Bot.leg_spike_oi_growth_gate(0.1, float("nan"))[0])
        self.assertFalse(Bot.leg_spike_oi_growth_gate(0.1, float("inf"))[0])

    def test_leg_spike_rejects_non_positive_short_term_oi(self):
        self.assertFalse(Bot.leg_spike_oi_growth_gate(0.0, 2.0)[0])
        self.assertFalse(Bot.leg_spike_oi_growth_gate(-0.02, -1.0)[0])

    def test_leg_spike_requires_both_oi_windows_to_be_available(self):
        self.assertFalse(Bot.leg_spike_oi_growth_gate(0.1, None)[0])
        self.assertFalse(Bot.leg_spike_oi_growth_gate(None, 1.0)[0])

    def test_oi_surge_24h_threshold_is_inclusive(self):
        self.assertEqual(
            Bot.oi_surge_tier({"valid": True, "oi_change24h": 30.0, "oi_change4h": None}),
            "24H",
        )

    def test_oi_surge_4h_threshold_is_inclusive(self):
        self.assertEqual(
            Bot.oi_surge_tier({"valid": True, "oi_change24h": None, "oi_change4h": 8.0}),
            "4H",
        )


if __name__ == "__main__":
    unittest.main()
