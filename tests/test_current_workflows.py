"""Current manual records, paper deletion and atomic administration flows."""
import json,tempfile,unittest
from contextlib import ExitStack
from dataclasses import asdict,replace
from pathlib import Path
from adaptive_crypto.core import Rules
from adaptive_crypto.state_paths import open_stores
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.manual_paper import create_manual_paper
from adaptive_crypto.paper_deletion import delete_paper_trade
from adaptive_crypto.position_options import paper_key
from adaptive_crypto.administration import apply_settings,purge_database
from adaptive_crypto.backups import create_backup
from adaptive_crypto.restore import preview_restore,confirm_restore
from tests.test_deribit_runtime import Spot,Binance,Model,ASSETS,NOW

class CurrentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.settings=self.root/'settings.json';self.base=self.root/'state.json'
        self.rules=Rules(paper_floor=0)
        self.save_settings(self.rules)
        paper,positions=self.stack.enter_context(open_stores(self.base,ASSETS,self.rules,'sqlite',settings_path=self.settings))
        self.rt=DashboardRuntime(ASSETS,self.rules,paper,provider=Spot(),position_store=positions,settings_path=self.settings,gex_provider=lambda *args:{})
        self.rt.state_base=self.base
        self.rt.engine.model=Model()
    def save_settings(self,rules):
        self.settings.write_text(json.dumps({'assets':[{'name':'BTC','symbol':'BTC/USDC','enabled':True,'price_decimals':2}],'strategy':asdict(rules),'refresh_seconds':15}),encoding='utf-8')
    def add_position(self):
        return self.rt.positions.open_position(ASSETS,'BTC','long',100,1,'a'*32,NOW)[0]
    def test_manual_paper_delete_reverses_cash_and_is_idempotent(self):
        trade,created=create_manual_paper(self.rt,'BTC',request_id='b'*32,budget_mode='amount',budget_usd=100)
        self.assertTrue(created);self.assertAlmostEqual(self.rt.store.snapshot()['cash'],1000-trade['collateral']-trade['entry_fee'])
        key=paper_key('neural',trade['id'])
        self.assertTrue(delete_paper_trade(self.rt,key)['deleted'])
        self.assertAlmostEqual(self.rt.store.snapshot()['cash'],1000)
        self.assertFalse(delete_paper_trade(self.rt,key)['deleted'])
        self.assertEqual(self.rt.store.snapshot()['assets']['BTC']['trades'],[])
    def test_apply_preserves_current_position_and_rebinds_shared_reader(self):
        p=self.add_position();self.rt.positions.set_stop(p['id'],95,NOW)
        new_rules=replace(self.rules,perky_min_score=5,max_spread_bps=15)
        self.save_settings(new_rules);apply_settings(self.rt,self.rt.generation)
        self.assertEqual(self.rt.rules,new_rules)
        saved=self.rt.positions.snapshot()['positions'][0]
        self.assertEqual(saved['id'],p['id']);self.assertEqual(saved['stop_price'],95)
        self.assertIs(self.rt.position_neural.shared_engine,self.rt.engine)
        self.assertIs(self.rt.paper_neural.shared_engine,self.rt.engine)
    def test_purge_pauses_and_apply_resumes_only_current_namespaces(self):
        self.add_position();purge_database(self.rt,self.rt.generation)
        self.assertTrue(self.rt.paused);self.assertEqual(self.rt.positions.snapshot()['positions'],[])
        self.assertEqual(set(self.rt.store._backend.database.documents()),{'neural','positions'})
        apply_settings(self.rt,self.rt.generation);self.assertFalse(self.rt.paused)
        self.assertEqual(self.rt.store.snapshot()['cash'],1000)
    def test_current_backup_restore_replaces_and_restores_shared_engine(self):
        p=self.add_position()
        create_backup(self.base,settings=self.settings,database=self.rt.store._backend.database)
        self.rt.positions.close_position(p['id'],110,NOW+1000)
        preview=preview_restore(self.rt,self.rt.generation)
        confirm_restore(self.rt,self.rt.generation,preview['token'],'restore')
        self.assertEqual(self.rt.positions.snapshot()['positions'][0]['status'],'open')
        self.assertIs(self.rt.position_neural.shared_engine,self.rt.engine)
        self.assertIs(self.rt.paper_neural.shared_engine,self.rt.engine)

    def test_pair_settings_rebind_deribit_consumers_but_keep_binance_nn(self):
        from adaptive_crypto.web import create_app
        self.rt.neural_feature_provider=Binance()
        self.rt.engine.feature_provider=self.rt.neural_feature_provider
        provider=self.rt.neural_feature_provider
        app=create_app(self.rt);client=app.test_client()
        doc=json.loads(self.settings.read_text());doc['assets'][0]['symbol']='BTC/USD'
        self.settings.write_text(json.dumps(doc))
        apply_settings(self.rt,self.rt.generation)
        self.assertIs(self.rt.engine.feature_provider,provider)
        self.assertEqual(self.rt.gex.assets['BTC'],'BTC/USD')
        self.rt.engine.model=Model();self.rt.engine.model_error=None
        self.rt.gex.snapshot=lambda name:{'status':'unavailable','error':'fixture'}
        self.rt.scan_once()
        self.assertEqual(self.rt.market['BTC']['symbol'],'BTC/USD')
        self.assertEqual(self.rt.data['BTC']['neural']['input_source']['pair'],'BTC/USDT')
        self.assertEqual(client.get('/api/chart/BTC').json['symbol'],'BTC/USD')
        self.assertEqual(client.get('/api/smc-chart/BTC').json['symbol'],'BTC/USD')

    def test_pair_change_does_not_relabel_existing_holding(self):
        from adaptive_crypto.core import DataError
        p=self.add_position()
        doc=json.loads(self.settings.read_text());doc['assets'][0]['symbol']='BTC/USD'
        self.settings.write_text(json.dumps(doc))
        with self.assertRaisesRegex(DataError,'holding or watch'):
            apply_settings(self.rt,self.rt.generation)
        self.assertEqual(self.rt.positions.snapshot()['positions'][0]['symbol'],'BTC/USDC')
        self.assertEqual(self.rt.assets['BTC']['symbol'],'BTC/USDC')
        with self.assertRaisesRegex(DataError,'holding or watch'):
            DashboardRuntime({'BTC':{'symbol':'BTC/USD','price_decimals':2}},self.rules,
                             self.rt.store,provider=Spot(),position_store=self.rt.positions,
                             settings_path=self.settings)

    def test_offline_import_refuses_to_merge_an_existing_database(self):
        from adaptive_crypto.state_tools import import_state
        from adaptive_crypto.core import DataError
        fresh_base = self.root/"offline.json"
        with open_stores(fresh_base, ASSETS, self.rules, "sqlite", settings_path=self.settings) as (paper, _):
            before = paper.snapshot()
        with self.assertRaisesRegex(DataError, "fresh database"):
            import_state(fresh_base, self.settings)
        with open_stores(fresh_base, ASSETS, self.rules, "sqlite", settings_path=self.settings) as (paper, _):
            self.assertEqual(paper.snapshot(), before)

    def test_source_change_applies_only_to_position_feed(self):
        calls=[]
        self.rt.provider.validate_pair=lambda pair,preference=None: calls.append((pair,preference))
        self.rt.provider.set_preference=lambda value: setattr(self.rt.provider,'preference',value)
        self.rt.neural_feature_provider=Binance()
        self.rt.engine.feature_provider=self.rt.neural_feature_provider
        neural_feed=self.rt.neural_feature_provider
        next_rules=replace(self.rules,market_data_source='binance')
        self.save_settings(next_rules)
        apply_settings(self.rt,self.rt.generation)
        self.assertEqual(calls,[('BTC/USDC','binance')])
        self.assertEqual(self.rt.provider.preference,'binance')
        self.assertIs(self.rt.engine.feature_provider,neural_feed)
        self.rt.engine.model=Model();self.rt.engine.model_error=None
        self.rt.gex.snapshot=lambda name:{'status':'unavailable','error':'fixture'}
        self.rt.scan_once()
        self.assertEqual(self.rt.market['BTC']['symbol'],'BTC/USDC')
        self.assertEqual(self.rt.data['BTC']['neural']['input_source']['pair'],'BTC/USDT')
        self.assertEqual(self.rt.engine.model.prices,[200.])

    def test_main_clock_failure_does_not_replace_original_nn_feed(self):
        self.rt.engine.feature_provider=Binance()
        reading=self.rt.engine.read('BTC',[],NOW,{'clock':'Selected market offline'})
        self.assertEqual(reading['signal']['label'],'BUY')
        self.assertEqual(reading['input_source']['pair'],'BTC/USDT')
        self.assertEqual(self.rt.engine.model.prices,[200.])

    def test_unicode_exact_exchange_pair_normalisation(self):
        from adaptive_crypto.core import normalise_pair
        self.assertEqual(normalise_pair('币安人生/USDC'),'币安人生/USDC')

    def test_selected_quote_receipt_uses_smc_observation_clock(self):
        from adaptive_crypto.market_feed import DashboardMarketData
        source=Spot()
        source.supports_pair=lambda pair: True
        source.validate_pair=lambda pair: pair
        router=DashboardMarketData(deribit=source,binance=source,wall_clock=lambda:NOW/1000)
        quote=router.quotes(['BTC/USDC'])['BTC/USDC']
        self.assertEqual(quote['asof_ms'],NOW)
        self.assertEqual(quote['provider_observed_ms'],NOW)
        source.quotes=lambda pairs:{p:{'bid':100.,'ask':100.1,'last':100.,'asof_ms':NOW+1500,'symbol':p} for p in pairs}
        quote=router.quotes(['BTC/USDC'])['BTC/USDC']
        self.assertEqual(quote['asof_ms'],NOW)
        self.assertEqual(quote['provider_observed_ms'],NOW+1500)
