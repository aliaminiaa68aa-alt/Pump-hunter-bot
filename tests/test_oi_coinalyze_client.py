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

    def test_asset_catalogue_classifies_crypto_and_excludes_equity_assets(self):
        client = CoinalyzeClient(api_key="test", sleeper=lambda _: None)
        client._get = lambda endpoint, params=None: [
            {"symbol": "BTC", "asset_category": "CRYPTO"},
            {"symbol": "ETH", "asset_category": "CRYPTO"},
            {"symbol": "QCOM", "asset_category": "EQUITY"},
            {"symbol": "SNXX", "asset_category": "ETF"},
        ] if endpoint == "assets" else []
        self.assertEqual(client.get_crypto_asset_symbols(), {"BTC", "ETH"})
        self.assertTrue(client.is_crypto_asset("btc"))
        self.assertFalse(client.is_crypto_asset("QCOM"))
        self.assertFalse(client.is_crypto_asset("SNXX"))

    def test_asset_catalogue_fails_closed_when_empty(self):
        client = CoinalyzeClient(api_key="test", sleeper=lambda _: None)
        client._get = lambda endpoint, params=None: []
        with self.assertRaisesRegex(CoinalyzeError, "assets response"):
            client.get_crypto_asset_symbols()

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


    def test_fetch_ohlcv_candles_returns_normalized_15m_rows(self):
        client = CoinalyzeClient(api_key="test", sleeper=lambda _: None)
        client.resolve_market = lambda symbol: "BTCUSDT_PERP.A"
        seen = {}
        def history(endpoint, symbol, start, end):
            seen.update({"endpoint": endpoint, "symbol": symbol, "start": start, "end": end})
            return [
                {"t": 180000, "o": "100", "h": "105", "l": "98", "c": "103"},
                {"t": 180900, "o": "103", "h": "106", "l": "101", "c": "104"},
            ]
        client._history = history
        rows = client.fetch_ohlcv_candles("BTCUSDT", 180000000, 181800000)
        self.assertEqual(seen, {
            "endpoint": "ohlcv-history", "symbol": "BTCUSDT_PERP.A",
            "start": 180000, "end": 181800,
        })
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][:5], [180000000, "100.0", "105.0", "98.0", "103.0"])

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

        requested_endpoints = []
        def fake_get(endpoint, params=None):
            requested_endpoints.append(endpoint)
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
        self.assertNotIn("funding-rate-history", requested_endpoints)
        self.assertEqual(len(snapshot["candles"]), 24)
        self.assertEqual(len(snapshot["oi_values"]), 3)
        last = snapshot["candles"][-1]
        expected_typical_price = (last["open"] + last["high"] + last["low"] + last["close"]) / 4
        self.assertAlmostEqual(last["quote_volume"], last["volume"] * expected_typical_price)
        self.assertAlmostEqual(last["taker_buy_quote"], 600 * expected_typical_price)
        self.assertAlmostEqual(last["taker_buy_quote"] / last["quote_volume"], 0.6)

    def test_core_only_metrics_skip_unused_supplementary_api_endpoints(self):
        client = CoinalyzeClient(
            api_key="test", sleeper=lambda _: None, include_optional_metrics=False
        )
        client.resolve_market = lambda symbol: "BTCUSDT_PERP.A"
        start = 180000
        candles = [
            {"t": start + i * 900, "o": 100+i, "h": 102+i, "l": 99+i,
             "c": 101+i, "v": 1000, "bv": 600}
            for i in range(24)
        ]
        oi = [{"t": start + i * 900, "c": 100000 + i * 100}
              for i in range(24)]
        requested = []
        def fake_get(endpoint, params=None):
            requested.append(endpoint)
            if endpoint == "funding-rate":
                return [{"symbol": "BTCUSDT_PERP.A", "value": 0.0001}]
            if endpoint == "open-interest-history":
                return [{"symbol": "BTCUSDT_PERP.A", "history": oi}]
            if endpoint == "ohlcv-history":
                return [{"symbol": "BTCUSDT_PERP.A", "history": candles}]
            raise AssertionError(f"Unexpected supplementary endpoint: {endpoint}")

        client._get = fake_get
        result = client.fetch_metrics("BTCUSDT", now=start + 24 * 900)
        self.assertEqual(requested, ["funding-rate", "open-interest-history", "ohlcv-history"])
        self.assertIsNotNone(result["market_snapshot"])
        self.assertIsNone(result["current_open_interest_usd"])
        self.assertIsNone(result["predicted_funding_rate"])
        self.assertIsNone(result["long_short_ratio"])
        self.assertIsNone(result["long_liquidations"])
        self.assertIsNone(result["short_liquidations"])
        self.assertTrue(result["data_quality"]["complete"])

    def test_fetch_metrics_calculates_closed_candle_oi_and_buy_ratio(self):
        client = CoinalyzeClient(api_key="test", sleeper=lambda _: None)
        client.resolve_market = lambda symbol: "BTCUSDT_PERP.A"
        start = 180000
        candles = [
            {"t": start + i * 900, "o": 100, "h": 102, "l": 99,
             "c": 101, "v": 100, "bv": 90 if i == 23 else 60}
            for i in range(24)
        ]
        # Latest price candle has no matching OI sample yet.
        oi = [{"t": start + i * 900, "c": (100 if i == 21 else 101 if i == 22 else 90 + i)}
              for i in range(23)]

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
        self.assertAlmostEqual(result["oi_change_15m_window_pct"], 1.0)
        self.assertAlmostEqual(result["taker_buy_ratio"], 0.6)
        snapshot = result["market_snapshot"]
        self.assertEqual(snapshot["candles"][-1]["open_time"], (start + 22 * 900) * 1000)
        self.assertEqual(snapshot["oi_timestamp"], (start + 23 * 900) * 1000 - 1)
        self.assertEqual(result["long_short_ratio"], 1.2)
        self.assertEqual(result["long_liquidations"], 1200.0)
        self.assertTrue(result["data_quality"]["complete"])


    def test_fetch_metrics_rejects_stale_aligned_market_snapshot(self):
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
                "long-short-ratio-history": [{"t": start, "r": 1.2}],
                "liquidation-history": [{"t": start, "l": 1200, "s": 900}],
                "ohlcv-history": candles,
            }
            return [{"symbol": "BTCUSDT_PERP.A", "history": history[endpoint]}]

        client._get = fake_get
        # The latest common candle closed at start + 24*900; the snapshot
        # is 901 seconds old, just beyond the one-interval tolerance.
        result = client.fetch_metrics("BTCUSDT", now=start + 24 * 900 + 901)
        self.assertIsNone(result["market_snapshot"])
        self.assertIsNone(result["oi_change_15m_window_pct"])
        self.assertIsNone(result["taker_buy_ratio"])


if __name__ == "__main__":
    unittest.main()
