"""Native candle units, causal 4H aggregation and shared feed failures."""
import unittest
from adaptive_crypto.core import H4, M5, DataError
from adaptive_crypto.deribit_market_data import DeribitMarketData, parse_candles, aggregate_four_hours
HOUR=3600000
NOW=1800000000000//H4*H4+1000

class Client:
    def __init__(self): self.calls=[]; self.drop=False; self.forming=False; self.now=NOW
    def request(self, method, params):
        self.calls.append((method,dict(params)))
        if method=="public/get_time":return self.now
        if method=="public/get_index_price_names":return ["btc_usdc","eth_usdc","sol_usdc"]
        if method=="public/get_instruments":
            if params["kind"] == "spot": return []
            return [{"base_currency":base,"quote_currency":"USDC","instrument_name":base+"_USDC-PERPETUAL",
                     "kind":"future","settlement_period":"perpetual","is_active":True,"state":"open",
                     "price_index":base.lower()+"_usdc"} for base in ("BTC","ETH","SOL")]
        if method=="public/get_index_price":return {"index_price":103.}
        unit=int(params["resolution"])*60000
        ticks=list(range(params["start_timestamp"],params["end_timestamp"]+1,unit))
        if self.drop:ticks=ticks[:3]+ticks[4:]
        if self.forming:ticks.append(params["end_timestamp"]+1)
        return {"status":"ok","ticks":ticks,"open":[100.]*len(ticks),"high":[105.]*len(ticks),
                "low":[98.]*len(ticks),"close":[103.]*len(ticks),"volume":[2.]*len(ticks)}

class FeedTests(unittest.TestCase):
    def test_four_hours_is_four_completed_native_hour_bars(self):
        client=Client();feed=DeribitMarketData(client)
        end=NOW//H4*H4;bars=feed.history("BTC/USDC",H4,end-2*H4,end)
        self.assertEqual(len(bars),2)
        self.assertEqual((bars[-1].o,bars[-1].h,bars[-1].l,bars[-1].c,bars[-1].v),(100.,105.,98.,103.,8.))
        self.assertEqual(bars[-1].end,end-1)
        self.assertTrue(all(p["resolution"]=="60" for m,p in client.calls if m=="public/get_tradingview_chart_data"))

    def test_gap_never_interpolated_and_forming_bar_excluded(self):
        client=Client();feed=DeribitMarketData(client);end=NOW//H4*H4
        client.drop=True
        with self.assertRaisesRegex(DataError,"gap"):feed.history("ETH/USDC",H4,end-2*H4,end)
        client.drop=False;client.forming=True
        self.assertEqual(len(feed.history("ETH/USDC",H4,end-2*H4,end)),2)

    def test_history_pages_do_not_double_count_boundary_bars(self):
        client=Client();feed=DeribitMarketData(client);end=NOW//H4*H4
        bars=feed.history("SOL/USDC",H4,end-420*H4,end)
        self.assertEqual(len(bars),420);self.assertEqual(sum(b.v for b in bars),420*8.)
        calls=[p for m,p in client.calls if m=="public/get_tradingview_chart_data"]
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0]["end_timestamp"]+1,calls[1]["start_timestamp"])

    def test_candle_cache_and_source_use_the_same_pair(self):
        client=Client();feed=DeribitMarketData(client)
        first=feed.candles("BTC/USDC",H4,NOW);n=len(client.calls)
        self.assertEqual(first,feed.chart_candles("BTC/USDC",H4,NOW+1000))
        self.assertEqual(len(client.calls),n)
        self.assertEqual(feed.source_metadata("BTC/USDC",H4)["instrument"],"BTC_USDC-PERPETUAL")
        client.now=NOW+H4; feed.clock_sample=None
        later=feed.candles("BTC/USDC",H4,NOW+H4)
        self.assertEqual(later[-1].t,first[-1].t+H4)
        self.assertEqual(len(later),420)

    def test_index_quote_is_explicitly_nonexecutable_and_has_no_observed_spread(self):
        q=DeribitMarketData(Client()).quotes(["BTC/USDC"])["BTC/USDC"]
        self.assertEqual(q["bid"],q["ask"]);self.assertFalse(q["executable"])
        self.assertFalse(q["spread_observed"]);self.assertEqual(q["symbol"],"BTC/USDC")
        from adaptive_crypto.take_profit_alerts import fresh_quote
        checked=fresh_quote(q,NOW)
        self.assertEqual(checked["symbol"],"BTC/USDC")
        self.assertFalse(checked["executable"])
        self.assertEqual(checked["quote_kind"],"index_reference")

    def test_unsupported_interval_fails_before_io_and_pairs_use_catalog(self):
        client=Client();feed=DeribitMarketData(client)
        with self.assertRaises(DataError):feed.history("BTC/USDC",12345,0,NOW)
        self.assertEqual(client.calls,[])
        for pair in ("BTC/USDT","SOL/USD","DOGE/USDC"):
            with self.subTest(pair=pair),self.assertRaises(DataError):feed.validate_pair(pair)
        self.assertEqual(len(client.calls),2)

    def test_exact_active_spot_is_selected_when_its_perpetual_is_halted(self):
        class MixedCatalog(Client):
            def request(self, method, params):
                if method == "public/get_instruments":
                    kind = params["kind"]
                    return [{"base_currency": "ETH", "quote_currency": "USDC",
                             "instrument_name": "ETH_USDC-PERPETUAL" if kind == "future" else "ETH_USDC",
                             "kind": kind, "settlement_period": "perpetual" if kind == "future" else "spot",
                             "is_active": kind == "spot", "state": "open" if kind == "spot" else "closed",
                             "price_index": "eth_usdc"}]
                return super().request(method, params)
        client = MixedCatalog(); feed = DeribitMarketData(client)
        self.assertEqual(feed.available_pairs(), ["ETH/USDC"])
        self.assertEqual(feed.source_metadata("ETH/USDC", M5)["instrument"], "ETH_USDC")
        feed.history("ETH/USDC", M5, NOW//M5*M5-M5, NOW//M5*M5)
        self.assertEqual([params["instrument_name"] for method, params in client.calls
                          if method == "public/get_tradingview_chart_data"], ["ETH_USDC"])

    def test_halted_catalog_markets_remain_supported_without_permitting_fallback(self):
        class Halted(Client):
            def request(self, method, params):
                result = super().request(method, params)
                if method == "public/get_instruments":
                    for row in result:
                        row.update(is_active=False, state="closed")
                return result
        feed = DeribitMarketData(Halted())
        self.assertTrue(feed.supports_pair("SOL/USDC"))
        self.assertNotIn("SOL/USDC", feed.available_pairs())
        with self.assertRaisesRegex(DataError, "halted"):
            feed.validate_pair("SOL/USDC")

if __name__=="__main__":unittest.main()
