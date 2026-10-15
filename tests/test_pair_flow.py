"""Mixed pair Settings reach the actual Pine, GEX, Parente, caches and APIs.

Only exchange responses are fixtures; the models and dashboard run unchanged.
"""
from contextlib import ExitStack
from dataclasses import asdict
import json
import math
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adaptive_crypto.administration import apply_settings
from adaptive_crypto.core import Candle, H4, M5, Rules
from adaptive_crypto.currency_conversion import identity_fx
from adaptive_crypto.gex import YEAR_MS, build_profile
from adaptive_crypto.gex_currency import convert_usd_profile
from adaptive_crypto.neural import NeuralModel
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.settings_editor import read_editor, save_editor
from adaptive_crypto.state_paths import open_stores
from adaptive_crypto.web import create_app

NOW = (1800000000000 // H4) * H4 + 1000
USD_PRICES = {"BTC": 50000., "ETH": 2400., "SOL": 120., "XRP": .7, "SUI": 2.}
USD_RATES = {"USD": 1., "USDC": 1.2, "USDT": .8, "BTC": 50000., "ETH": 2400.}


def history(interval, price, count=160):
    end = NOW // interval * interval
    factors = [1 + .008 * math.sin(i/3) + .0001*i for i in range(count)]
    return [Candle(end-(count-i)*interval, price*factor/factors[-1]*.998,
                   price*factor/factors[-1]*1.01, price*factor/factors[-1]*.99,
                   price*factor/factors[-1], 100+(i % 17), interval)
            for i, factor in enumerate(factors)]


class RecordedFX:
    """Captured non-par USD reference rates, including a crypto quote."""
    def quote(self, currency, account_currency="USD", now=None, refresh=False):
        if currency == account_currency:
            return identity_fx(currency, now, account_currency)
        rate = USD_RATES[currency]
        source = {"from_currency": currency, "to_currency": "USD", "pair": currency+"/USD",
                  "venue": "Deribit", "direction": "direct", "bid": rate, "ask": rate,
                  "buy_rate": rate, "sell_rate": rate, "asof_ms": now, "expires_ms": now+20000}
        return {"quote_currency": currency, "account_currency": "USD", "buy_rate": rate,
                "sell_rate": rate, "asof_ms": now, "expires_ms": now+20000, "sources": [source]}


class RecordedMarkets:
    candle_source = {"venue": "Deribit", "market": "perpetual", "volume": "base_asset"}
    def __init__(self):
        self.requested, self.quote_requests, self.validated = [], [], []
        self.fx_provider = RecordedFX()
        self.preference = "deribit_first"
    def now(self):
        return NOW
    def validate_pair(self, pair, preference=None):
        base, quote = pair.split("/")
        if base not in USD_PRICES or quote not in USD_RATES:
            raise ValueError("Unrecorded fixture pair")
        self.validated.append((pair, preference))
        return pair
    def set_preference(self, value):
        self.preference = value
    @staticmethod
    def price(pair):
        base, quote = pair.split("/")
        return USD_PRICES[base]/USD_RATES[quote]
    def candles(self, pair, interval, now):
        self.requested.append((pair, interval))
        return history(interval, self.price(pair))
    def chart_candles(self, pair, interval, now):
        return self.candles(pair, interval, now)
    def quotes(self, pairs):
        self.quote_requests.extend(pairs)
        return {pair: {"bid": self.price(pair), "ask": self.price(pair)*1.0001,
                       "last": self.price(pair), "asof_ms": NOW, "symbol": pair,
                       "synthetic": True, "executable": False} for pair in pairs}
    def source_metadata(self, pair, interval, now=None):
        return {**self.candle_source, "pair": pair, "quote_asset": pair.split("/")[1],
                "interval_ms": interval, "completed_only": True}


class RecordedBinance:
    def __init__(self):
        self.requested = []
    def candles(self, asset, now):
        self.requested.append(asset+"/USDT")
        return history(H4, USD_PRICES[asset]*1.01), {
            "venue": "Binance", "market": "spot", "pair": asset+"/USDT",
            "quote_asset": "USDT", "volume": "base_asset", "interval_ms": H4,
            "completed_only": True}


class RecordedOptions:
    def __init__(self, fx):
        self.fx, self.requested = fx, []
    def __call__(self, base, quote):
        self.requested.append((base, quote))
        spot = USD_PRICES[base]
        instruments, summaries = [], []
        # No SUI option chain was captured: do not fabricate its availability.
        if base != "SUI":
            for kind, multiple, oi in (("call", 1.1, 30), ("put", .9, 20)):
                name = base+"-"+kind
                instruments.append({"instrument_name": name, "base_currency": base,
                    "kind": "option", "is_active": True, "option_type": kind,
                    "expiration_timestamp": NOW+YEAR_MS//4, "strike": spot*multiple})
                summaries.append({"instrument_name": name, "creation_timestamp": NOW,
                    "open_interest": oi, "mark_iv": 50, "underlying_price": spot})
        profile = build_profile(instruments, summaries, base, spot, NOW)
        profile.update(symbol=base+"/USD", coverage_complete=True)
        if quote != "USD":
            profile = convert_usd_profile(profile, quote, self.fx.quote(quote, "USD", NOW), NOW)
        return profile


class PairFlowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch("adaptive_crypto.runtime.time.time", return_value=NOW/1000))
        self.rules = Rules(paper_floor=999.)  # No allocation above the $10 minimum can open.
        self.settings, self.base = self.root/"settings.json", self.root/"state.json"
        self.assets = {base: {"symbol": base+"/USD", "price_decimals": 6}
                       for base in ("BTC", "ETH", "SOL")}
        self.settings.write_text(json.dumps(self.document(self.assets)), encoding="utf-8")
        paper, positions = self.stack.enter_context(open_stores(
            self.base, self.assets, self.rules, "sqlite", settings_path=self.settings))
        self.provider = RecordedMarkets()
        self.options = RecordedOptions(self.provider.fx_provider)
        self.runtime = DashboardRuntime(self.assets, self.rules, paper, provider=self.provider,
            position_store=positions, settings_path=self.settings, gex_provider=self.options)
        self.runtime.state_base = self.base
        self.binance = RecordedBinance()
        self.runtime.neural_feature_provider = self.binance
        self.runtime.engine.feature_provider = self.binance
        self.client = create_app(self.runtime).test_client()

    def document(self, assets):
        return {"assets": [{"name": name, **cfg, "enabled": True} for name, cfg in assets.items()],
                "strategy": asdict(self.rules), "refresh_seconds": 15}

    def apply_pairs(self, pairs):
        doc, revision = read_editor(self.runtime)
        doc["assets"] = [{"name": base, "symbol": pair, "enabled": True, "price_decimals": 6}
                         for base, pair in pairs.items()]
        save_editor(self.runtime, doc, revision)
        apply_settings(self.runtime, self.runtime.generation)

    def scan(self):
        # Prime the real cache synchronously so worker timing cannot race assertions.
        for name in self.runtime.assets:
            self.runtime.gex.get(name)
            self.runtime.gex.next_refresh[name] = self.runtime.gex.cache[name]["retry_at"]
        self.runtime.scan_once()

    def active_banner_pairs(self):
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        banner = re.search(r'<p\b[^>]*id="active-market-pairs"[^>]*>(.*?)</p>',
                           response.get_data(as_text=True), re.DOTALL)
        self.assertIsNotNone(banner, "Settings must show the applied market pair list")
        return re.findall(r"[A-Z0-9]+/[A-Z0-9]+", banner.group(1))

    def assert_pair_outputs(self, pairs):
        self.assertEqual(self.active_banner_pairs(), list(pairs.values()))
        self.assertEqual(self.runtime.gex.assets, pairs)
        self.assertIsInstance(self.runtime.engine.model, NeuralModel)
        self.assertIs(self.runtime.engine.feature_provider, self.binance)
        self.assertIs(self.runtime.position_neural.shared_engine, self.runtime.engine)
        self.assertIs(self.runtime.paper_neural.shared_engine, self.runtime.engine)
        requested = set(self.provider.requested)
        for base, pair in pairs.items():
            with self.subTest(pair=pair):
                quote_currency = pair.split("/")[1]
                price = self.provider.price(pair)
                for interval in (H4, M5, self.rules.smc_setup_minutes*60000):
                    self.assertIn((pair, interval), requested)
                self.assertIn(pair, self.provider.quote_requests)
                self.assertIn((base, quote_currency), self.options.requested)
                market = self.runtime.market[base]
                self.assertEqual(market["symbol"], pair)
                self.assertEqual(market["input_source"]["pair"], pair)
                self.assertEqual(market["input_source"]["quote_asset"], quote_currency)
                self.assertEqual(market["pine_structure"]["status"], "ready")
                self.assertAlmostEqual(market["pine_structure"]["close"], price)
                self.assertAlmostEqual(market["liquidity"]["reference_price"], price*1.00005)
                self.assertEqual(market["account_conversion"]["quote_currency"], quote_currency)
                self.assertEqual(market["account_conversion"]["account_currency"], "USD")
                self.assertEqual(market["nn"]["input_source"]["pair"], base+"/USDT")
                chart = self.client.get("/api/chart/"+base)
                self.assertEqual(chart.status_code, 200)
                self.assertEqual(chart.json["symbol"], pair)
                self.assertEqual(chart.json["input_source"]["pair"], pair)
                self.assertAlmostEqual(chart.json["candles"][-1]["c"], price)
                pine = self.client.get("/api/smc-chart/"+base)
                self.assertEqual(pine.status_code, 200)
                self.assertEqual(pine.json["symbol"], pair)
                self.assertEqual(pine.json["input_source"]["pair"], pair)
                self.assertAlmostEqual(pine.json["candles"][-1]["c"], price)
                self.assertAlmostEqual(pine.json["quote"]["bid"], price)
                if base != "SUI":
                    self.assertIsNotNone(market["nn"]["signal"], market["nn"]["error"])
                    profile = self.client.get("/api/gex/"+base)
                    self.assertEqual(profile.status_code, 200, profile.json)
                    self.assertEqual(profile.json["status"], "ready")
                    self.assertEqual(profile.json["symbol"], pair)
                    self.assertEqual(profile.json["price_currency"], quote_currency)
                    self.assertEqual(profile.json["exposure_currency"], quote_currency)
                    self.assertAlmostEqual(profile.json["spot"], price)
                    self.assertAlmostEqual(profile.json["call_wall"]["strike"], price*1.1)
                    self.assertAlmostEqual(profile.json["put_wall"]["strike"], price*.9)
        self.assertEqual(self.runtime.snapshot()["account_currency"], "USD")
        self.assertEqual(sum(len(row["trades"]) for row in self.runtime.store.snapshot()["assets"].values()), 0)

    def test_saved_but_unapplied_edits_leave_active_banner_and_caches_on_current_pairs(self):
        old = {name: cfg["symbol"] for name, cfg in self.assets.items()}
        self.scan()
        self.assert_pair_outputs(old)
        pairs = {"BTC": "BTC/USDC", "ETH": "ETH/BTC", "SOL": "SOL/USDT"}
        document, revision = read_editor(self.runtime)
        for entry in document["assets"]:
            entry["symbol"] = pairs[entry["name"]]
        save_editor(self.runtime, document, revision)
        self.assertEqual(read_editor(self.runtime)[0]["assets"], document["assets"])
        self.assertEqual(self.active_banner_pairs(), list(old.values()))
        self.assertEqual(self.runtime.gex.assets, old)
        for name, pair in old.items():
            self.assertEqual(self.client.get("/api/chart/"+name).json["symbol"], pair)
            self.assertEqual(self.client.get("/api/smc-chart/"+name).json["symbol"], pair)
        apply_settings(self.runtime, self.runtime.generation)
        self.assertEqual(self.active_banner_pairs(), list(pairs.values()))
        self.scan()
        self.assert_pair_outputs(pairs)

    def test_eth_and_sol_mixed_quotes_flow_through_real_smc_gex_nn_and_apis(self):
        pairs = {"BTC": "BTC/USDC", "ETH": "ETH/BTC", "SOL": "SOL/USDT"}
        self.apply_pairs(pairs)
        self.scan()
        self.assert_pair_outputs(pairs)
        self.assertEqual(set(self.binance.requested), {base+"/USDT" for base in pairs})

    def test_apply_changes_every_asset_and_adds_xrp_without_old_cached_symbols(self):
        old = {name: cfg["symbol"] for name, cfg in self.assets.items()}
        self.scan()
        self.assert_pair_outputs(old)  # Populate charts, Pine snapshots, GEX and NN caches.
        previous_engine, previous_gex = self.runtime.engine, self.runtime.gex
        self.provider.requested.clear()
        self.provider.quote_requests.clear()
        self.options.requested.clear()
        self.binance.requested.clear()
        pairs = {"BTC": "BTC/USDT", "ETH": "ETH/USDC", "SOL": "SOL/BTC", "XRP": "XRP/USDC"}
        self.apply_pairs(pairs)
        self.assertIsNot(self.runtime.engine, previous_engine)
        self.assertIsNot(self.runtime.gex, previous_gex)
        self.assertEqual(self.runtime.engine.cache, {})
        self.assertEqual(self.runtime.gex.cache, {})
        self.assertEqual(self.runtime.market, {})
        self.assertEqual(self.runtime.pine_charts, {})
        for name, pair in pairs.items():
            self.assertEqual(self.client.get("/api/smc-chart/"+name).json["symbol"], pair)
        self.scan()
        self.assert_pair_outputs(pairs)
        self.assertFalse(set(old.values()).intersection(pair for pair, _ in self.provider.requested))
        self.assertEqual(set(self.provider.quote_requests), set(pairs.values()))
        self.assertEqual(set(self.options.requested), {tuple(pair.split("/")) for pair in pairs.values()})
        self.assertEqual(set(self.binance.requested), {base+"/USDT" for base in pairs})

    def test_untrained_new_asset_keeps_selected_pair_smc_and_charts_available(self):
        pairs = {"ETH": "ETH/USDC", "SOL": "SOL/USDT", "SUI": "SUI/USDC"}
        self.apply_pairs(pairs)
        self.assertNotIn("SUI", self.runtime.engine.model.volume_stats)
        self.scan()
        self.assert_pair_outputs(pairs)
        reading = self.runtime.market["SUI"]["nn"]
        self.assertIsNone(reading["signal"])
        self.assertIn("no frozen volume calibration for SUI", reading["error"])
        profile = self.client.get("/api/gex/SUI")
        self.assertEqual(profile.status_code, 503)
        self.assertEqual(profile.json["symbol"], "SUI/USDC")
        self.assertIn("No active options", profile.json["error"])
        self.assertEqual(set(self.binance.requested), {base+"/USDT" for base in pairs})


if __name__ == "__main__":
    unittest.main()


