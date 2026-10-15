"""Offline integration of the one Parente reader with shared Deribit SMC/paper/UI."""
from dataclasses import asdict
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adaptive_crypto.core import Candle, H4, M5, Rules
from adaptive_crypto.neural_ledger import NeuralStore
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.web import create_app

NOW = (1800000000000 // H4) * H4 + 1000
ASSETS = {"BTC": {"symbol": "BTC/USDC", "price_decimals": 2}}

def bars(interval, price=100.):
    end = NOW // interval * interval
    return [Candle(end-(120-i)*interval, price, price+1, price-1, price, 100., interval) for i in range(120)]

class Spot:
    candle_source = {"venue": "Deribit", "market": "perpetual", "volume": "base_asset"}
    def __init__(self): self.requested = []; self.fx_provider = FixtureFX()
    def now(self): return NOW
    def candles(self, pair, interval, now):
        self.requested.append((pair, interval)); return bars(interval)
    def chart_candles(self, pair, interval, now): return self.candles(pair, interval, now)
    def source_metadata(self, pair, interval, now=None):
        return {**self.candle_source, "pair": pair, "quote_asset": pair.split("/")[1], "interval_ms": interval}
    def quotes(self, pairs):
        return {p: {"bid":100., "ask":100.01,"last":100.,"asof_ms":NOW,"synthetic":True,"executable":False} for p in pairs}

class FixtureFX:
    def quote(self, currency, account_currency="USD", now=None):
        from adaptive_crypto.currency_conversion import identity_fx
        if currency == account_currency:
            return identity_fx(currency, now, account_currency)
        source = {"from_currency": currency, "to_currency": "USD", "pair": currency+"/USD",
                  "venue": "Deribit", "direction": "direct", "bid": 1., "ask": 1.,
                  "buy_rate": 1., "sell_rate": 1., "asof_ms": now, "expires_ms": now+20000}
        return {"quote_currency": currency, "account_currency": "USD", "buy_rate": 1., "sell_rate": 1.,
                "asof_ms": now, "expires_ms": now+20000, "sources": [source]}

class Binance:
    def candles(self, asset, now):
        return bars(H4,200.), {"venue":"Binance", "market":"spot", "pair":asset+"/USDT", "quote_asset":"USDT", "volume":"base_asset", "interval_ms":H4, "completed_only":True}

class Model:
    identity = "a"*64
    trained_through_ms = 0
    metadata = {}
    volume_stats = {"BTC":{}}
    def __init__(self): self.prices=[]
    def predict(self, candles, asset):
        self.prices.append(candles[-1].c)
        return {"label":"BUY","model_id":self.identity,"signal_end":candles[-1].end,"probabilities":{"BUY":.8,"HOLD":.1,"SELL":.1}}

class DeribitRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)
        self.rules=Rules(fee_rate=.001,paper_floor=0)
        self.settings=self.directory/'settings.json'
        self.settings.write_text(json.dumps({"assets":[{"name":"BTC","symbol":"BTC/USDC","enabled":True,"price_decimals":2}],"strategy":asdict(self.rules),"refresh_seconds":15}))
        store=NeuralStore(self.directory/'state.neural.json',ASSETS,self.rules)
        self.provider=Spot()
        self.runtime=DashboardRuntime(ASSETS,self.rules,store,provider=self.provider,settings_path=self.settings,gex_provider=lambda _: {})
        self.runtime.gex.snapshot=lambda _: {"status":"unavailable","error":"No fixture options"}
        self.runtime.gex.diagnostics=lambda now_ms=None: {"BTC":{"status":"unavailable","error":"No fixture options"}}
        self.model=Model();self.runtime.engine.model=self.model;self.runtime.engine.model_error=None
        self.runtime.neural_feature_provider=Binance();self.runtime.engine.feature_provider=self.runtime.neural_feature_provider

    def test_model_uses_binance_while_chart_uses_settings_deribit_pair(self):
        self.runtime.scan_once();self.runtime.scan_once()
        self.assertEqual(self.model.prices,[200.])
        self.assertTrue(all(p=='BTC/USDC' for p,_ in self.provider.requested))
        self.assertEqual(self.runtime.market['BTC']['input_source']['venue'],'Deribit')
        self.assertEqual(self.runtime.data['BTC']['neural']['input_source']['venue'],'Binance')
        self.assertEqual(self.runtime.pine_charts['BTC']['symbol'],'BTC/USDC')
        self.assertEqual(self.runtime.pine_charts['BTC']['candles'][-1]['c'],100.)
        self.assertEqual(self.runtime.store.snapshot()['assets']['BTC']['trades'],[])

    def test_runtime_passes_qualified_plan_to_paper_and_deduplicates(self):
        plan={"id":"pine:runtime","source":"pine_smc","side":"long","stop":90.,"tp1":110.,"tp2":120.,"exit_price":120.,"target":110.,"signal_end":NOW//M5*M5-1,"expires_ms":NOW+30000,"checks":[{"key":"pine_rejection","status":"pass"}]}
        market={"source":"pine_smc","error":None,"guide":{"status":"qualified","expires_ms":NOW+30000},"suggestion":plan,"suggestions":[plan],"gex_context":{},"smc_long":{},"smc_short":{}}
        with patch('adaptive_crypto.pine_guidance.analyse_pine_market',return_value=copy.deepcopy(market)),patch('adaptive_crypto.pine_alerts.monitor_pine_setups'):
            self.runtime.scan_once();self.runtime.scan_once()
        trades=self.runtime.store.snapshot()['assets']['BTC']['trades']
        self.assertEqual(len(trades),1)
        self.assertEqual((trades[0]['entry_origin'],trades[0]['stop'],trades[0]['target']),('smc_parente',90.,120.))
        self.assertEqual(self.model.prices,[200.])

    def test_pages_settings_and_chart_share_pair_and_retired_routes_are_absent(self):
        self.runtime.scan_once();client=create_app(self.runtime).test_client()
        for route in ('/','/paper-trading','/positions','/settings','/smc-chart'):
            response=client.get(route);self.assertEqual(response.status_code,200,route)
        settings=client.get('/settings').get_data(as_text=True)
        self.assertIn('Parente 5/2',settings);self.assertNotIn('Self-train candidate',settings)
        self.assertNotIn('nn-model-choice',settings)
        chart=client.get('/api/chart/BTC');self.assertEqual(chart.status_code,200)
        self.assertEqual(chart.json['symbol'],'BTC/USDC');self.assertEqual(chart.json['input_source']['venue'],'Deribit')
        self.assertEqual(client.get('/api/smc-chart/BTC').json['symbol'],'BTC/USDC')
        self.assertEqual(client.get('/api/models/self-training/status').status_code,404)
        self.assertEqual(client.get('/api/models/retired/report').status_code,404)

    def test_runtime_respects_disabled_gex_target_setting(self):
        from dataclasses import replace
        self.runtime.rules = replace(self.rules, smc_gex_targets=False)
        self.runtime.scan_once()
        self.assertFalse(self.runtime.market["BTC"]["gex_context"]["enabled"])

    def test_account_currency_is_usd_while_market_and_nn_keep_native_inputs(self):
        self.runtime.scan_once()
        snapshot = self.runtime.snapshot()
        market = snapshot["market"]["BTC"]
        self.assertEqual(snapshot["account_currency"], "USD")
        self.assertEqual(snapshot["quote_currency"], "USD")
        self.assertEqual(market["symbol"], "BTC/USDC")
        self.assertEqual(market["account_conversion"]["quote_currency"], "USDC")
        self.assertEqual(market["account_conversion"]["account_currency"], "USD")
        self.assertEqual(market["nn"]["input_source"]["pair"], "BTC/USDT")

    def test_fx_outage_blocks_paper_without_disabling_structure_or_nn(self):
        from adaptive_crypto.core import DataError
        class MissingFX:
            def quote(self, *args, **kwargs):
                raise DataError("Currency market temporarily unavailable")
        self.runtime.paper_fx = MissingFX()
        self.runtime.scan_once()
        snapshot = self.runtime.snapshot()
        market = snapshot["market"]["BTC"]
        self.assertIsNone(market["account_conversion"])
        self.assertIn("temporarily unavailable", market["account_conversion_error"])
        self.assertNotIn("paper_fx", market.get("errors", {}))
        self.assertEqual(market["pine_structure"]["status"], "ready")
        self.assertEqual(market["nn"]["signal"]["label"], "BUY")
        self.assertEqual(snapshot["data"]["BTC"]["neural"]["active_count"], 0)
        self.assertIn("fresh USD conversion", snapshot["data"]["BTC"]["neural"]["result"])

if __name__=='__main__': unittest.main()
