"""Currency units, directional spreads and expiry for the single USD paper account."""
import copy
import unittest

from adaptive_crypto.core import DataError
from adaptive_crypto.currency_conversion import CurrencyConversions, identity_fx, validate_fx

NOW = 1800000000000


class Deribit:
    def __init__(self, indices):
        self.indices = indices
        self.calls = []
        self.client = self
        self.fail = False
    def available_indices(self): return sorted(self.indices)
    def request(self, method, params):
        self.calls.append((method, params.copy()))
        if self.fail: raise ValueError("secret-token-in-network-exception")
        return {"index_price": self.indices[params["index_name"]]}


class Binance:
    def __init__(self, provider, pairs):
        self.provider, self.pairs = provider, pairs
        self.calls = []
    def available_pairs(self): return sorted(self.pairs)
    def quotes(self, pairs):
        self.calls.extend(pairs)
        return {pair: {"symbol": pair, "bid": self.pairs[pair][0], "ask": self.pairs[pair][1],
                       "asof_ms": self.provider.now()} for pair in pairs}


class Provider:
    def __init__(self, indices=None, pairs=None, preference="deribit_first"):
        self.stamp = NOW
        self.preference = preference
        self.deribit = Deribit(indices or {})
        self.binance = Binance(self, pairs or {})
    def now(self): return self.stamp


class CurrencyConversionTests(unittest.TestCase):
    def test_only_identical_currencies_have_identity(self):
        provider = Provider()
        conversions = CurrencyConversions(provider)
        identity = conversions.quote("USD", "USD", NOW)
        self.assertEqual(identity["buy_rate"], 1.)
        self.assertEqual(identity["sources"], [])
        self.assertEqual(provider.deribit.calls, [])
        self.assertEqual(validate_fx(identity, "USD", NOW), identity)
        with self.assertRaises(DataError): identity_fx("USDC", NOW)
        with self.assertRaises(DataError): conversions.quote("USDC", "USD", NOW)

    def test_actual_deribit_currency_index_does_not_assume_a_peg(self):
        provider = Provider({"usdt_usd": .9987})
        fx = CurrencyConversions(provider).quote("USDT", "USD", NOW)
        self.assertEqual(fx["buy_rate"], .9987)
        self.assertEqual(fx["sell_rate"], .9987)
        self.assertEqual(fx["sources"][0]["pair"], "USDT/USD")
        self.assertFalse(fx["sources"][0]["executable"])
        self.assertEqual(fx["sources"][0]["role"], "usd_account_reference")
        with self.assertRaises(DataError): validate_fx(fx, "USDC", NOW)

    def test_inverse_index_uses_reciprocal_and_exact_units(self):
        provider = Provider({"usd_usdc": 1.02})
        fx = CurrencyConversions(provider).quote("USDC", "USD", NOW)
        self.assertAlmostEqual(fx["buy_rate"], 1. / 1.02)
        self.assertEqual(fx["sources"][0]["direction"], "inverse")

    def test_binance_preference_uses_spot_then_explicit_usd_terminal(self):
        provider = Provider({"btc_usd": 999999., "usdc_usd": .99}, {"BTC/USDC": (100., 101.)}, "binance")
        fx = CurrencyConversions(provider).quote("BTC", "USD", NOW)
        self.assertAlmostEqual(fx["buy_rate"], 101. * .99)
        self.assertAlmostEqual(fx["sell_rate"], 100. * .99)
        self.assertEqual([s["venue"] for s in fx["sources"]], ["Binance", "Deribit"])
        self.assertEqual(provider.binance.calls, ["BTC/USDC"])
        self.assertEqual(provider.deribit.calls[0][1]["index_name"], "usdc_usd")

    def test_reverse_spot_buy_uses_inverse_bid_and_sell_inverse_ask(self):
        provider = Provider({"btc_usd": 100.}, {"BTC/X": (4., 5.)})
        fx = CurrencyConversions(provider).quote("X", "USD", NOW)
        self.assertEqual(fx["buy_rate"], 25.)
        self.assertEqual(fx["sell_rate"], 20.)
        self.assertEqual(fx["sources"][0]["direction"], "inverse")

    def test_native_binance_usd_pair_wins_over_terminal_index(self):
        provider = Provider({"usdc_usd": .99}, {"USDC/USD": (.98, 1.01)}, "binance")
        def failed_catalog(): raise DataError("Deribit unavailable")
        provider.deribit.available_indices = failed_catalog
        fx = CurrencyConversions(provider).quote("USDC", "USD", NOW)
        self.assertEqual((fx["buy_rate"], fx["sell_rate"]), (1.01, .98))
        self.assertEqual(fx["sources"][0]["venue"], "Binance")
        self.assertEqual(provider.deribit.calls, [])

    def test_path_is_bounded_to_three_actual_markets(self):
        provider = Provider({"usdt_usd": .99}, {"A/B": (2., 3.), "B/USDT": (4., 5.)})
        fx = CurrencyConversions(provider).quote("A", "USD", NOW)
        self.assertEqual(len(fx["sources"]), 3)
        self.assertAlmostEqual(fx["buy_rate"], 3. * 5. * .99)
        self.assertAlmostEqual(fx["sell_rate"], 2. * 4. * .99)
        provider = Provider({"usdt_usd": .99}, {"A/B": (2., 3.), "B/C": (4., 5.), "C/USDT": (1., 2.)})
        with self.assertRaises(DataError): CurrencyConversions(provider).quote("A", "USD", NOW)

    def test_cache_preserves_original_timestamps_and_refreshes_expired_rate(self):
        provider = Provider({"usdc_usd": .99})
        conversions = CurrencyConversions(provider)
        first = conversions.quote("USDC", "USD", NOW)
        provider.stamp += 19000
        provider.deribit.indices["usdc_usd"] = .95
        cached = conversions.quote("USDC", "USD", provider.now())
        self.assertEqual(cached["asof_ms"], first["asof_ms"])
        self.assertEqual(cached["expires_ms"], first["expires_ms"])
        self.assertEqual(cached["buy_rate"], .99)
        cached["sources"][0]["bid"] = 3.
        self.assertEqual(conversions.quote("USDC", "USD", provider.now())["buy_rate"], .99)
        provider.stamp += 1000
        refreshed = conversions.quote("USDC", "USD", provider.now())
        self.assertEqual(refreshed["buy_rate"], .95)
        self.assertEqual(refreshed["asof_ms"], NOW + 20000)
        self.assertEqual(len(provider.deribit.calls), 2)

    def test_outage_never_falls_back_after_supported_index_selection(self):
        provider = Provider({"usdc_usd": .99}, {"USDC/USDT": (.9, 1.)})
        provider.deribit.fail = True
        with self.assertRaises(DataError) as context:
            CurrencyConversions(provider).quote("USDC", "USD", NOW)
        self.assertNotIn("secret-token", str(context.exception))
        self.assertEqual(provider.binance.calls, [])

    def test_explicit_refresh_captures_a_new_rate_before_cache_expiry(self):
        provider = Provider({"usdc_usd": .99})
        conversions = CurrencyConversions(provider)
        first = conversions.quote("USDC", "USD", NOW)
        provider.stamp += 15000
        provider.deribit.indices["usdc_usd"] = .98
        second = conversions.quote("USDC", "USD", provider.now(), refresh=True)
        self.assertEqual(second["buy_rate"], .98)
        self.assertEqual(second["asof_ms"], NOW + 15000)
        self.assertGreater(second["expires_ms"], first["expires_ms"])

    def test_expired_cached_snapshot_is_not_retimestamped_during_outage(self):
        provider = Provider({"usdc_usd": .99})
        conversions = CurrencyConversions(provider)
        fx = conversions.quote("USDC", "USD", NOW)
        provider.stamp = fx["expires_ms"]
        provider.deribit.fail = True
        with self.assertRaises(DataError): conversions.quote("USDC", "USD", provider.now())

    def test_validator_rejects_future_expiry_path_and_rate_tampering(self):
        provider = Provider({"usdc_usd": .99})
        fx = CurrencyConversions(provider).quote("USDC", "USD", NOW)
        with self.assertRaises(DataError): validate_fx(fx, "USDC", NOW - 1)
        with self.assertRaises(DataError): validate_fx(fx, "USDC", fx["expires_ms"])
        for mutate in (lambda x: x.update(buy_rate=1.),
                       lambda x: x.update(expires_ms=NOW + 60000),
                       lambda x: x["sources"][0].update(to_currency="USDT"),
                       lambda x: x["sources"][0].update(direction="inverse"),
                       lambda x: x["sources"][0].update(bid=.5)):
            changed = copy.deepcopy(fx); mutate(changed)
            with self.assertRaises(DataError): validate_fx(changed, "USDC", NOW)

    def test_slow_path_must_not_combine_unsynchronised_currency_quotes(self):
        provider = Provider({"usdt_usd": .99}, {"A/USDT": (2., 3.)})
        original = provider.deribit.request
        def delayed(method, params):
            result = original(method, params)
            provider.stamp += 5001
            return result
        provider.deribit.request = delayed
        with self.assertRaises(DataError): CurrencyConversions(provider).quote("A", "USD", NOW)


if __name__ == "__main__": unittest.main()
