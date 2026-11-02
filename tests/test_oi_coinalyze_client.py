import unittest
from unittest.mock import Mock

from oi_engine.coinalyze_client import CoinalyzeClient, CoinalyzeError


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class CoinalyzeClientTests(unittest.TestCase):
    def test_requires_secret(self):
        with self.assertRaisesRegex(CoinalyzeError, "COINALYZE_API_KEY"):
            CoinalyzeClient(api_key="")

    def test_resolves_binance_perpetual(self):
        client = CoinalyzeClient(api_key="test", sleeper=lambda _: None)
        client.get_binance_exchange_codes = lambda: {"BINANCE"}
        client.get_future_markets = lambda: [
            {"symbol": "BTCUSDT_PERP.A", "exchange": "BINANCE",
             "base_asset": "BTC", "quote_asset": "USDT",
             "is_perpetual": True, "margined": "STABLE"}
        ]
        self.assertEqual(client.resolve_market("BTCUSDT"), "BTCUSDT_PERP.A")


    def test_resolves_exchange_code_using_exchange_directory(self):
        client = CoinalyzeClient(api_key="test", sleeper=lambda _: None)
        client.get_binance_exchange_codes = lambda: {"A"}
        client.get_future_markets = lambda: [
            {"symbol": "BTCUSDT_PERP.A", "exchange": "A",
             "base_asset": "BTC", "quote_asset": "USDT",
             "is_perpetual": True, "margined": "STABLE"}
        ]
        self.assertEqual(client.resolve_market("BTCUSDT"), "BTCUSDT_PERP.A")

    def test_rejects_unmapped_symbol(self):
        client = CoinalyzeClient(api_key="test", sleeper=lambda _: None)
        client.get_binance_exchange_codes = lambda: {"BINANCE"}
        client.get_future_markets = lambda: []
        with self.assertRaisesRegex(CoinalyzeError, "No Binance USDT perpetual mapping"):
            client.resolve_market("UNKNOWNUSDT")

    def test_api_key_is_sent_in_header(self):
        session = Mock()
        session.headers = {}
        session.get.return_value = FakeResponse([{"name": "BINANCE", "code": "BINANCE"}])
        client = CoinalyzeClient(api_key="secret-value", session=session, sleeper=lambda _: None)
        client._get("exchanges")
        self.assertEqual(session.headers["api_key"], "secret-value")
        self.assertNotIn("secret-value", repr(session.get.call_args))


    def test_fetch_metrics_builds_closed_15m_market_snapshot(self):
        client = CoinalyzeClient(api_key="test", sleeper=lambda _: None)
        client.resolve_market = lambda symbol: "BTCUSDT_PERP.A"
        start = 180000
        candles = [
            {"t": start + i * 900, "o": 100+i, "h": 102+i, "l": 99+i,
             "c": 101+i, "v": 1000, "bv": 600}
            for i in range(24)
        ]
        oi = [{"t": start + i * 900, "c": 100000 + i * 100}
              for i in range(24)]

        def fake_get(endpoint, params=None):
            if endpoint == "open-interest":
                return [{"symbol": "BTCUSDT_PERP.A", "value": 1000000}]
            if endpoint == "funding-rate":
                return [{"symbol": "BTCUSDT_PERP.A", "value": 0.0001}]
            if endpoint == "predicted-funding-rate":
                return [{"symbol": "BTCUSDT_PERP.A", "value": 0.0002}]
            history = {
                "open-interest-history": oi,
                "funding-rate-history": [{"t": start, "c": 0.0001}],
                "long-short-ratio-history": [{"t": start, "r": 1.2, "l": 0.55, "s": 0.45}],
                "liquidation-history": [{"t": start, "l": 1200, "s": 900}],
                "ohlcv-history": candles,
            }
            return [{"symbol": "BTCUSDT_PERP.A", "history": history[endpoint]}]

        client._get = fake_get
        result = client.fetch_metrics("BTCUSDT", now=202000)
        snapshot = result["market_snapshot"]
        self.assertIsNotNone(snapshot)
        self.assertEqual(len(snapshot["candles"]), 24)
        self.assertEqual(len(snapshot["oi_values"]), 3)
        last = snapshot["candles"][-1]
        expected_typical_price = (last["open"] + last["high"] + last["low"] + last["close"]) / 4
        self.assertAlmostEqual(last["quote_volume"], last["volume"] * expected_typical_price)
        self.assertAlmostEqual(last["taker_buy_quote"], 600 * expected_typical_price)
        self.assertAlmostEqual(last["taker_buy_quote"] / last["quote_volume"], 0.6)

    def test_fetch_metrics_calculates_closed_candle_oi_and_buy_ratio(self):
        client = CoinalyzeClient(api_key="test", sleeper=lambda _: None)
        client.resolve_market = lambda symbol: "BTCUSDT_PERP.A"
        start = 180000
        candles = [
            {"t": start + i * 900, "o": 100, "h": 102, "l": 99,
             "c": 101, "v": 100, "bv": 60}
            for i in range(24)
        ]
        oi = [{"t": start + i * 900, "c": (100 if i == 21 else 101 if i == 22 else 103 if i == 23 else 90 + i)}
              for i in range(24)]

        def fake_get(endpoint, params=None):
            if endpoint == "open-interest":
                return [{"symbol": "BTCUSDT_PERP.A", "value": 1000000}]
            if endpoint == "funding-rate":
                return [{"symbol": "BTCUSDT_PERP.A", "value": 0.0001}]
            if endpoint == "predicted-funding-rate":
                return [{"symbol": "BTCUSDT_PERP.A", "value": 0.0002}]
            history = {
                "open-interest-history": oi,
                "funding-rate-history": [{"t": start, "c": 0.0001}],
                "long-short-ratio-history": [{"t": start, "r": 1.2, "l": 0.55, "s": 0.45}],
                "liquidation-history": [{"t": start, "l": 1200, "s": 900}],
                "ohlcv-history": candles,
            }
            return [{"symbol": "BTCUSDT_PERP.A", "history": history[endpoint]}]

        client._get = fake_get
        result = client.fetch_metrics("BTCUSDT", now=start + 24 * 900)
        self.assertAlmostEqual(result["oi_change_15m_window_pct"], 3.0)
        self.assertAlmostEqual(result["taker_buy_ratio"], 0.6)
        self.assertEqual(result["long_short_ratio"], 1.2)
        self.assertEqual(result["long_liquidations"], 1200.0)
        self.assertTrue(result["data_quality"]["complete"])


if __name__ == "__main__":
    unittest.main()
