"""Native quote contract exercised through actual SMC consumers and the cache."""
import copy
from dataclasses import replace
import unittest
from unittest.mock import patch
from adaptive_crypto.core import DataError, Rules
from adaptive_crypto.gex import DeribitGEX, map_smc
from adaptive_crypto.gex_currency import profile_for_market, require_market_currency, convert_usd_profile
from adaptive_crypto.currency_conversion import CurrencyConversions
from tests.test_currency_conversion import Provider as FXProvider
from tests.test_gex_formula import profile as canonical_profile
from adaptive_crypto.pine_guidance import pine_confluence
from tests.test_gex_formula import NOW


def raw_profile():
    return {"status": "ready", "stale": False, "base": "BTC", "symbol": "BTC/USDC",
        "quote_currency": "USDC", "price_currency": "USDC", "exposure_currency": "USDC",
        "spot": 100., "net_gex": 50., "regime": "positive", "coverage_complete": True,
        "call_wall": {"strike": 110.}, "put_wall": {"strike": 90.},
        "strikes": [{"strike": 110., "net_gex": 50.}], "curve": [],
        "asof_ms": NOW-1000, "calculated_ms": NOW, "expires_ms": NOW+3600000,
        "fx_valid_until_ms": NOW+20000, "units": "USDC delta per 1% index move"}


def evidence():
    interval = 300000
    end = NOW//interval*interval-1
    structure = {"source": "pine_smc", "status": "ready", "interval_ms": interval,
        "last_closed_ms": end, "last_bar_ms": end-interval+1, "asof_ms": end,
        "liquidity": [{"price": 110., "side": "long", "bar_ms": end-interval*3,
                       "known_ms": end-interval, "taken": False}],
        "zones": [{"low": 89., "high": 91., "side": "long", "known_ms": end,
                   "status": "active"}]}
    quote = {"bid": 100., "ask": 100., "last": 100., "asof_ms": NOW}
    return structure, quote


class GEXCurrencyTests(unittest.TestCase):
    def conversion(self, quote, usd_rate):
        provider = FXProvider({quote.lower()+"_usd": usd_rate})
        provider.stamp = NOW
        return CurrencyConversions(provider).quote(quote, "USD", NOW)

    def test_nonpar_usdt_coordinate_scales_every_price_and_exposure(self):
        original = canonical_profile()
        fx = self.conversion("USDT", .8)
        converted = convert_usd_profile(original, "USDT", fx, NOW)
        factor = 1.25
        self.assertEqual(converted["symbol"], "BTC/USDT")
        self.assertEqual(converted["price_currency"], "USDT")
        self.assertEqual(converted["exposure_currency"], "USDT")
        self.assertEqual(converted["units"], "USDT delta per 1% index move")
        for key in ("spot", "net_gex", "gross_gex", "gamma_flip"):
            self.assertAlmostEqual(converted[key], original[key]*factor)
        for key in ("flip_range", "flip_candidates"):
            self.assertEqual(converted[key], [value*factor for value in original[key]])
        for key in ("call_wall", "put_wall"):
            for field in ("strike", "call_gex", "put_gex", "net_gex"):
                self.assertAlmostEqual(converted[key][field], original[key][field]*factor)
        for before, after in zip(original["strikes"], converted["strikes"]):
            for field in ("strike", "call_gex", "put_gex", "net_gex"):
                self.assertAlmostEqual(after[field], before[field]*factor)
        for before, after in zip(original["curve"], converted["curve"]):
            self.assertAlmostEqual(after["price"], before["price"]*factor)
            self.assertAlmostEqual(after["gex"], before["gex"]*factor)
        self.assertEqual(converted["net_to_gross"], original["net_to_gross"])
        self.assertEqual(converted["option_count"], original["option_count"])
        self.assertEqual(original["price_currency"], "USD")
        self.assertEqual(require_market_currency(converted, "BTC/USDT", NOW), fx["expires_ms"])

    def test_crypto_quote_uses_usd_per_coin_rate_and_requires_trace(self):
        fx = self.conversion("ETH", 2000.)
        converted = convert_usd_profile(canonical_profile(), "ETH", fx, NOW)
        self.assertAlmostEqual(converted["spot"], .05)
        self.assertAlmostEqual(converted["call_wall"]["strike"], .055)
        self.assertAlmostEqual(converted["net_gex"], canonical_profile()["net_gex"]/2000.)
        for change in ({"coordinate_fx": None}, {"coordinate_factor": 1.},
                       {"canonical_quote_currency": "USDT"}, {"coordinate_kind": None},
                       {"symbol": "BTC/USDT"}):
            with self.subTest(change=change), self.assertRaises(DataError):
                require_market_currency({**converted, **change}, "BTC/ETH", NOW)
        with self.assertRaises(DataError):
            require_market_currency(converted, "BTC/ETH", fx["expires_ms"])

    def test_advisory_coordinate_uses_midpoint_while_retaining_directional_fx(self):
        provider = FXProvider({}, {"USDT/USD": (.7, .9)}, "binance"); provider.stamp = NOW
        fx = CurrencyConversions(provider).quote("USDT", "USD", NOW)
        converted = convert_usd_profile(canonical_profile(), "USDT", fx, NOW)
        self.assertAlmostEqual(converted["coordinate_factor"], 1.25)
        self.assertAlmostEqual(converted["spot"], 125.)
        self.assertEqual(converted["coordinate_fx"]["buy_rate"], .9)
        self.assertEqual(converted["coordinate_fx"]["sell_rate"], .7)
        self.assertEqual(converted["coordinate_kind"], "synthetic_currency_reference")

    def test_cache_refreshes_before_foreign_coordinate_expires(self):
        converted = convert_usd_profile(canonical_profile(), "USDT", self.conversion("USDT", .8), NOW)
        converted["coverage_complete"] = True
        with patch("adaptive_crypto.gex.time.time", return_value=NOW/1000), patch("adaptive_crypto.gex.time.monotonic", return_value=10.):
            cache = DeribitGEX({"BTC": {"symbol": "BTC/USDT"}}, lambda *_: converted, ttl=120)
            self.assertEqual(cache.get("BTC")["status"], "ready")
            self.assertEqual(cache.cache["BTC"]["retry_at"], 25.)

    def test_native_profile_preserves_all_units_and_is_independent_copy(self):
        profile = raw_profile()
        result = profile_for_market(profile, "BTC/USDC", NOW)
        self.assertEqual(result, profile)
        result["strikes"][0]["strike"] = 1
        self.assertEqual(profile["strikes"][0]["strike"], 110.)
        self.assertEqual(require_market_currency(profile, "BTC/USDC", NOW), NOW+20000)

    def test_mixed_asset_quote_exposure_or_missing_metadata_fails_closed(self):
        for update in ({"base": "ETH"}, {"price_currency": "USD"},
                       {"exposure_currency": "USD"}, {"price_currency": None},
                       {"exposure_currency": None}):
            with self.subTest(update=update), self.assertRaises(DataError):
                require_market_currency({**raw_profile(), **update}, "BTC/USDC", NOW)
        for pair in ("BTC/USD", "BTC/USDT", "ETH/USDC"):
            with self.subTest(pair=pair), self.assertRaises(DataError):
                profile_for_market(raw_profile(), pair, NOW)

    def test_currency_conversion_expiry_is_strict(self):
        for deadline in (NOW, NOW-1, str(NOW+1), True):
            with self.subTest(deadline=deadline), self.assertRaises(DataError):
                require_market_currency({**raw_profile(), "fx_valid_until_ms": deadline}, "BTC/USDC", NOW)

    def test_consumer_matches_only_current_complete_native_gex_and_pine_liquidity(self):
        structure, quote = evidence()
        rules = replace(Rules(), smc_gex_targets=True)
        result = pine_confluence(structure, quote, rules, raw_profile(), NOW, "BTC/USDC")
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["price_currency"], "USDC")
        self.assertEqual(result["valid_until_ms"], NOW+20000)
        self.assertEqual([m["poi"]["price"] for m in result["matches"] if m["eligible"]], [110.])
        row = {"symbol": "BTC/USDC", "quote": quote, "asof_ms": NOW,
               "pine_structure": structure, "gex_context": result, "errors": {}}
        view = map_smc(raw_profile(), row, NOW)
        self.assertTrue(view["target_confluence"]["ready"])
        self.assertTrue(view["zones"][0]["confluence"])
        self.assertEqual(view["options_valid_until_ms"], NOW+20000)

    def test_consumer_rejects_expired_fx_partial_and_mixed_quotes_but_retains_pine_levels(self):
        structure, quote = evidence()
        rules = replace(Rules(), smc_gex_targets=True)
        for update in ({"fx_valid_until_ms": NOW}, {"status": "partial"},
                       {"coverage_complete": False}, {"coverage_complete": None},
                       {"price_currency": "USD"}, {"exposure_currency": "USD"}):
            with self.subTest(update=update):
                profile = {**raw_profile(), **update}
                result = pine_confluence(structure, quote, rules, profile, NOW, "BTC/USDC")
                self.assertFalse(result["ready"])
                self.assertEqual(result["matches"], [])
                self.assertEqual([p["price"] for p in result["liquidity_choices"]], [110.])
                view = map_smc(profile, {"symbol": "BTC/USDC", "quote": quote,
                    "asof_ms": NOW, "pine_structure": structure, "errors": {}}, NOW)
                self.assertFalse(any(z["confluence"] for z in view["zones"]))

    def test_cache_requests_configured_pair_and_rejects_incomplete_claim(self):
        calls = []
        def provider(base, quote):
            calls.append((base, quote))
            return raw_profile()
        with patch("adaptive_crypto.gex.time.time", return_value=NOW/1000):
            cache = DeribitGEX({"BTC": {"symbol": "BTC/USDC"}}, provider)
            self.assertEqual(cache.get("BTC")["status"], "ready")
            self.assertEqual(calls, [("BTC", "USDC")])
            bad = DeribitGEX({"BTC": {"symbol": "BTC/USDC"}},
                lambda *_: {**raw_profile(), "coverage_complete": False})
            self.assertNotEqual(bad.get("BTC")["status"], "ready")

    def test_refresh_failure_retains_prior_levels_as_stale_not_usable(self):
        replies = [raw_profile(), DataError("Deribit unavailable")]
        def provider(*_):
            result = replies.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        with patch("adaptive_crypto.gex.time.time", return_value=NOW/1000):
            cache = DeribitGEX({"BTC": {"symbol": "BTC/USDC"}}, provider, ttl=0)
            self.assertEqual(cache.get("BTC")["status"], "ready")
            failed = cache.get("BTC")
            self.assertEqual(failed["status"], "stale")
            self.assertEqual(failed["call_wall"]["strike"], 110.)
            self.assertTrue(failed["stale"])
