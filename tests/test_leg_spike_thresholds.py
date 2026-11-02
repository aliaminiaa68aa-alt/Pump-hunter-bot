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


if __name__ == "__main__":
    unittest.main()
