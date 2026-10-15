"""Current dashboard routes, retired model endpoints, and HTML nesting."""
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from adaptive_crypto.core import Candle, H4, Rules
from adaptive_crypto.neural_ledger import NeuralStore
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.web import create_app

from tests.neural_fixtures import ASSETS, NOW, FakeModel
from tests.ui_helpers import DashboardHTML


def bars(interval, price=100.):
    end = NOW // interval * interval
    return [Candle(end - (120 - i) * interval, price, price + 1, price - 1, price, 100., interval)
            for i in range(120)]


class Spot:
    candle_source = {"venue": "Deribit", "market": "perpetual", "volume": "base_asset"}

    def __init__(self):
        self.requested = []

    def now(self):
        return NOW

    def candles(self, pair, interval, now):
        self.requested.append((pair, interval))
        return bars(interval)

    def chart_candles(self, pair, interval, now):
        return self.candles(pair, interval, now)

    def source_metadata(self, pair, interval, now=None):
        return {**self.candle_source, "pair": pair, "quote_asset": "USDC", "interval_ms": interval}

    def quotes(self, pairs):
        return {p: {"bid": 100., "ask": 100.01, "last": 100., "asof_ms": NOW,
                    "synthetic": True, "executable": False} for p in pairs}


class Binance:
    def candles(self, asset, now):
        return bars(H4,200.), {"venue":"Binance", "market":"spot", "pair":asset+"/USDT", "quote_asset":"USDT", "volume":"base_asset", "interval_ms":H4, "completed_only":True}

class WebAndUiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.rules = Rules(fee_rate=.001, paper_floor=0)
        self.settings = self.directory / "settings.json"
        self.settings.write_text(json.dumps({
            "assets": [{"name": "BTC", "symbol": "BTC/USDC", "enabled": True, "price_decimals": 2}],
            "strategy": asdict(self.rules), "refresh_seconds": 15}))
        store = NeuralStore(self.directory / "state.neural.json", ASSETS, self.rules)
        self.provider = Spot()
        self.runtime = DashboardRuntime(ASSETS, self.rules, store, provider=self.provider,
                                        settings_path=self.settings, gex_provider=lambda _: {})
        self.runtime.gex.snapshot = lambda _: {"status": "unavailable", "error": "No fixture options"}
        self.runtime.gex.diagnostics = lambda now_ms=None: {"BTC": {"status": "unavailable"}}
        self.model = FakeModel()
        self.runtime.engine.model = self.model
        self.runtime.engine.model_error = None
        self.runtime.neural_feature_provider=Binance();self.runtime.engine.feature_provider=self.runtime.neural_feature_provider
        self.runtime.scan_once()
        self.client = create_app(self.runtime).test_client()

    def test_all_pages_render_with_valid_html(self):
        for route in ("/", "/paper-trading", "/positions", "/settings", "/smc-chart", "/trade-records"):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200, route)
                parsed = DashboardHTML(response.get_data(as_text=True))
                self.assertFalse(parsed.errors, (route, parsed.errors))

    def test_add_manual_position_redirect_and_repeated_reload(self):
        self.assertEqual(self.client.get("/positions").status_code, 200)
        with self.client.session_transaction() as session:
            token = session["csrf_token"]
        response = self.client.post("/api/positions", data={
            "asset": "BTC", "position_type": "spot", "entry": "100",
            "quantity": "1", "stop_price": "95", "request_id": "d" * 32,
            "csrf_token": token}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        position = self.runtime.positions.snapshot()["positions"][0]
        self.assertNotIn("watches", self.runtime.snapshot()["holdings"])
        for _ in range(3):
            response = self.client.get("/positions")
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            parsed = DashboardHTML(html)
            self.assertFalse(parsed.errors, parsed.errors)
            self.assertIn('data-position-id="' + position["id"] + '"', html)
            for text in ("Parente signal for this position", "SMC · TP1",
                         "SMC + GEX · TP2", "SMC structural low", "Save stop"):
                self.assertIn(text, parsed.text)
        saved = self.runtime.positions.snapshot()["positions"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["stop_price"], 95.)

    def test_positions_render_current_guidance_for_all_recorded_types(self):
        for index, kind in enumerate(("spot", "margin_long", "margin_short")):
            self.runtime.positions.open_position(
                ASSETS, "BTC", None, 100., 1., f"{index + 1:032x}", NOW,
                position_type=kind)
        self.runtime.scan_once()
        self.assertNotIn("watches", self.runtime.snapshot()["holdings"])
        response = self.client.get("/positions")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        parsed = DashboardHTML(html)
        self.assertFalse(parsed.errors, parsed.errors)
        self.assertEqual(html.count('class="manual-position"'), 3)
        for text in ("Perky SMC strategy · completed bars", "Structure / NN alerts",
                     "SMC structural low", "SMC structural high"):
            self.assertIn(text, parsed.text)

    def test_settings_exposes_parente_only_controls(self):
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("Parente 5/2", html)
        self.assertNotIn("Self-train candidate", html)
        self.assertNotIn("nn-model-choice", html)

    def test_model_selection_and_candidate_routes_are_absent(self):
        for route in ("/api/models", "/api/models/self-training/status", "/api/models/retired/report",
                      "/api/candidates", "/api/settings/model"):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 404, route)

    def test_selected_pair_drives_chart_and_pine(self):
        chart = self.client.get("/api/chart/BTC")
        self.assertEqual(chart.status_code, 200)
        self.assertEqual(chart.json["symbol"], "BTC/USDC")
        pine = self.client.get("/api/smc-chart/BTC")
        self.assertEqual(pine.status_code, 200)
        self.assertEqual(pine.json["symbol"], "BTC/USDC")


if __name__ == "__main__":
    unittest.main()
