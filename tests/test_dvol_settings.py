"""Independent DVOL persistence and real Deribit pair/index requirements."""
import json
from pathlib import Path
import tempfile
import unittest

from adaptive_crypto.core import DataError
from adaptive_crypto.dvol_settings import DVOLSettings, validate_document


class Client:
    def __init__(self): self.calls=[];self.names=["btc_usdc","btc_usd","eth_usdc","eth_usd","btc_usdt","sol_usdc"];self.error=False
    def request(self, method, params):
        self.calls.append((method,params))
        if self.error: raise DataError("Deribit unavailable")
        if method=="public/get_index_price_names": return self.names
        if method=="public/get_index_price": return {"index_price":12345.}
        raise AssertionError(method)


class DVOLSettingsTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.path=Path(folder.name)/"dvol-settings.json"
        self.client=Client()
        self.markets={"BTC/USDC","ETH/USDC","BTC/USD","ETH/USD"}
        self.store=DVOLSettings(self.path,self.client,lambda pair:pair in self.markets,wall_clock=lambda:1800000000.)

    def test_defaults_independent_and_not_written_until_save(self):
        document,revision=self.store.read()
        self.assertEqual([row["symbol"] for row in document["pairs"]],["BTC/USDC","ETH/USDC"])
        self.assertFalse(self.path.exists())
        document["pairs"].clear()
        self.assertEqual(len(self.store.read()[0]["pairs"]),2)

    def test_available_pairs_require_both_market_and_index(self):
        self.markets.add("BTC/EUR")
        self.assertEqual(self.store.available_pairs(),["BTC/USD","BTC/USDC","ETH/USD","ETH/USDC"])
        self.assertNotIn("BTC/USDT",self.store.available_pairs())
        self.assertEqual(len(self.client.calls),1)

    def test_save_normalizes_and_allows_independent_quote_currencies(self):
        _,revision=self.store.read()
        result=self.store.save({"pairs":[{"symbol":"btc/usd","enabled":True},{"symbol":"ETH/USDC","enabled":False}]},revision)
        self.assertEqual(result["settings"]["pairs"][0]["symbol"],"BTC/USD")
        self.assertEqual(self.store.enabled_pairs()[0],["BTC/USD"])
        self.assertEqual(json.loads(self.path.read_text()),result["settings"])
        self.assertNotEqual(revision,result["revision"])

    def test_market_or_index_absence_cannot_be_saved(self):
        _,revision=self.store.read()
        for pair in ("BTC/USDT","BTC/EUR"):
            with self.subTest(pair=pair),self.assertRaises(DataError):
                self.store.save({"pairs":[{"symbol":pair,"enabled":True}]},revision)
        self.assertFalse(self.path.exists())

    def test_no_sol_duplicate_malformed_or_unknown_settings(self):
        bad=({"pairs":[{"symbol":"SOL/USDC","enabled":True}]},
             {"pairs":[{"symbol":"BTC/USDC","enabled":"yes"}]},
             {"pairs":[{"symbol":"BTC/USDC","enabled":True}]*2},
             {"pairs":[{"symbol":"BTC//USDC","enabled":True}]},
             {"pairs":[],"assets":[]})
        for document in bad:
            with self.subTest(document=document),self.assertRaises(DataError): validate_document(document)

    def test_revision_conflict_preserves_file(self):
        _,revision=self.store.read()
        first=self.store.save({"pairs":[]},revision)
        with self.assertRaises(DataError): self.store.save({"pairs":[{"symbol":"BTC/USDC","enabled":True}]},revision)
        self.assertEqual(self.store.read()[0],first["settings"])

    def test_unavailable_catalog_never_accepts_pair(self):
        self.client.error=True
        with self.assertRaises(DataError): self.store.save({"pairs":[{"symbol":"BTC/USDC","enabled":True}]},self.store.read()[1])
        self.assertFalse(self.path.exists())

    def test_reference_price_is_deribit_index_for_dvol_pair(self):
        result=self.store.quote("BTC/USD")
        self.assertEqual(result["symbol"],"BTC/USD")
        self.assertEqual(result["source"],"Deribit composite index")
        self.assertEqual(result["bid"],12345.)
        self.assertEqual(self.client.calls[-1],("public/get_index_price",{"index_name":"btc_usd"}))
        with self.assertRaises(DataError): self.store.quote("BTC/USDT")

    def test_remove_every_pair_works_during_catalog_outage(self):
        self.client.error=True
        result=self.store.save({"pairs":[]},self.store.read()[1])
        self.assertEqual(result["settings"],{"pairs":[]})
        self.assertEqual(self.client.calls,[])

    def test_invalid_index_payload_rejected(self):
        for names in ({"index_names":["btc_usdc"]},[123],[]):
            with self.subTest(names=names):
                self.store._indices=None;self.client.names=names
                with self.assertRaises(DataError): self.store.available_pairs()


if __name__=="__main__": unittest.main()
