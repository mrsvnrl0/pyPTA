"""Execution rejects changed, forged or malformed Parente confirmations."""
import copy
from pathlib import Path
import tempfile
import unittest

from adaptive_crypto.core import H4, M5
from tests.currency_fixtures import fx
from adaptive_crypto.neural_engine import NeuralEngine
from adaptive_crypto.neural_ledger import NeuralStore, validate
from tests.neural_fixtures import (ASSETS, NOW, RULES, FakeModel, bars,
                                   qualified_market, quote)


class BinanceFixtureFeed:
    """Deterministic completed candles and explicit trained input provenance."""
    source_key = "binance-spot-usdt-4h-fixture"

    def __init__(self):
        self.source = {"venue": "Binance", "market": "spot", "pair": "BTC/USDT",
                       "symbol": "BTC/USDT", "quote_asset": "USDT", "volume": "base_asset",
                       "interval_ms": H4, "completed_only": True,
                       "source_key": self.source_key}

    def candles(self, asset, now):
        return bars(now), copy.deepcopy(self.source)


class ProbabilityModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.probabilities = {"BUY": .8, "HOLD": .1, "SELL": .1}

    def predict(self, candles, asset):
        signal = super().predict(candles, asset)
        signal["probabilities"] = copy.deepcopy(self.probabilities)
        return signal


class NeuralExecutionGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.case_number = 0
        self.fresh_state()

    def fresh_state(self, label="BUY", *, feature_provider=None, model=None):
        self.case_number += 1
        self.path = Path(self.temp.name) / (str(self.case_number) + ".neural.json")
        self.store = NeuralStore(self.path, ASSETS, RULES)
        self.model = model or FakeModel(label)
        self.engine = NeuralEngine(ASSETS, RULES, self.store, model=self.model,
                                   feature_provider=feature_provider)

    def evaluate(self, now=NOW, *, price=100., reading=None, smc=None):
        return self.engine.evaluate("BTC", quote(now, price), bars(now),
                                    bars(now, interval=M5), now,
                                    smc_market=smc, neural_reading=reading, fx_quote=fx(now))

    def record(self):
        return self.store.snapshot()["assets"]["BTC"]

    def assert_rejected(self, row):
        self.assertIsNone(row["neural"]["signal"])
        self.assertTrue(row["neural"].get("error"), row["neural"])

    def test_persisted_buy_and_hold_cannot_flip_after_cache_clear_or_restart(self):
        for baseline in ("BUY", "HOLD"):
            for restart in (False, True):
                with self.subTest(baseline=baseline, restart=restart):
                    self.fresh_state(baseline)
                    self.evaluate()
                    accepted = copy.deepcopy(self.record()["last_signal"])
                    self.model.label = "SELL" if baseline == "BUY" else "BUY"
                    if restart:
                        self.store = NeuralStore(self.path, ASSETS, RULES)
                        self.engine = NeuralEngine(ASSETS, RULES, self.store, model=self.model)
                    else:
                        self.engine.cache.clear()
                    reading = self.engine.read("BTC", bars(NOW + 2000), NOW + 2000)
                    self.assertIsNone(reading["signal"])
                    self.assertTrue(reading["error"])
                    row = self.evaluate(NOW + 2000, smc=qualified_market(NOW + 2000))
                    self.assert_rejected(row)
                    self.assertEqual(self.record()["last_signal"], accepted)
                    self.assertEqual(self.record()["trades"], [])
                    validate(self.store.snapshot())

    def test_same_label_probability_change_above_tolerance_is_rejected(self):
        for supplied in (False, True):
            with self.subTest(supplied=supplied):
                self.fresh_state(model=ProbabilityModel())
                self.evaluate()
                accepted = copy.deepcopy(self.record()["last_signal"])
                self.model.probabilities["BUY"] += 2e-8
                self.model.probabilities["HOLD"] -= 2e-8
                if supplied:
                    reading = self.engine.read("BTC", bars(), NOW)
                    reading["signal"]["probabilities"] = copy.deepcopy(self.model.probabilities)
                else:
                    self.engine.cache.clear()
                    reading = self.engine.read("BTC", bars(), NOW)
                    self.assertIsNone(reading["signal"])
                    self.assertTrue(reading["error"])
                row = self.evaluate(reading=reading, smc=qualified_market())
                self.assert_rejected(row)
                self.assertEqual(self.record()["last_signal"], accepted)
                self.assertEqual(self.record()["trades"], [])

    def test_newer_completed_sell_closes_and_copies_exit_decision(self):
        self.evaluate(smc=qualified_market())
        self.model.label = "SELL"
        later = NOW + H4
        reading = self.engine.read("BTC", bars(later), later)
        expected = copy.deepcopy(reading["signal"])
        row = self.evaluate(later, price=104., reading=reading)
        trade = self.record()["trades"][0]
        self.assertEqual(trade["status"], "sold")
        self.assertEqual(trade["exit_reason"], "Parente SELL")
        self.assertEqual(trade["exit_decision"], expected)
        self.assertGreater(trade["exit_decision"]["signal_end"], trade["signal_end"])
        self.assertEqual(row["neural"]["signal"], expected)
        reading["signal"]["probabilities"]["SELL"] = 0.
        self.assertEqual(self.record()["trades"][0]["exit_decision"], expected)
        validate(self.store.snapshot())

    def test_wrong_signal_or_reading_model_identity_cannot_confirm_entry(self):
        for location in ("signal", "reading"):
            with self.subTest(location=location):
                self.fresh_state()
                reading = self.engine.read("BTC", bars(), NOW)
                (reading["signal"] if location == "signal" else reading)["model_id"] = "b" * 64
                row = self.evaluate(reading=reading, smc=qualified_market())
                self.assert_rejected(row)
                self.assertEqual(self.record()["trades"], [])
                self.assertNotIn("last_signal", self.record())

    def test_malformed_probabilities_argmax_candle_and_expiry_cannot_confirm_entry(self):
        def candle(reading, end):
            reading["signal"]["signal_end"] = end
            reading["expires_ms"] = end + H4 + 1
            reading["latest_4h_ms"] = end + 1 - H4
            reading["latest_decision_ms"] = end + 1 - H4

        changes = {
            "probability_keys": lambda r: r["signal"]["probabilities"].pop("SELL"),
            "probability_list": lambda r: r["signal"].update(probabilities=[.8, .1, .1]),
            "probability_nan": lambda r: r["signal"]["probabilities"].update(BUY=float("nan")),
            "probability_infinite": lambda r: r["signal"]["probabilities"].update(BUY=float("inf")),
            "probability_negative": lambda r: r["signal"]["probabilities"].update(BUY=1.1, HOLD=-.2),
            "probability_sum": lambda r: r["signal"]["probabilities"].update(BUY=.7),
            "argmax": lambda r: r["signal"].update(label="HOLD"),
            "unknown_label": lambda r: r["signal"].update(label="WAIT"),
            "candle_float": lambda r: candle(r, float(NOW // H4 * H4 - 1)),
            "candle_bool": lambda r: candle(r, True),
            "candle_misaligned": lambda r: candle(r, NOW // H4 * H4 - 2),
            "candle_stale": lambda r: candle(r, NOW // H4 * H4 - H4 - 1),
            "candle_future": lambda r: candle(r, NOW // H4 * H4 + H4 - 1),
            "expiry_float": lambda r: r.update(expires_ms=float(r["expires_ms"])),
            "expiry_inconsistent": lambda r: r.update(expires_ms=r["expires_ms"] + 1),
            "expiry_elapsed": lambda r: r.update(expires_ms=NOW),
        }
        for name, mutate in changes.items():
            with self.subTest(defect=name):
                self.fresh_state()
                reading = self.engine.read("BTC", bars(), NOW)
                mutate(reading)
                row = self.evaluate(reading=reading, smc=qualified_market())
                self.assert_rejected(row)
                self.assertEqual(self.record()["trades"], [])
                self.assertNotIn("last_signal", self.record())

    def test_feature_feed_rejects_malformed_or_wrong_binance_source(self):
        sources = [None, [], {},
                   {"venue": 1}, {"venue": "Kraken"}, {"market": "futures"},
                   {"pair": "ETH/USDT", "symbol": "ETH/USDT"},
                   {"volume": "quote_asset"}, {"interval_ms": M5},
                   {"completed_only": False}]
        for source in sources:
            with self.subTest(source=source):
                feed = BinanceFixtureFeed()
                if isinstance(source, dict):
                    feed.source = {**feed.source, **source} if source else source
                else:
                    feed.source = source
                self.fresh_state(feature_provider=feed)
                reading = self.engine.read("BTC", bars(), NOW)
                self.assertIsNone(reading["signal"])
                self.assertTrue(reading["error"])
                row = self.evaluate(reading=reading, smc=qualified_market())
                self.assert_rejected(row)
                self.assertEqual(self.record()["trades"], [])

    def test_supplied_reading_rejects_malformed_or_wrong_binance_source(self):
        for changed in (None, [], {}, {"venue": 1}, {"venue": "Kraken"},
                        {"pair": "ETH/USDT", "symbol": "ETH/USDT"},
                        {"volume": "quote_asset"}, {"completed_only": False}):
            with self.subTest(source=changed):
                self.fresh_state(feature_provider=BinanceFixtureFeed())
                reading = self.engine.read("BTC", bars(), NOW)
                source = reading["input_source"]
                invalid = {**source, **changed} if isinstance(changed, dict) and changed else changed
                reading["input_source"] = copy.deepcopy(invalid)
                reading["signal"]["input_source"] = copy.deepcopy(invalid)
                row = self.evaluate(reading=reading, smc=qualified_market())
                self.assert_rejected(row)
                self.assertEqual(self.record()["trades"], [])

    def test_nested_source_cannot_bypass_current_source_and_persisted_hold(self):
        for nested in (None, "missing", {"venue": "Kraken"}):
            with self.subTest(nested=nested):
                self.fresh_state("HOLD", feature_provider=BinanceFixtureFeed())
                self.evaluate()
                accepted = copy.deepcopy(self.record()["last_signal"])
                reading = self.engine.read("BTC", bars(), NOW)
                reading["signal"].update(label="BUY", probabilities={"BUY": .8, "HOLD": .1, "SELL": .1})
                if nested == "missing":
                    reading["signal"].pop("input_source")
                elif isinstance(nested, dict):
                    reading["signal"]["input_source"].update(nested)
                else:
                    reading["signal"]["input_source"] = nested
                row = self.evaluate(reading=reading, smc=qualified_market())
                self.assert_rejected(row)
                self.assertEqual(self.record()["last_signal"], accepted)
                self.assertEqual(self.record()["trades"], [])

    def test_malformed_persisted_probability_does_not_block_saved_stop(self):
        self.evaluate(smc=qualified_market())
        self.store.transaction(lambda d: d["assets"]["BTC"]["last_signal"]["probabilities"].update(BUY=10**1000))
        row = self.evaluate(NOW+2000, price=80.)
        self.assert_rejected(row)
        trade = self.record()["trades"][0]
        self.assertEqual(trade["status"], "stopped")
        self.assertEqual(trade["exit_reason"], "stop")
        validate(self.store.snapshot())

    def test_saved_smc_stop_and_target_close_despite_malformed_neural_reading(self):
        for price, reason in ((80., "stop"), (111., "target")):
            with self.subTest(reason=reason):
                self.fresh_state()
                self.evaluate(smc=qualified_market())
                reading = self.engine.read("BTC", bars(), NOW)
                reading["signal"]["probabilities"]["BUY"] = .7
                row = self.evaluate(NOW + 2000, price=price, reading=reading)
                self.assert_rejected(row)
                trade = self.record()["trades"][0]
                self.assertEqual(trade["exit_reason"], reason)
                self.assertNotIn("exit_decision", trade)
                self.assertEqual((trade["stop"], trade["target"]), (90., 110.))
                validate(self.store.snapshot())

    def test_loaded_model_identity_is_part_of_completed_candle_cache_identity(self):
        first = self.engine.read("BTC", bars(), NOW)
        self.assertEqual(self.model.calls, 1)
        other = FakeModel("HOLD")
        other.identity = "b" * 64
        self.engine.model = other
        second = self.engine.read("BTC", bars(), NOW)
        self.assertIsNone(second["error"])
        self.assertEqual(other.calls, 1)
        self.assertEqual(second["signal"]["model_id"], other.identity)
        self.assertEqual(second["signal"]["label"], "HOLD")
        self.assertNotEqual(first["signal"]["model_id"], second["signal"]["model_id"])
        self.engine.read("BTC", bars(), NOW)
        self.assertEqual(other.calls, 1)


if __name__ == "__main__":
    unittest.main()
