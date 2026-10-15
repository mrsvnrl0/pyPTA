"""Mixed native quotes share one real, captured-FX USD paper account."""
from dataclasses import replace
import copy
import json
from pathlib import Path
import tempfile
import unittest

from adaptive_crypto.core import DataError, H4, M5
from adaptive_crypto.neural_engine import NeuralEngine
from adaptive_crypto.neural_ledger import (NeuralStore, close_trade, normalize_manual_order,
                                           open_combined_trade, open_manual_trade, prepare_neural, validate)
from adaptive_crypto.storage import portfolio
from adaptive_crypto.manual_paper import create_manual_paper
from tests.currency_fixtures import fx
from tests.neural_fixtures import NOW, RULES, FakeModel, bars, quote, qualified_market


def signal(now=NOW):
    return {"label":"BUY","signal_end":now//H4*H4-1,"model_id":"a"*64,
            "probabilities":{"BUY":.8,"HOLD":.1,"SELL":.1}}


class PaperCurrencyAccountTests(unittest.TestCase):
    def setUp(self):
        self.rules=replace(RULES,nn_limitations=False,paper_equity=10000.,fee_rate=.01,slippage_rate=0.)
        self.assets={"ETH":{"symbol":"ETH/BTC","price_decimals":8},
                     "SOL":{"symbol":"SOL/USDC","price_decimals":2},
                     "BTC":{"symbol":"BTC/USD","price_decimals":2}}
        self.document=prepare_neural(None,self.assets,self.rules)[0]

    def manual(self,name,price,budget,conversion,request_id="1"*32,now=NOW):
        order=normalize_manual_order(request_id,"amount",budget,manual_price=price)
        return open_manual_trade(self.document,name,signal(now),quote(now,price),self.rules,now,order,fx=conversion)[0]

    def test_crypto_quote_price_quantity_and_usd_budget_have_distinct_units(self):
        captured=fx(NOW,"BTC",80000.,79900.)
        trade=self.manual("ETH",.03,1000.,captured)
        expected_quantity=1000/(.03*80000*1.01)
        self.assertAlmostEqual(trade["quantity"],expected_quantity)
        self.assertEqual(trade["entry"],.03)
        self.assertEqual(trade["symbol"],"ETH/BTC")
        self.assertAlmostEqual(trade["quote_collateral"],trade["quantity"]*.03)
        self.assertAlmostEqual(trade["collateral"]+trade["entry_fee"],1000.)
        self.assertAlmostEqual(self.document["cash"],9000.)
        self.assertEqual(self.document["account_currency"],"USD")
        captured["buy_rate"]=1.
        self.assertEqual(trade["entry_fx"]["buy_rate"],80000.)
        validate(self.document)

    def test_exit_uses_new_market_fx_and_keeps_native_pnl_separate(self):
        trade=self.manual("ETH",.03,1000.,fx(NOW,"BTC",80000.,79900.))
        original_fx=copy.deepcopy(trade["entry_fx"])
        end=NOW+1000
        close_trade(self.document,trade,.033,end,"target",fx=fx(end,"BTC",71000.,70000.))
        expected_proceeds=trade["quantity"]*.033*70000.
        expected_fee=expected_proceeds*.01
        expected_pnl=expected_proceeds-expected_fee-1000.
        self.assertAlmostEqual(trade["realized_pnl"],expected_pnl)
        self.assertLess(trade["realized_pnl"],0.)
        self.assertGreater(trade["realized_pnl_quote"],0.)
        self.assertEqual(trade["entry_fx"],original_fx)
        self.assertEqual(trade["exit_fx"]["sell_rate"],70000.)
        self.assertAlmostEqual(self.document["cash"],10000.+expected_pnl)
        validate(self.document)

    def test_mixed_quotes_portfolio_sums_only_usd_costs_and_risk(self):
        eth=self.manual("ETH",.03,1000.,fx(NOW,"BTC",80000.,79900.))
        sol=self.manual("SOL",120.,2000.,fx(NOW,"USDC",1.02,1.01),"2"*32)
        btc=self.manual("BTC",80000.,500.,None,"3"*32)
        book=portfolio(self.document)
        self.assertEqual(book["account_currency"],"USD")
        self.assertAlmostEqual(book["cash"],6500.)
        self.assertAlmostEqual(book["open_risk"],3500.)
        self.assertAlmostEqual(book["cost_equity"],10000.-eth["entry_fee"]-sol["entry_fee"]-btc["entry_fee"])
        self.assertEqual(book["active_positions"],3)
        validate(self.document)

    def test_combined_entry_keeps_native_bracket_and_limits_usd_stop_risk(self):
        rules=replace(self.rules,nn_limitations=True,risk_per_trade=.02,max_total_risk=.05,max_allocation=.9,minimum_notional=1.)
        document=prepare_neural(None,self.assets,rules)[0]
        plan=qualified_market()["suggestion"]
        plan.update(stop=.025,target=.035,tp1=.035,tp2=.04,exit_price=.04)
        conversion=fx(NOW,"BTC",80000.,79000.)
        trade=open_combined_trade(document,"ETH",signal(),plan,{"ask":.03,"bid":.03,"asof_ms":NOW},rules,NOW,fx=conversion)
        self.assertEqual((trade["entry"],trade["stop"],trade["tp1"],trade["tp2"]),(.03,.025,.035,.04))
        self.assertAlmostEqual(trade["initial_risk_usd"],200.)
        risk_unit=.03*1.01*80000.-.025*.99*79000.
        self.assertAlmostEqual(trade["quantity"],200./risk_unit)
        validate(document)

    def test_foreign_currency_without_valid_fx_cannot_open_or_close(self):
        for conversion in (None,fx(NOW-30000,"BTC",80000.),fx(NOW,"USDC")):
            before=copy.deepcopy(self.document)
            with self.subTest(fx=conversion),self.assertRaises(DataError):self.manual("ETH",.03,1000.,conversion)
            self.assertEqual(self.document,before)
        trade=self.manual("ETH",.03,1000.,fx(NOW,"BTC",80000.))
        before=copy.deepcopy(self.document)
        with self.assertRaises(DataError):close_trade(self.document,trade,.033,NOW+30000,"target",fx=fx(NOW,"BTC",80000.))
        self.assertEqual(self.document,before)

    def test_saved_currency_arithmetic_and_fx_trace_are_validated(self):
        self.manual("ETH",.03,1000.,fx(NOW,"BTC",80000.,79900.))
        for field in ("collateral","entry_fee","quote_collateral","initial_risk_usd","entry_fee_quote"):
            document=copy.deepcopy(self.document)
            document["assets"]["ETH"]["trades"][0][field]*=1.1
            with self.subTest(field=field),self.assertRaises(DataError):validate(document)
        document=copy.deepcopy(self.document)
        document["assets"]["ETH"]["trades"][0]["entry_fx"]["sources"][0]["pair"]="USDC/USD"
        with self.assertRaises(DataError):validate(document)

    def test_untraded_pair_can_change_without_relabelling_another_assets_history(self):
        trade=self.manual("ETH",.03,1000.,fx(NOW,"BTC",80000.,79900.))
        proposed=copy.deepcopy(self.assets);proposed["BTC"]["symbol"]="BTC/USDC"
        prepared,_=prepare_neural(json.dumps(self.document).encode(),proposed,self.rules)
        self.assertEqual(prepared["assets"]["BTC"]["symbol"],"BTC/USDC")
        self.assertEqual(prepared["assets"]["ETH"]["trades"][0],trade)
        self.assertEqual(prepared["cash"],self.document["cash"])
        self.assertEqual(prepared["account_currency"],"USD")
        validate(prepared)
        proposed["ETH"]["symbol"]="ETH/USD"
        with self.assertRaisesRegex(DataError,"recorded trades cannot be relabelled"):
            prepare_neural(json.dumps(self.document).encode(),proposed,self.rules)

    def test_previous_native_currency_ledger_is_rejected_instead_of_relabelled(self):
        previous=copy.deepcopy(self.document);previous["version"]="smc-parente-paper-v1";previous.pop("account_currency")
        with self.assertRaisesRegex(DataError,"older currency contract"):
            prepare_neural(json.dumps(previous).encode(),self.assets,self.rules)


class PaperFXExecutionGuardTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.assets={"BTC":{"symbol":"BTC/USDC","price_decimals":2}}
        self.store=NeuralStore(Path(temp.name)/"paper.json",self.assets,RULES)
        self.model=FakeModel()
        self.engine=NeuralEngine(self.assets,RULES,self.store,model=self.model)

    def evaluate(self,now=NOW,price=100.,conversion=None,low=None,market=None):
        return self.engine.evaluate("BTC",quote(now,price),bars(now),bars(now,interval=M5) if low is None else low,now,
                                    smc_market=market,fx_quote=conversion)

    def test_fx_outage_preserves_pending_buy_until_same_setup_can_open(self):
        result=self.evaluate(market=qualified_market())
        self.assertIn("USD conversion",result["neural"]["result"])
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["last_signal_end"],0)
        self.evaluate(NOW+1000,conversion=fx(NOW+1000),market=qualified_market(NOW+1000))
        self.assertEqual(len(self.store.snapshot()["assets"]["BTC"]["trades"]),1)

    def test_manual_entry_rechecks_price_and_fx_after_slow_conversion(self):
        from types import SimpleNamespace
        import threading
        observed={"now":NOW}
        class Provider:
            def now(self): return observed["now"]
            def candles(self,pair,interval,now): return bars(now,interval=interval)
            def quotes(self,pairs): return {pair:quote(observed["now"]) for pair in pairs}
        class SlowFX:
            def quote(self,currency,account,now):
                captured=fx(now,currency)
                observed["now"]+=30000
                return captured
        runtime=SimpleNamespace(is_neural=True,paused=False,stop=threading.Event(),
                                assets=self.assets,store=self.store,engine=self.engine,
                                provider=Provider(),paper_fx=SlowFX(),rules=RULES,wake=threading.Event())
        with self.assertRaisesRegex(DataError,"stale"):
            create_manual_paper(runtime,"BTC",request_id="f"*32,budget_mode="amount",budget_usd=100.)
        self.assertEqual(self.store.snapshot()["assets"]["BTC"]["trades"],[])

    def test_fx_outage_preserves_historical_stop_trigger_and_settles_with_current_fx(self):
        self.evaluate(conversion=fx(NOW),market=qualified_market())
        now=NOW+2*M5
        low=bars(now,interval=M5)
        triggered=low[-1]
        from adaptive_crypto.core import Candle
        low[-1]=Candle(triggered.t,100.,101.,80.,100.,1000.,M5)
        self.model.label="HOLD"
        self.evaluate(now,low=low)
        waiting=self.store.snapshot()["assets"]["BTC"]["trades"][0]
        self.assertEqual(waiting["status"],"active")
        self.assertEqual(waiting["last_bar_end"],NOW)
        self.evaluate(now,conversion=fx(now,buy=.99,sell=.98),low=low)
        closed=self.store.snapshot()["assets"]["BTC"]["trades"][0]
        self.assertEqual(closed["status"],"stopped")
        self.assertEqual(closed["closed_ms"],now)
        self.assertEqual(closed["exit_price_ms"],triggered.end+1)
        self.assertEqual(closed["exit_price_source"],"completed_bar_trigger")
        self.assertEqual(closed["exit_fx"]["asof_ms"],now)
        validate(self.store.snapshot())


if __name__=="__main__": unittest.main()
