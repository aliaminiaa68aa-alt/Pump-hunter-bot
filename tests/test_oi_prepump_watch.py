import unittest

from Bot import detect_oi_prepump_watch


def candle(t, o, h, l, c, turnover):
    return {"t": str(t), "o": str(o), "h": str(h), "l": str(l), "c": str(c), "sum": str(turnover)}


class OIPrePumpWatchTests(unittest.TestCase):
    def candles(self, near_resistance=True, strong_volume=True, compressed=True):
        candles_5m = []
        for i in range(24):
            base = 100.0
            candles_5m.append(candle(
                i * 300, base, base + (0.25 if compressed else 2.0),
                base - (0.25 if compressed else 2.0), base + 0.02, 100.0
            ))
        # Keep recent resistance just above price to test pre-breakout detection.
        resistance = 100.20 if near_resistance else 101.50
        for row in candles_5m[-6:-1]:
            row["h"] = str(resistance)
        candles_5m[-1] = candle(
            23 * 300, 100.0, 100.15, 99.95, 100.10,
            220.0 if strong_volume else 100.0
        )
        candles_1m = [
            candle(i * 60, 100.0, 100.10, 99.95, 100.02,
                   200.0 if i == 29 and strong_volume else 100.0)
            for i in range(30)
        ]
        return candles_1m, candles_5m

    def test_detects_quiet_high_turnover_setup_near_resistance(self):
        one, five = self.candles()
        result = detect_oi_prepump_watch(one, five)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["turnover_ratio_5m"], 1.5)

    def test_rejects_candidate_far_from_resistance(self):
        one, five = self.candles(near_resistance=False)
        self.assertIsNone(detect_oi_prepump_watch(one, five))

    def test_rejects_candidate_without_volume_surge(self):
        one, five = self.candles(strong_volume=False)
        self.assertIsNone(detect_oi_prepump_watch(one, five))

    def test_rejects_wide_compression(self):
        one, five = self.candles(compressed=False)
        self.assertIsNone(detect_oi_prepump_watch(one, five))


if __name__ == "__main__":
    unittest.main()
