import unittest

import Bot


class LegSpikeThresholdTests(unittest.TestCase):
    def test_volume_to_market_cap_conditional_floor_is_1_8x(self):
        self.assertEqual(Bot.FIB_MIN_VOLUME_TO_MARKET_CAP, 1.8)

    def test_full_volume_tier_starts_at_2x(self):
        self.assertEqual(Bot.FIB_FULL_VOLUME_TO_MARKET_CAP, 2.0)

    def test_open_interest_to_market_cap_is_not_a_hard_filter(self):
        self.assertFalse(hasattr(Bot, "FIB_MAX_OI_TO_MARKET_CAP"))


if __name__ == "__main__":
    unittest.main()
