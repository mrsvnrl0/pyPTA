"""Exact market routing, public catalog validation and completed fallback bars."""
import unittest
from adaptive_crypto.core import H4, M5, DataError
from adaptive_crypto.market_feed import DashboardMarketData
from adaptive_crypto.deribit_market_data import DeribitMarketData
from adaptive_crypto.binance_spot_data import BinanceSpotMarketData, parse_rows

NOW = 1800000000000 // H4 * H4 + 1000


class RoutedFeed:
    def __init__(self, venue, pairs):
        self.venue, self.pairs = venue, set(pairs)
        self.calls = []
        self.failure = None
        self.catalog_failure = None
    def supports_pair(self, pair):
        if self.catalog_failure: raise DataError(self.catalog_failure)
        return pair in self.pairs
    def validate_pair(self, pair):
        self.calls.append(("validate", pair))
        if not self.supports_pair(pair): raise DataError("unsupported pair")
        return pair
    def available_pairs(self): return sorted(self.pairs)
    def candles(self, pair, interval, now):
        self.calls.append(("candles", pair))
        if self.failure: raise DataError(self.failure)
        return [(self.venue, pair, interval)]
    def source_metadata(self, pair, interval, now=None):
        return {"venue": self.venue, "pair": pair, "quote_asset": pair.split("/")[1]}
    def quotes(self, pairs):
        self.calls.append(("quotes", tuple(pairs)))
        return {pair: {"symbol": pair, "source": self.venue, "asof_ms": NOW} for pair in pairs}
    def close(self): self.calls.append(("close",))


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.deribit = RoutedFeed("Deribit", ["BTC/USDC"])
        self.binance = RoutedFeed("Binance", ["BTC/USDC", "BTC/USDT", "XRP/USDC"])
        self.feed = DashboardMarketData(deribit=self.deribit, binance=self.binance, wall_clock=lambda: NOW / 1000)

    def test_deribit_primary_and_exact_missing_market_fallback(self):
        self.assertEqual(self.feed.candles("BTC/USDC", H4, NOW)[0][0], "Deribit")
        self.assertEqual(self.feed.candles("XRP/USDC", H4, NOW)[0][0], "Binance")
        self.assertEqual(self.feed.source_metadata("XRP/USDC", H4)["quote_asset"], "USDC")
        self.assertEqual(self.feed.quotes(["BTC/USDC", "XRP/USDC"])["XRP/USDC"]["source"], "Binance")

    def test_supported_deribit_outage_or_gap_never_changes_feed(self):
        self.deribit.failure = "Deribit candle history has a gap"
        with self.assertRaisesRegex(DataError, "gap"):
            self.feed.candles("BTC/USDC", H4, NOW)
        self.assertEqual(self.binance.calls, [])

    def test_catalog_failure_cannot_be_treated_as_unsupported(self):
        self.deribit.catalog_failure = "Deribit catalog unavailable"
        with self.assertRaisesRegex(DataError, "catalog"):
            self.feed.validate_pair("XRP/USDC")
        self.assertEqual(self.binance.calls, [])

    def test_new_preference_validation_does_not_change_active_routing(self):
        self.assertEqual(self.feed.validate_pair("BTC/USDC", "binance"), "BTC/USDC")
        self.assertEqual(self.feed.preference, "deribit_first")
        self.feed.set_preference("binance")
        self.assertEqual(self.feed.candles("BTC/USDC", H4, NOW)[0][0], "Binance")
        with self.assertRaises(DataError): self.feed.set_preference("kraken")
        self.assertEqual(self.feed.preference, "binance")

    def test_fallback_preserves_currency_and_clock_is_independent(self):
        self.deribit.catalog_failure = "outage"
        self.assertEqual(self.feed.now(), NOW)
        self.deribit.catalog_failure = None
        self.assertEqual(self.feed.candles("BTC/USDT", H4, NOW)[0][1], "BTC/USDT")
        with self.assertRaises(DataError): self.feed.validate_pair("XRP/USD")


class DeribitCatalog:
    def __init__(self): self.calls = []; self.ticker = False
    def request(self, method, params):
        self.calls.append((method, params))
        if method == "public/get_time": return NOW
        if method == "public/get_instruments":
            kind = params["kind"]
            rows = [{"kind": kind, "base_currency": "DOGE", "quote_currency": "USDC",
                     "instrument_name": "DOGE_USDC" + ("-PERPETUAL" if kind == "future" else ""),
                     "price_index": "doge_usdc", "settlement_period": "perpetual",
                     "underlying_type": "crypto", "is_active": True, "state": "open"}]
            if kind == "spot":
                rows.append({**rows[0], "base_currency": "ETH", "quote_currency": "USDT",
                             "instrument_name": "ETH_USDT", "price_index": "eth_usdt"})
            rows.append({**rows[0], "base_currency": "AAPL", "instrument_name": "AAPL_USDC-PERPETUAL",
                         "underlying_type": "equity"})
            rows.append({**rows[0], "base_currency": "XRP", "instrument_name": "XRP_USDC-PERPETUAL", "state": "halted"})
            return rows
        if method == "public/get_index_price_names": return ["doge_usdc"]
        if method == "public/get_index_price": return {"index_price": .12}
        if method == "public/ticker":
            return {"instrument_name": params["instrument_name"], "best_bid_price": 100.,
                    "best_ask_price": 101., "last_price": 100.5, "timestamp": NOW}
        raise AssertionError(method)


class DeribitCatalogTests(unittest.TestCase):
    def test_dynamic_catalog_prefers_perpetual_and_excludes_equity_halted(self):
        feed = DeribitMarketData(DeribitCatalog())
        self.assertTrue(feed.supports_pair("DOGE/USDC"))
        self.assertFalse(feed.supports_pair("AAPL/USDC"))
        self.assertTrue(feed.supports_pair("XRP/USDC"))
        self.assertNotIn("XRP/USDC", feed.available_pairs())
        with self.assertRaisesRegex(DataError, "halted"):
            feed.validate_pair("XRP/USDC")
        self.assertEqual(feed.source_metadata("DOGE/USDC", H4)["market"], "perpetual")
        self.assertEqual(feed.source_metadata("ETH/USDT", H4)["market"], "spot")

    def test_quote_uses_exact_index_or_observed_ticker(self):
        feed = DeribitMarketData(DeribitCatalog())
        self.assertTrue(feed.supports_index("DOGE/USDC"))
        self.assertFalse(feed.supports_index("ETH/USDT"))
        quotes = feed.quotes(["DOGE/USDC", "ETH/USDT"])
        self.assertFalse(quotes["DOGE/USDC"]["executable"])
        self.assertEqual(quotes["ETH/USDT"]["symbol"], "ETH/USDT")
        self.assertEqual((quotes["ETH/USDT"]["bid"], quotes["ETH/USDT"]["ask"]), (100., 101.))
        self.assertTrue(quotes["ETH/USDT"]["spread_observed"])

    def test_halted_deribit_market_does_not_fall_back_to_binance(self):
        deribit = DeribitMarketData(DeribitCatalog())
        binance = RoutedFeed("Binance", ["XRP/USDC"])
        feed = DashboardMarketData(deribit=deribit, binance=binance)
        with self.assertRaisesRegex(DataError, "halted"):
            feed.validate_pair("XRP/USDC")
        self.assertEqual(binance.calls, [])
        self.assertNotIn("XRP/USDC", feed.available_pairs())


class BinanceFixture(BinanceSpotMarketData):
    def __init__(self):
        super().__init__(); self.calls = []; self.drop = False
    def _get(self, endpoint, **params):
        self.calls.append((endpoint, params))
        if endpoint == "time": return {"serverTime": NOW}
        if endpoint == "exchangeInfo":
            return {"symbols": [{"baseAsset": "XRP", "quoteAsset": "USDC", "symbol": "XRPUSDC",
                                "status": "TRADING", "isSpotTradingAllowed": True},
                                {"baseAsset": "BAD", "quoteAsset": "USDC", "symbol": "BADUSDC",
                                "status": "BREAK", "isSpotTradingAllowed": True}]}
        if endpoint == "ticker/bookTicker": return {"symbol": "XRPUSDC", "bidPrice": "2", "askPrice": "2.01"}
        interval = {"5m": M5, "4h": H4}[params["interval"]]
        ticks = list(range(params["startTime"], params["endTime"] + 1, interval))
        if self.drop: ticks = ticks[:3] + ticks[4:]
        return [[t, "2", "3", "1", "2.5", "7", t + interval - 1, "17.5", 5, "3", "7.5", "0"] for t in ticks]


class BinanceSpotTests(unittest.TestCase):
    def test_unicode_listing_does_not_break_the_public_catalog(self):
        feed = BinanceFixture(); self.addCleanup(feed.close)
        original = feed._get
        def response(endpoint, **params):
            body = original(endpoint, **params)
            if endpoint == "exchangeInfo":
                body["symbols"].append({"baseAsset": "币安人生", "quoteAsset": "USDC", "symbol": "币安人生USDC",
                                        "status": "TRADING", "isSpotTradingAllowed": True})
            return body
        feed._get = response
        self.assertTrue(feed.supports_pair("XRP/USDC"))
        self.assertIn("币安人生/USDC", feed.available_pairs())

    def test_catalog_exact_currency_and_real_observed_quote(self):
        feed = BinanceFixture(); self.addCleanup(feed.close)
        self.assertTrue(feed.supports_pair("XRP/USDC"))
        self.assertFalse(feed.supports_pair("XRP/USDT"))
        self.assertFalse(feed.supports_pair("BAD/USDC"))
        quote = feed.quotes(["XRP/USDC"])["XRP/USDC"]
        self.assertEqual(quote["symbol"], "XRP/USDC")
        self.assertEqual((quote["bid"], quote["ask"]), (2., 2.01))
        self.assertFalse(quote["synthetic"])

    def test_completed_ohlcv_volume_and_cache_are_native(self):
        feed = BinanceFixture(); self.addCleanup(feed.close)
        bars = feed.candles("XRP/USDC", H4, NOW)
        self.assertEqual(len(bars), 420)
        self.assertEqual(bars[-1].v, 7.)
        self.assertLess(bars[-1].end, NOW)
        calls = len(feed.calls)
        self.assertEqual(feed.chart_candles("XRP/USDC", H4, NOW), bars)
        self.assertEqual(len(feed.calls), calls)
        self.assertEqual(feed.source_metadata("XRP/USDC", H4)["instrument"], "XRPUSDC")

    def test_pagination_is_exact_and_gaps_rejected(self):
        feed = BinanceFixture(); self.addCleanup(feed.close)
        end = NOW // M5 * M5
        bars = feed.history("XRP/USDC", M5, end - 1500 * M5, end)
        self.assertEqual(len(bars), 1500)
        calls = [p for e, p in feed.calls if e == "klines"]
        self.assertEqual(calls[0]["endTime"] + 1, calls[1]["startTime"])
        feed.drop = True
        with self.assertRaisesRegex(DataError, "gap"):
            feed.history("XRP/USDC", M5, end - 10 * M5, end)

    def test_forming_candles_excluded_and_malformed_bounds_rejected(self):
        end = NOW // M5 * M5
        row = [end, "2", "3", "1", "2.5", "7", end + M5 - 1, "17.5", 5, "3", "7.5", "0"]
        self.assertEqual(parse_rows([row], M5, end - M5, end), [])
        row[6] += 1
        with self.assertRaisesRegex(DataError, "Misaligned"):
            parse_rows([row], M5, end, end + M5)


if __name__ == "__main__": unittest.main()
