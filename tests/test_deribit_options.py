"""Deribit settlement aggregation checked against analytical units and failure gates."""
import copy
import unittest
from adaptive_crypto.core import DataError
from adaptive_crypto.deribit_options import DeribitOptions
from adaptive_crypto.gex import YEAR_MS, exposure
from adaptive_crypto.currency_conversion import CurrencyConversions
from tests.test_currency_conversion import Provider as FXProvider
from tests.test_gex_formula import NOW


def market(base, prefix, strike, forward, oi=30, size=1, *, settlement=None, price_quote=None):
    name = prefix+"-C"
    settlement = settlement or (base if prefix == "inverse" else "USDC")
    price_quote = price_quote or ("USD" if prefix == "inverse" else "USDC")
    return ([{"instrument_name": name, "base_currency": base, "kind": "option",
        "is_active": True, "settlement_currency": settlement,
        "quote_currency": settlement, "price_index": base.lower()+"_"+price_quote.lower(),
        "expiration_timestamp": NOW+YEAR_MS*.25,
        "strike": strike, "option_type": "call", "contract_size": size}],
        [{"instrument_name": name, "creation_timestamp": NOW, "underlying_price": forward,
          "open_interest": oi, "mark_iv": 50}])


class Client:
    def __init__(self, chains=None, factor=1.1):
        self.chains = chains or {"BTC": market("BTC", "inverse", 100., 102.),
            "USDC": market("BTC", "linear", 110., 112.2, oi=20)}
        self.factor, self.calls = factor, []
        self.initial_catalog = [copy.deepcopy(row) for chain in self.chains.values() for row in chain[0]]
    def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "public/get_index_price":
            index = params["index_name"]
            return {"index_price": {"btc_usdc": 110., "btc_usd": 100.,
                "sol_usdc": 110., "sol_usd": 100., "eth_usd": 100., "eth_usdc": 110.,
                "xrp_usd": 100., "xrp_usdc": 110., "usd_usdc": self.factor, "usdc_usd": 1/self.factor if self.factor else 0}[index]}
        if method == "public/get_instruments":
            assert params["currency"] == "any"
            result = []
            for settlement, chain in self.chains.items():
                if isinstance(chain, Exception):
                    result.extend(row for row in self.initial_catalog if row["settlement_currency"] == settlement)
                else:
                    result.extend(chain[0])
            return copy.deepcopy(result)
        chain = self.chains[params["currency"]]
        if isinstance(chain, Exception):
            raise chain
        return copy.deepcopy(chain[0 if method == "public/get_instruments" else 1])


class DeribitOptionsTests(unittest.TestCase):
    def test_arbitrary_quote_wraps_complete_canonical_options_and_captures_fresh_fx(self):
        provider = FXProvider({"usdt_usd": .8}); provider.stamp = NOW
        fx = CurrencyConversions(provider)
        feed = DeribitOptions(Client(), wall_clock=lambda: provider.stamp/1000, currency_provider=fx)
        canonical = feed.fetch("BTC", "USD")
        first = feed.fetch("BTC", "USDT")
        self.assertEqual(first["status"], "ready")
        self.assertTrue(first["coverage_complete"])
        self.assertEqual(first["option_count"], canonical["option_count"])
        self.assertAlmostEqual(first["net_gex"], canonical["net_gex"]/.8)
        self.assertAlmostEqual(first["sources"][0]["net_gex"], canonical["sources"][0]["net_gex"]/.8)
        self.assertEqual(first["sources"][0]["quote_currency"], "USDT")
        self.assertEqual(first["fx_valid_until_ms"], NOW+20000)
        provider.stamp += 15000; provider.deribit.indices["usdt_usd"] = .9
        second = feed.fetch("BTC", "USDT")
        self.assertEqual(second["coordinate_fx"]["buy_rate"], .9)
        self.assertEqual(second["fx_asof_ms"], NOW+15000)
        self.assertEqual(second["fx_valid_until_ms"], NOW+35000)

    def test_inverse_and_linear_sum_in_native_quote_with_observed_nonpar_fx(self):
        client = Client()
        result = DeribitOptions(client, wall_clock=lambda: NOW/1000).fetch("BTC", "USDC")
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["option_count"], 2)
        self.assertEqual(result["price_currency"], "USDC")
        self.assertEqual(result["exposure_currency"], "USDC")
        expected_usd = exposure(dict(strike=100., forward_ratio=1.02, iv=.5,
            years=.25, oi=50., sign=1), 100.)
        self.assertAlmostEqual(result["net_gex"], expected_usd*1.1)
        self.assertAlmostEqual(result["call_wall"]["strike"], 110.)
        self.assertEqual({s["settlement"] for s in result["sources"]}, {"BTC", "USDC"})
        self.assertIn("USDC", result["units"])
        self.assertEqual(client.chains["BTC"][0][0]["strike"], 100.)
        self.assertIn(("public/get_index_price", {"index_name": "usd_usdc"}), client.calls)

    def test_reverse_conversion_matches_same_economic_exposure(self):
        feed = DeribitOptions(Client(), wall_clock=lambda: NOW/1000)
        usd, usdc = feed.fetch("BTC", "USD"), feed.fetch("BTC", "USDC")
        self.assertEqual(usd["status"], "ready")
        self.assertAlmostEqual(usdc["net_gex"], usd["net_gex"]*1.1)
        self.assertAlmostEqual(usdc["call_wall"]["strike"], usd["call_wall"]["strike"]*1.1)

    def test_sol_linear_oi_already_base_coins_and_other_assets_are_excluded(self):
        sol = market("SOL", "sol", 110., 112.2, oi=1110., size=10)
        other = market("ETH", "eth", 2500., 2505., oi=999999.)
        client = Client({"USDC": (sol[0]+other[0], sol[1]+other[1])})
        result = DeribitOptions(client, wall_clock=lambda: NOW/1000).fetch("SOL", "USDC")
        expected = exposure(dict(strike=110., forward_ratio=1.02, iv=.5,
            years=.25, oi=1110., sign=1), 110.)
        self.assertEqual(result["option_count"], 1)
        self.assertAlmostEqual(result["net_gex"], expected)
        self.assertEqual([s["settlement"] for s in result["sources"]], ["USDC"])

    def test_any_broken_active_chain_is_excluded_and_marks_aggregate_partial(self):
        for failure in ("missing", "duplicate_book", "duplicate_instrument", "missing_forward", "stale"):
            with self.subTest(failure=failure):
                client = Client()
                inst, books = client.chains["BTC"]
                if failure == "missing": books.clear()
                elif failure == "duplicate_book": books.append(dict(books[0]))
                elif failure == "duplicate_instrument": inst.append(dict(inst[0]))
                elif failure == "missing_forward": books[0].pop("underlying_price")
                elif failure == "stale": books[0]["creation_timestamp"] = NOW-300001
                result = DeribitOptions(client, wall_clock=lambda: NOW/1000).fetch("BTC", "USDC")
                self.assertEqual(result["status"], "partial")
                self.assertFalse(result["coverage_complete"])
                self.assertEqual(result["option_count"], 1)
                self.assertEqual(result["sources"][0]["status"], "unavailable")

    def test_chain_outage_never_claims_complete_and_all_outages_are_unavailable(self):
        for broken in (("BTC",), ("BTC", "USDC")):
            client = Client()
            for settlement in broken:
                client.chains[settlement] = RuntimeError("upstream detail")
            result = DeribitOptions(client, wall_clock=lambda: NOW/1000).fetch("BTC", "USDC")
            self.assertEqual(result["status"], "partial" if len(broken)==1 else "unavailable")
            self.assertFalse(result["coverage_complete"])
            self.assertNotIn("upstream detail", repr(result))

    def test_empty_or_zero_oi_markets_do_not_fake_an_outage(self):
        for chain in (([], []), market("BTC", "empty", 100., 102., oi=0, settlement="BTC", price_quote="USD")):
            client = Client(); client.chains["BTC"] = chain
            result = DeribitOptions(client, wall_clock=lambda: NOW/1000).fetch("BTC", "USDC")
            self.assertEqual(result["status"], "ready")
            self.assertTrue(result["coverage_complete"])
            self.assertEqual(result["option_count"], 1)

    def test_chain_cache_shares_snapshot_without_mutating_or_refetching_contracts(self):
        client = Client(); feed = DeribitOptions(client, wall_clock=lambda: NOW/1000)
        first = feed.fetch("BTC", "USDC"); first["strikes"][0]["strike"] = 1
        second = feed.fetch("BTC", "USDC")
        self.assertAlmostEqual(second["call_wall"]["strike"], 110.)
        self.assertEqual(sum(m == "public/get_instruments" for m, _ in client.calls), 1)
        self.assertEqual(sum(m == "public/get_book_summary_by_currency" for m, _ in client.calls), 2)

    def test_invalid_fx_or_unsupported_currency_cannot_silently_assume_a_peg(self):
        for factor in (None, 0, -1, float("nan"), True):
            with self.subTest(factor=factor), self.assertRaises((DataError, TypeError)):
                DeribitOptions(Client(factor=factor), wall_clock=lambda: NOW/1000).fetch("BTC", "USDC")
        with self.assertRaises(DataError):
            DeribitOptions(Client(), wall_clock=lambda: NOW/1000).fetch("BTC", "USDT")


    def test_settlement_and_strike_currency_come_from_catalog_for_eth_sol_and_new_base(self):
        for base in ("ETH", "SOL", "XRP"):
            with self.subTest(base=base):
                # A hypothetical newly listed settlement must be discovered, and
                # its premium currency must never be mistaken for strike units.
                client = Client({"NEW": market(base, "new", 100., 102.,
                                               settlement="NEW", price_quote="USD")})
                result = DeribitOptions(client, wall_clock=lambda: NOW/1000).fetch(base, "USDC")
                expected = exposure(dict(strike=100., forward_ratio=1.02, iv=.5,
                                         years=.25, oi=30., sign=1), 100.)*1.1
                self.assertEqual(result["symbol"], base+"/USDC")
                self.assertEqual(result["sources"][0]["settlement"], "NEW")
                self.assertEqual(result["sources"][0]["native_quote"], "USD")
                self.assertAlmostEqual(result["net_gex"], expected)
                self.assertAlmostEqual(result["call_wall"]["strike"], 110.)
                self.assertEqual(result["status"], "ready")

    def test_unlisted_base_reports_unsupported_options_before_requesting_an_index(self):
        client = Client()
        result = DeribitOptions(client, wall_clock=lambda: NOW/1000).fetch("DOGE", "BTC")
        self.assertEqual(result["symbol"], "DOGE/BTC")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["price_currency"], "BTC")
        self.assertEqual(result["error"], "Deribit has no currently listed options for DOGE")
        self.assertFalse(any(method == "public/get_index_price" for method, _ in client.calls))

    def test_catalog_refresh_discovers_new_settlement_without_restarting(self):
        clock = [NOW]
        client = Client()
        feed = DeribitOptions(client, wall_clock=lambda: clock[0]/1000)
        self.assertEqual(feed.fetch("BTC", "USD")["option_count"], 2)
        client.chains["NEW"] = market("BTC", "new", 100., 102., settlement="NEW", price_quote="USD")
        clock[0] += 61000
        result = feed.fetch("BTC", "USD")
        self.assertEqual(result["option_count"], 3)
        self.assertEqual({row["settlement"] for row in result["sources"]}, {"BTC", "USDC", "NEW"})
        self.assertTrue(result["coverage_complete"])

    def test_missing_or_wrong_price_index_cannot_silently_mix_price_units(self):
        for index in (None, "eth_usd", "btc"):
            with self.subTest(index=index):
                client = Client(); client.chains["BTC"][0][0]["price_index"] = index
                with self.assertRaises(DataError):
                    DeribitOptions(client, wall_clock=lambda: NOW/1000).fetch("BTC", "USD")

    def test_failed_catalog_cannot_publish_complete_coverage(self):
        class BrokenCatalog(Client):
            def request(self, method, params):
                if method == "public/get_instruments":
                    return {"invalid": True}
                return super().request(method, params)
        with self.assertRaises(DataError):
            DeribitOptions(BrokenCatalog(), wall_clock=lambda: NOW/1000).fetch("BTC", "USD")


    def test_new_native_strike_currency_uses_observed_fx_and_its_expiry(self):
        provider = FXProvider({"eur_usd": 1.2}); provider.stamp = NOW
        client = Client({"NEW": market("ETH", "new", 100./1.2, 102./1.2,
                                       settlement="NEW", price_quote="EUR")})
        result = DeribitOptions(client, wall_clock=lambda: NOW/1000,
                               currency_provider=CurrencyConversions(provider)).fetch("ETH", "USD")
        expected = exposure(dict(strike=100., forward_ratio=1.02, iv=.5,
                                 years=.25, oi=30., sign=1), 100.)
        self.assertAlmostEqual(result["net_gex"], expected)
        self.assertAlmostEqual(result["call_wall"]["strike"], 100.)
        self.assertEqual(result["sources"][0]["native_quote"], "EUR")
        self.assertEqual(result["sources"][0]["conversion_factor"], 1.2)
        self.assertEqual(result["fx_valid_until_ms"], NOW+20000)

    def test_matching_native_strike_quote_does_not_require_an_unrelated_conversion(self):
        client = Client({"USDC": market("SOL", "sol", 110., 112.2)}, factor=None)
        result = DeribitOptions(client, wall_clock=lambda: NOW/1000).fetch("SOL", "USDC")
        self.assertEqual(result["status"], "ready")
        self.assertNotIn("fx_valid_until_ms", result)
        self.assertEqual([params["index_name"] for method, params in client.calls
                          if method == "public/get_index_price"], ["sol_usdc"])
