"""Exchange pair configuration stays separate from the frozen model input."""
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from dataclasses import asdict

from adaptive_crypto.core import DataError, Rules, load_settings, normalise_pair, validate_feed_pair
from adaptive_crypto.positions import PositionStore, validate_positions
from adaptive_crypto.settings_editor import field_groups, read_editor, save_editor, validate_document


class MarketSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "settings.json"
        self.document = {
            "assets": [{"name": "BTC", "symbol": "BTC/USDC", "enabled": True, "price_decimals": 2}],
            "strategy": asdict(Rules()), "refresh_seconds": 15,
        }
        self.path.write_text(json.dumps(self.document), encoding="utf-8")

    def test_explicit_pairs_and_common_compact_quotes(self):
        cases = {" btc / eur ": "BTC/EUR", "xrp_usdc": "XRP/USDC", "ETH-BTC": "ETH/BTC",
                 "BTCFDUSD": "BTC/FDUSD", "SOLEUR": "SOL/EUR", "ETHBTC": "ETH/BTC",
                 "1000PEPE/USDC": "1000PEPE/USDC", "ASSET/NEWQUOTE": "ASSET/NEWQUOTE",
                 "币安人生/usdc": "币安人生/USDC", "牛来/USDC": "牛来/USDC"}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalise_pair(value), expected)
                self.assertEqual(validate_feed_pair(expected), expected)
        for value in (None, 123, {}, "BTC", "BTC/BTC", "BTC//EUR", "BTC/EUR;DROP", "BTC/", "€/EUR"):
            with self.subTest(value=value), self.assertRaises(DataError):
                normalise_pair(value)

    def test_mixed_quote_pairs_are_preserved_for_each_enabled_asset(self):
        self.document["assets"].extend([
            {"name": "ETH", "symbol": "ETH/BTC", "enabled": True, "price_decimals": 8},
            {"name": "SOL", "symbol": "SOL/EUR", "enabled": True, "price_decimals": 4},
            {"name": "XRP", "symbol": "XRP/USDT", "enabled": False, "price_decimals": 4},
        ])
        assets, rules, _ = validate_document(self.document, self.root)
        self.assertEqual({name: row["symbol"] for name, row in assets.items()}, {
            "BTC": "BTC/USDC", "ETH": "ETH/BTC", "SOL": "SOL/EUR",
        })
        self.assertEqual(rules.nn_model_id, "parente_mlp_v1")
        self.path.write_text(json.dumps(self.document), encoding="utf-8")
        self.assertEqual(load_settings(self.path)[0], assets)

    def test_source_control_has_only_supported_preferences(self):
        fields = field_groups({"strategy": asdict(Rules())})["Market data"]
        self.assertEqual(fields[0]["key"], "market_data_source")
        self.assertEqual({choice[0] for choice in fields[0]["choices"]}, {"deribit_first", "binance"})
        self.assertEqual(Rules(market_data_source="binance").validate().market_data_source, "binance")
        for value in ("kraken", "deribit", "", True):
            with self.subTest(value=value), self.assertRaisesRegex(DataError, "Invalid market_data_source"):
                Rules(market_data_source=value).validate()

    def runtime(self, validate_pair):
        return SimpleNamespace(settings_path=self.path, lock=threading.RLock(), stop=threading.Event(),
                               provider=SimpleNamespace(validate_pair=validate_pair))

    def test_save_validates_catalog_for_new_preference_without_mutating_provider(self):
        validator = Mock()
        runtime = self.runtime(validator)
        _, revision = read_editor(runtime)
        self.document["strategy"]["market_data_source"] = "binance"
        self.document["assets"][0]["symbol"] = "BTC/EUR"
        self.document["assets"].append({"name": "XRP", "symbol": "XRP/EUR", "enabled": False, "price_decimals": 4})
        with patch("adaptive_crypto.settings_editor.backup_runtime", return_value=self.root / "backup.zip"):
            result = save_editor(runtime, self.document, revision)
        validator.assert_called_once_with("BTC/EUR", preference="binance")
        self.assertNotEqual(result["revision"], revision)
        assets, rules, _ = load_settings(self.path)
        self.assertEqual(assets["BTC"]["symbol"], "BTC/EUR")
        self.assertEqual(rules.nn_model_id, "parente_mlp_v1")
        self.assertEqual(rules.market_data_source, "binance")

    def test_unavailable_pair_never_writes_or_backs_up_settings(self):
        runtime = self.runtime(Mock(side_effect=DataError("BTC/EUR is unavailable")))
        _, revision = read_editor(runtime)
        raw = self.path.read_bytes()
        self.document["assets"][0]["symbol"] = "BTC/EUR"
        with patch("adaptive_crypto.settings_editor.backup_runtime") as backup:
            with self.assertRaisesRegex(DataError, "unavailable"):
                save_editor(runtime, self.document, revision)
            backup.assert_not_called()
        self.assertEqual(self.path.read_bytes(), raw)

    def test_canonical_non_dollar_holdings_and_watches_round_trip(self):
        assets = {"ETH": {"symbol": "ETH/BTC", "price_decimals": 8}}
        store = PositionStore(self.root / "positions.json")
        position, _ = store.open_position(assets, "ETH", "long", 0.04, 1, "a" * 32, 1)
        store.open_buy_watch(assets, "ETH", 0.04, 0.035, 1, "b" * 32, 2)
        document = store.snapshot()
        validate_positions(document)
        loaded = PositionStore(store.path).snapshot()
        self.assertEqual(loaded["positions"][0]["symbol"], "ETH/BTC")
        self.assertEqual(loaded["buy_watches"][0]["symbol"], "ETH/BTC")
        invalid = copy.deepcopy(document)
        invalid["positions"][0]["asset"] = "BTC"
        with self.assertRaisesRegex(DataError, "Invalid position asset"):
            validate_positions(invalid)
        invalid = copy.deepcopy(document)
        invalid["buy_watches"][0]["asset"] = "BTC"
        with self.assertRaisesRegex(DataError, "Invalid buy watch type"):
            validate_positions(invalid)

    def test_restart_rejects_changed_holding_or_watch_pair_before_stopping(self):
        from adaptive_crypto.state_paths import open_stores
        from adaptive_crypto.runtime import DashboardRuntime
        from adaptive_crypto.web import create_app
        from tests.test_deribit_runtime import Spot, NOW
        assets = {"BTC": {"symbol": "BTC/USDC", "price_decimals": 2}}
        rules = Rules(paper_floor=0)
        for kind in ("holding", "watch", "matching"):
            with self.subTest(kind=kind):
                self.document["assets"][0]["symbol"] = "BTC/USDC"
                self.document["strategy"] = asdict(rules)
                self.path.write_text(json.dumps(self.document), encoding="utf-8")
                with open_stores(self.root / (kind + ".json"), assets, rules, "sqlite", settings_path=self.path) as (paper, positions):
                    runtime = DashboardRuntime(assets, rules, paper, provider=Spot(), position_store=positions,
                        settings_path=self.path, gex_provider=lambda *args: {})
                    if kind in {"holding", "matching"}:
                        positions.open_position(assets, "BTC", "long", 100, 1, "a" * 32, NOW)
                    else:
                        positions.open_buy_watch(assets, "BTC", 100, 95, 1, "b" * 32, NOW)
                    if kind != "matching":
                        self.document["assets"][0]["symbol"] = "BTC/USD"
                        self.path.write_text(json.dumps(self.document), encoding="utf-8")
                    restart = Mock()
                    client = create_app(runtime, restart_callback=restart).test_client()
                    with client.session_transaction() as session:
                        session["generation"] = runtime.generation
                        session["csrf_token"] = "test-token"
                    response = client.post("/api/dashboard/restart", json={"csrf_token": "test-token"})
                    if kind == "matching":
                        self.assertEqual(response.status_code, 202, response.json)
                        restart.assert_called_once()
                        self.assertTrue(runtime.stop.is_set())
                    else:
                        self.assertEqual(response.status_code, 400, response.json)
                        self.assertIn("Keep the recorded holding or watch pair enabled", response.json["error"])
                        restart.assert_not_called()
                        self.assertFalse(runtime.stop.is_set())
                        self.assertEqual(runtime.assets, assets)
                        self.assertEqual(paper.snapshot()["assets"]["BTC"]["symbol"], "BTC/USDC")


if __name__ == "__main__":
    unittest.main()
