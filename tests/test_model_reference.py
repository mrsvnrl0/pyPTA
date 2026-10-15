"""The fixed Parente 5/2 artifact, its shared reader, and its input contract."""
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from adaptive_crypto.core import DataError, H4, M5, Rules
from adaptive_crypto.neural import DEFAULT_MODEL, NeuralModel
from adaptive_crypto.neural_input_contract import (
    CandleInputContract,
    PARENTE_BINANCE_USDT,
    normalise_input_source,
)
from adaptive_crypto.neural_models import MODEL_ID, load_selected_model, model_catalog

from tests.neural_fixtures import NUMERIC_AVAILABLE, bars


@unittest.skipUnless(NUMERIC_AVAILABLE, "Install requirements-neural.txt for model tests")
class ParenteArtifactTests(unittest.TestCase):
    def test_artifact_sha_and_shared_reader_agree(self):
        expected = "8907c6f395dc8cf385619f1cace6e27c3b23a04a820fe980cde65898cad56c87"
        self.assertEqual(hashlib.sha256(DEFAULT_MODEL.read_bytes()).hexdigest(),expected)
        model = NeuralModel()
        self.assertEqual(model.identity, expected)
        catalog = model_catalog()
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]["id"], MODEL_ID)
        self.assertEqual(catalog[0]["artifact_id"], expected)
        self.assertTrue(catalog[0]["available"])
        self.assertEqual(load_selected_model(Rules()).identity, expected)

    def test_only_the_fixed_model_id_is_accepted(self):
        with self.assertRaisesRegex(DataError, "only supported neural model"):
            load_selected_model(replace(Rules(), nn_model_id="something_else"))

    def test_model_outputs_finite_probabilities(self):
        model = NeuralModel()
        result = model.predict(bars(), "BTC")
        self.assertAlmostEqual(sum(result["probabilities"].values()), 1., places=6)
        self.assertIn(result["label"], ("BUY", "HOLD", "SELL"))
        self.assertEqual(result["model_id"], model.identity)

    def test_bad_model_metadata_fails_closed(self):
        import numpy as np
        model = NeuralModel()
        with np.load(model.path, allow_pickle=False) as data:
            original = {key: data[key].copy() for key in data.files}
        with tempfile.TemporaryDirectory() as tmp:
            arrays = dict(original)
            metadata = json.loads(str(arrays["metadata"].item()))
            metadata["classes"].reverse()
            arrays["metadata"] = np.array(json.dumps(metadata))
            path = Path(tmp) / "bad.npz"
            np.savez(path, **arrays)
            with self.assertRaises(DataError):
                NeuralModel(path)


class ParenteInputContractTests(unittest.TestCase):
    def test_contract_is_binance_usdt_4h_base_volume(self):
        self.assertEqual(PARENTE_BINANCE_USDT, CandleInputContract("Binance", "USDT", H4))
        metadata = PARENTE_BINANCE_USDT.metadata()
        self.assertEqual(metadata["interval_ms"], H4)
        self.assertEqual(metadata["volume"], "base_asset")
        self.assertEqual(metadata["market"], "spot")

    def test_validate_accepts_exact_contract_and_rejects_any_drift(self):
        good = {"venue": "Binance", "market": "spot", "pair": "BTC/USDT",
                "interval_ms": H4, "volume": "base_asset"}
        PARENTE_BINANCE_USDT.validate(good, "BTC")
        for changes in ({"venue": "Kraken"}, {"pair": "BTC/USD"},
                        {"interval_ms": M5}, {"volume": "quote_asset"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(DataError, "trained candle contract"):
                PARENTE_BINANCE_USDT.validate({**good, **changes}, "BTC")

    def test_normalise_input_source_normalises_and_rejects_incomplete_provenance(self):
        normalised = normalise_input_source(
            {"venue": "Binance", "market": "spot", "volume": "base_asset",
             "pair": "BTCUSDT", "interval_ms": H4, "completed_only": True},
            interval_ms=H4)
        self.assertEqual(normalised["pair"], "BTC/USDT")
        self.assertEqual(normalised["quote_asset"], "USDT")
        self.assertEqual(normalised["interval_ms"], H4)
        for source, fragment in (
            ({}, "missing venue"),
            ({"venue": "Binance"}, "missing market"),
            ({"venue": "Binance", "market": "spot", "volume": "base_asset",
              "pair": "BTC/USDT", "interval_ms": H4, "completed_only": False}, "completed")):
            with self.subTest(source=source), self.assertRaisesRegex(DataError, fragment):
                normalise_input_source(source, interval_ms=H4)


if __name__ == "__main__":
    unittest.main()
