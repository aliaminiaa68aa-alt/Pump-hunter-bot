import unittest

from Bot import build_oi_surge_wide_risk_watch_message


class OISurgeWideRiskWatchTests(unittest.TestCase):
    def test_message_is_explicitly_watch_only_and_discloses_risk(self):
        message = build_oi_surge_wide_risk_watch_message(
            contract="ARIA_USDT",
            tier="24H",
            oi4="+0.87%",
            oi24="+117.11%",
            funding=-0.0001,
            close=1.25,
            resistance=1.30,
            support=1.15,
            risk_pct=11.97,
            price_change=8.5,
            turnover=12_000_000,
        )
        self.assertIn("ARIA/USDT", message)
        self.assertIn("بدون ورود", message)
        self.assertIn("11.97%", message)
        self.assertIn("117.11%", message)
        self.assertIn("قیمت را تعقیب نکن", message)
        self.assertNotIn("🎯", message)
        self.assertNotIn("قیمت ورود", message)


if __name__ == "__main__":
    unittest.main()
