import unittest
from unittest.mock import Mock,patch
from adaptive_crypto.binance_market_data import BinanceParenteFeed,parse_binance_rows
from adaptive_crypto.core import H4,DataError,Rules
from adaptive_crypto.neural_engine import NeuralEngine

NOW=H4*120000+1000

def rows():
 return [[NOW//H4*H4-(100-i)*H4,"100","110","90","105","12.5",NOW//H4*H4-(99-i)*H4-1,"999999",4,"6","700",0] for i in range(100)]

class BinanceFeedTests(unittest.TestCase):
 def test_base_volume_completed_and_contiguous(self):
  bars=parse_binance_rows(rows(),NOW)
  self.assertEqual(bars[-1].v,12.5)
  self.assertEqual(bars[-1].end,NOW//H4*H4-1)
  self.assertEqual(len(bars),100)
 def test_missing_or_invalid_candle_rejected(self):
  for field,value in [(6,0),(5,'NaN'),(0,1),(4,'200')]:
   sample=rows();sample[50][field]=value
   with self.assertRaises(DataError):parse_binance_rows(sample,NOW)
  with self.assertRaises(DataError):parse_binance_rows(rows()[:-1],NOW)
 def test_live_forming_candle_is_not_model_input(self):
  sample=rows();t=NOW//H4*H4;sample.append([t,'100','110','90','105','8',t+H4-1,'999',4,'5','600',0])
  self.assertEqual(len(parse_binance_rows(sample,NOW)),100)
 def test_engine_uses_binance_and_never_falls_back(self):
  model=Mock();model.required_history_bars=100;model.trained_through_ms=0;del model.trained_through_ms_for
  model.identity='test';model.predict.return_value={'label':'HOLD','signal_end':NOW//H4*H4-1,'model_id':model.identity,'probabilities':{'BUY':.1,'HOLD':.8,'SELL':.1}}
  feed=Mock();feed.source_key='binance-spot-usdt-4h-v1';feed.candles.return_value=(parse_binance_rows(rows(),NOW),{'venue':'Binance','market':'spot','volume':'base_asset','pair':'BTC/USDT','interval_ms':H4,'source_key':'binance-spot-usdt-4h-v1'})
  engine=NeuralEngine({'BTC':{'symbol':'BTC/USDT'}},Rules(),None,model=model,feature_provider=feed)
  result=engine.read('BTC',[],NOW)
  self.assertIsNone(result['error']);self.assertEqual(result['signal']['input_source']['venue'],'Binance')
  feed.candles.side_effect=DataError('Binance unavailable')
  result=engine.read('BTC',parse_binance_rows(rows(),NOW),NOW)
  self.assertIsNone(result['signal']);self.assertIn('Binance unavailable',result['error'])
 def test_public_endpoint_redirect_and_error_body_are_sanitized(self):
  session=Mock();session.headers={};session.get.return_value.status_code=302
  feed=BinanceParenteFeed(session)
  with self.assertRaisesRegex(DataError,'public data unavailable'):feed._get('time')
  self.assertFalse(session.get.call_args.kwargs['allow_redirects']);self.assertFalse(session.trust_env)
