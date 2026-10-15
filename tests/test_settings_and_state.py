"""Current settings contract and the two-namespace neural/positions store."""
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from adaptive_crypto.core import DataError, Rules, load_settings
from adaptive_crypto.persistence import SQLiteDatabase
from adaptive_crypto.settings_editor import field_groups
from adaptive_crypto.state_paths import StatePaths, open_stores, validate_documents

from tests.neural_fixtures import ASSETS, RULES


class SettingsContractTests(unittest.TestCase):
    def test_only_parente_and_perky_fields_are_exposed(self):
        self.assertEqual(Rules().validate().nn_model_id, "parente_mlp_v1")
        groups = field_groups({"strategy": asdict(Rules())})
        keys = {field["key"] for group in groups.values() for field in group}
        self.assertIn("perky_min_score", keys)
        self.assertIn("perky_sweep_len", keys)
        self.assertIn("perky_stop_mode", keys)
        self.assertIn("nn_limitations", keys)
        self.assertIn("smc_gex_alignment_bps", keys)
        for key in ("nn_model_id", "nn_model_path", "smc_entry_method", "momentum_rsi_period"):
            self.assertNotIn(key, keys)
        for model in ("unsupported_model", "unknown_model", ""):
            with self.subTest(model=model), self.assertRaises(DataError):
                Rules(nn_model_id=model).validate()
        self.assertNotIn("nn_model_bundle_path", Rules.__dataclass_fields__)

    def test_load_settings_allows_pair_syntax_and_preserves_original_nn(self):
        doc = {"assets": [{"name": "BTC", "symbol": "BTC/USDC", "enabled": True, "price_decimals": 2}],
               "strategy": asdict(Rules()), "refresh_seconds": 15}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps(doc))
            assets, rules, refresh = load_settings(path)
            self.assertEqual(assets, {"BTC": {"symbol": "BTC/USDC", "price_decimals": 2}})
            self.assertEqual(rules.nn_model_id, "parente_mlp_v1")
            self.assertEqual(refresh, 15)
            for symbol in ("BTC/USDT", "BTC/EUR", "BTC/FDUSD"):
                doc["assets"][0]["symbol"] = symbol
                path.write_text(json.dumps(doc))
                assets, rules, _ = load_settings(path)
                self.assertEqual(assets["BTC"]["symbol"], symbol)
                self.assertEqual(rules.nn_model_id, "parente_mlp_v1")
                self.assertEqual(rules.market_data_source, "deribit_first")


class TwoNamespaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "study.json"

    def test_sources_are_neural_and_positions(self):
        self.assertEqual(set(StatePaths(self.base).sources), {"neural", "positions"})

    def test_old_namespaces_are_rejected(self):
        for name in ("unknown", "foreign", "unsupported"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(DataError, "Unknown state namespace"):
                    validate_documents({name: {}}, {name: {}})

    def test_sqlite_round_trip_preserves_two_namespaces(self):
        with open_stores(self.base, ASSETS, RULES, "sqlite") as (paper, positions):
            self.assertEqual(paper.database_path, positions.database_path)
            positions.open_position(ASSETS, "BTC", "long", 100, 1, "1" * 32, 1)
        paths = StatePaths(self.base)
        self.assertTrue(paths.database.exists())
        with SQLiteDatabase(paths.database) as db:
            self.assertEqual(set(db.documents()), {"neural", "positions"})
        with open_stores(self.base, ASSETS, RULES, "sqlite") as (paper, positions):
            self.assertEqual(len(positions.snapshot()["positions"]), 1)
            self.assertEqual(positions.snapshot()["positions"][0]["entry"], 100)

    def test_json_sources_and_auto_selection(self):
        with open_stores(self.base, ASSETS, RULES, "json") as (paper, positions):
            positions.open_position(ASSETS, "BTC", "long", 100, 1, "1" * 32, 1)
        paths = StatePaths(self.base)
        self.assertTrue(paths.sources["neural"].exists())
        self.assertTrue(paths.sources["positions"].exists())
        self.assertEqual(paths.select("auto"), "json")


if __name__ == "__main__":
    unittest.main()
