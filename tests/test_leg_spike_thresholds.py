import unittest

import Bot


class LegSpikeThresholdTests(unittest.TestCase):
    def test_volume_to_market_cap_must_be_at_least_two_x(self):
        self.assertEqual(Bot.FIB_MIN_VOLUME_TO_MARKET_CAP, 2.0)

    def test_open_interest_to_market_cap_must_not_exceed_fifty_percent(self):
        self.assertEqual(Bot.FIB_MAX_OI_TO_MARKET_CAP, 0.50)


if __name__ == "__main__":
    unittest.main()
