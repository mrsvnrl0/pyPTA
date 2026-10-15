"""Offline checks for the Deribit DVOL volatility-index module and routes."""
import math
import threading
import unittest
import tempfile
from pathlib import Path

from flask import Flask, render_template

from adaptive_crypto.core import DataError
from adaptive_crypto.dvol import (
    DVOL, MOVE_HORIZONS, SUPPORTED_CURRENCIES, expected_move,
    parse_row, register_dvol_routes,
)


NOW = 1_800_000_000_000
ROOT = Path(__file__).resolve().parent.parent


def row(t, c):
    return [t, c - 0.2, c + 0.3, c - 0.4, c]


class FakeClient:
    """Stand-in for the feed worker's DeribitClient.request surface."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.error = None

    def request(self, method, params):
        self.calls.append((method, params))
        if self.error:
            raise DataError(self.error)
        if method == "public/get_index_price_names":
            return ["btc_usdc", "eth_usdc", "btc_usd", "eth_usd", "sol_usdc"]
        if method == "public/get_index_price":
            return {"index_price": 50000.}
        if not self.responses:
            return {"data": [], "continuation": None}
        return self.responses.pop(0)


HOUR = 3600000
NOW = NOW//HOUR*HOUR+1000

def series(now=NOW, hours=168):
    end = now//HOUR*HOUR
    return [row(t, 50.) for t in range(end-hours*HOUR,end+1,HOUR)]

def quote(now=NOW, symbol="BTC/USDC"):
    return {"bid":50000., "ask":50000., "last":50000., "asof_ms":now,
            "symbol":symbol, "source":"Deribit composite index", "quote_kind":"index_reference"}

class FakeRuntime:
    def __init__(self, client):
        self.assets = {"BTC": {"symbol": "BTC/USDC", "price_decimals": 2},
                       "ETH": {"symbol": "ETH/USDC", "price_decimals": 2},
                       "SOL": {"symbol": "SOL/USDC", "price_decimals": 3}}
        self.deribit = client
        self.provider = None
        self.stop = threading.Event()
        self.market = {"BTC": {"quote": quote()}}
        self.lock = threading.RLock()


class ParseTests(unittest.TestCase):
    def test_valid_row(self):
        self.assertEqual(parse_row(row(3600_000, 50.0)),
                         {"t": 3600_000, "o": 49.8, "h": 50.3, "l": 49.6, "c": 50.0})

    def test_rejects_invalid_payload(self):
        for bad in ("nope", None, 1.5, [1, 2], [1, 2, 3, 4, 5, 6]):
            with self.subTest(bad=bad), self.assertRaises(DataError):
                parse_row(bad)

    def test_rejects_invalid_timestamp(self):
        for ts in (True, 1.5, -1, "1"):
            with self.subTest(ts=ts), self.assertRaises(DataError):
                parse_row([ts, 50.0, 51.0, 49.0, 50.0])

    def test_rejects_non_finite_and_negative_values(self):
        for value in (float("nan"), float("inf"), -1.0, True, "high"):
            with self.subTest(value=value), self.assertRaises(DataError):
                parse_row([0, value, 51.0, 49.0, 50.0])

    def test_rejects_inconsistent_ohlc(self):
        with self.assertRaises(DataError):
            parse_row([0, 50.0, 49.0, 49.0, 50.0])
        with self.assertRaises(DataError):
            parse_row([0, 50.0, 51.0, 52.0, 50.0])


class ExpectedMoveTests(unittest.TestCase):
    def test_annualized_percent_units(self):
        self.assertAlmostEqual(expected_move(1000.0, 100.0, 365), 1000.0)
        self.assertAlmostEqual(expected_move(1000.0, 1.0, 365), 10.0)
        self.assertAlmostEqual(expected_move(50000.0, 36.65, 1),
                               50000.0 * 0.3665 * math.sqrt(1 / 365))

    def test_horizons_are_configured(self):
        self.assertEqual(set(MOVE_HORIZONS), {1, 7, 30})

    def test_rejects_invalid_inputs(self):
        for price, dvol, days in ((0.0, 10.0, 1), (100.0, float("nan"), 1),
                                  (100.0, 10.0, 0), (100.0, 10.0, -2),
                                  (100.0, 10.0, True)):
            with self.subTest(price=price, dvol=dvol, days=days), self.assertRaises(DataError):
                expected_move(price, dvol, days)


class DVOLTests(unittest.TestCase):
    def test_no_sol_fallback(self):
        client = FakeClient([])
        dvol = DVOL(client=client, assets={"SOL": {"symbol": "SOL/USDC"}})
        self.assertIsNone(dvol.currency_for("SOL"))
        self.assertEqual(dvol.currency_for("BTC"), "BTC")
        result = dvol.snapshot("SOL", now_ms=NOW)
        self.assertFalse(result["supported"])
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["series"], [])
        self.assertIsNone(result["latest"])
        self.assertEqual(result["expected_move"], {})
        self.assertIn("SOL", result["error"])
        self.assertEqual(client.calls, [])
        with self.assertRaises(DataError):
            dvol.fetch_series("SOL", "3600", 86400, NOW)

    def test_supported_currencies(self):
        self.assertEqual(set(SUPPORTED_CURRENCIES), {"BTC", "ETH"})

    def test_backward_pagination_and_deduplication(self):
        data=series(hours=24)
        boundary=data[12][0]
        client=FakeClient([{"data":data[12:], "continuation":boundary},
                           {"data":data[:13], "continuation":None}])
        result=DVOL(client=client).fetch_series("BTC","3600",86400,NOW)
        self.assertEqual([r["t"] for r in result],[r[0] for r in data])
        self.assertEqual(client.calls[1][1]["end_timestamp"],boundary)
        self.assertEqual(client.calls[1][1]["start_timestamp"],NOW-86400000)

    def test_page_limit_and_conflicting_rows_fail_closed(self):
        data=series(hours=24)
        for pages in ([{"data":data[12:],"continuation":data[12][0]}],
                      [{"data":[*data,row(data[-1][0],60.)],"continuation":None}]):
            with self.subTest(pages=len(pages)),self.assertRaises(DataError):
                DVOL(client=FakeClient(pages),max_pages=1).fetch_series("BTC","3600",86400,NOW)

    def test_snapshot_fresh_expected_move_and_cache_outage(self):
        clock={"now":NOW}
        client=FakeClient([{"data":series(),"continuation":None}])
        dvol=DVOL(client=client,assets={"BTC":{"symbol":"BTC/USDC"}},
                  quote_provider=lambda name:quote(clock["now"]),clock=lambda:clock["now"])
        first=dvol.snapshot("BTC")
        self.assertTrue(first["fresh"],first)
        self.assertTrue(first["latest"]["forming"])
        self.assertAlmostEqual(first["expected_move"]["1d"],50000*.5/math.sqrt(365))
        clock["now"]+=30000
        self.assertTrue(dvol.snapshot("BTC")["fresh"])
        self.assertEqual(len(client.calls),1)
        clock["now"]+=31000;client.error="unavailable"
        stale=dvol.snapshot("BTC")
        self.assertTrue(stale["stale"]);self.assertFalse(stale["fresh"])
        self.assertEqual(stale["expected_move"],{})

    def test_stale_dvol_or_wrong_stale_price_never_produces_moves(self):
        for rows,q in ((series()[:-3],quote()),(series(),quote(NOW-31000)),
                       (series(),quote(symbol="BTC/USD"))):
            dvol=DVOL(client=FakeClient([{"data":rows,"continuation":None}]),
                      assets={"BTC":{"symbol":"BTC/USDC"}},quote_provider=lambda name:q,clock=lambda:NOW)
            result=dvol.snapshot("BTC")
            self.assertEqual(result["expected_move"],{})
            self.assertTrue(result["expected_move_error"])

    def test_gap_future_or_misaligned_history_rejected(self):
        data=series(hours=24)
        for broken in (data[:5]+data[6:],data+[row(NOW+HOUR,50)],data+[row(NOW-1,50)]):
            with self.subTest(n=len(broken)),self.assertRaises(DataError):
                DVOL(client=FakeClient([{"data":broken,"continuation":None}])).fetch_series("BTC","3600",86400,NOW)

    def test_changed_settings_uses_new_pair_and_quote_without_relabelling(self):
        dvol=DVOL(client=FakeClient([{"data":series(),"continuation":None}]),clock=lambda:NOW)
        first=dvol.snapshot("BTC",asset_config={"symbol":"BTC/USDC"},quote=quote())
        changed=dvol.snapshot("BTC",asset_config={"symbol":"BTC/USD"},quote=quote(symbol="BTC/USD"))
        self.assertEqual(changed["symbol"],"BTC/USD")
        self.assertEqual(changed["latest"],first["latest"])
        self.assertTrue(changed["expected_move"])

    def test_clock_rollback_does_not_reuse_cache(self):
        clock={"now":NOW};client=FakeClient([{"data":series(),"continuation":None}])
        dvol=DVOL(client=client,clock=lambda:clock["now"])
        dvol.snapshot("BTC");clock["now"]-=1000;client.error="offline"
        self.assertFalse(dvol.snapshot("BTC")["fresh"])
        self.assertEqual(len(client.calls),2)



class RouteTests(unittest.TestCase):
    def _app(self):
        from unittest.mock import patch
        from flask import request, session
        clock=patch("adaptive_crypto.dvol.time.time",return_value=NOW/1000)
        clock.start();self.addCleanup(clock.stop)
        class Market:
            def __init__(self, client):
                self.client=client
                self.open_pairs={"BTC/USDC","ETH/USDC","BTC/USD","ETH/USD"}
                client.market=self
            def supports_pair(self, pair): return pair in {"BTC/USDC","ETH/USDC","BTC/USD","ETH/USD"}
            def available_pairs(self): return sorted(self.open_pairs)
        market=patch("adaptive_crypto.deribit_market_data.DeribitMarketData",Market)
        market.start();self.addCleanup(market.stop)
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        client = FakeClient([{"data":series(),"continuation":None}])
        runtime = FakeRuntime(client)
        runtime.settings_path = Path(folder.name)/"settings.json"
        app = Flask(__name__, template_folder=str(ROOT / "adaptive_crypto" / "templates"),
                    static_folder=str(ROOT / "adaptive_crypto" / "static"))
        app.secret_key="offline-test"
        for endpoint in ("dashboard", "pine_chart_page", "paper_page", "positions_page",
                         "trade_records_page", "settings_page"):
            app.add_url_rule("/" + endpoint, endpoint, lambda: "stub")

        def render_dashboard(template=None, **context):
            return render_template(template, assets=runtime.assets, **context)

        def mutation_payload(required):
            payload=request.get_json(silent=True)
            if not isinstance(payload,dict) or payload.get("csrf_token")!=session.get("csrf_token") or not session.get("csrf_token"):
                raise DataError("Invalid CSRF token")
            if set(payload)!=set(required)|{"csrf_token"}: raise DataError("Unexpected fields")
            return payload

        register_dvol_routes(app, runtime, render_dashboard, mutation_payload)
        app.runtime=runtime
        return app

    def test_embedded_runtime_without_main_settings_uses_state_directory(self):
        from types import SimpleNamespace
        app=self._app();app.runtime.store=SimpleNamespace(path=app.runtime.settings_path.parent/"state.json")
        app.runtime.settings_path=None
        other=Flask("embedded")
        register_dvol_routes(other,app.runtime,lambda *args,**kwargs:"stub",lambda fields:{})
        self.assertEqual(other.extensions["pypta_dvol_settings"].path.parent,app.runtime.store.path.parent)

    def test_page_uses_independent_pairs(self):
        app=self._app();app.runtime.assets={"SOL":{"symbol":"SOL/USDT"}}
        client=app.test_client();page=client.get("/dvol")
        self.assertEqual(page.status_code,200)
        html=page.get_data(as_text=True)
        for text in ("DVOL", "BTC/USDC", "ETH/USDC", "Recent history", "Approximate expected move"):
            self.assertIn(text,html)
        self.assertNotIn("SOL/USDT",html)
        self.assertNotIn('value="SOL"',html)

    def test_api_reads_independent_deribit_reference(self):
        app=self._app();app.runtime.market={"BTC":{"quote":quote(symbol="BTC/USDT")}}
        client=app.test_client();btc=client.get("/api/dvol?pair=BTC/USDC")
        self.assertEqual(btc.status_code,200)
        payload=btc.get_json()
        self.assertEqual(payload["status"],"ok")
        self.assertEqual(payload["symbol"],"BTC/USDC")
        self.assertEqual(payload["currency"],"BTC")
        self.assertEqual(payload["price"],50000.)
        self.assertTrue(payload["expected_move"])
        self.assertEqual(client.get("/api/dvol?pair=SOL/USDC").status_code,404)

    def test_main_assets_pairs_and_source_changes_leave_dvol_independent(self):
        app=self._app();client=app.test_client()
        before=client.get("/api/settings/dvol").get_json()
        app.runtime.assets={"DOGE":{"symbol":"DOGE/USDT"},"BTC":{"symbol":"BTC/USD"}}
        app.runtime.provider=object()
        app.runtime.market={"BTC":{"quote":dict(quote(symbol="BTC/USD"),source="Binance spot",bid=60000.,ask=60000.)}}
        after=client.get("/api/settings/dvol").get_json()
        self.assertEqual(after,before)
        result=client.get("/api/dvol?pair=BTC/USDC").get_json()
        self.assertEqual(result["symbol"],"BTC/USDC")
        self.assertEqual(result["price"],50000.)
        self.assertTrue(result["expected_move"])

    def test_api_rejects_unconfigured_pair_and_period(self):
        client=self._app().test_client()
        self.assertEqual(client.get("/api/dvol?pair=BTC/USD").status_code,404)
        self.assertEqual(client.get("/api/dvol?pair=BTC/USDC&period=999").status_code,400)

    def test_settings_csrf_revision_and_immediate_application(self):
        app=self._app();client=app.test_client()
        saved=client.get("/api/settings/dvol").get_json()
        self.assertEqual(saved["available_pairs"],["BTC/USD","BTC/USDC","ETH/USD","ETH/USDC"])
        body={"settings":{"pairs":[{"symbol":"BTC/USD","enabled":True}]},"revision":saved["revision"]}
        self.assertEqual(client.post("/api/settings/dvol",json=body).status_code,400)
        with client.session_transaction() as session: session["csrf_token"]="test"
        body["csrf_token"]="test"
        result=client.post("/api/settings/dvol",json=body)
        self.assertEqual(result.status_code,200,result.get_json())
        self.assertEqual(app.runtime.assets["BTC"]["symbol"],"BTC/USDC")
        self.assertEqual(client.get("/api/dvol?pair=BTC/USDC").status_code,404)
        self.assertEqual(client.get("/api/dvol?pair=BTC/USD").get_json()["symbol"],"BTC/USD")
        self.assertEqual(client.post("/api/settings/dvol",json=body).status_code,400)

    def test_disabled_or_removed_rows_are_not_displayed(self):
        app=self._app();store=app.extensions["pypta_dvol_settings"]
        _,revision=store.read()
        store.save({"pairs":[{"symbol":"BTC/USDC","enabled":False}]},revision)
        client=app.test_client();page=client.get("/dvol")
        self.assertIn("No enabled DVOL pairs",page.get_data(as_text=True))
        self.assertNotIn('<option value="BTC/USDC">',page.get_data(as_text=True))
        self.assertEqual(client.get("/api/dvol?pair=BTC/USDC").status_code,404)

    def test_halted_deribit_market_is_excluded_even_when_catalog_retains_it(self):
        app=self._app();client=app.test_client()
        app.runtime.deribit.market.open_pairs.remove("BTC/USD")
        self.assertTrue(app.runtime.deribit.market.supports_pair("BTC/USD"))
        saved=client.get("/api/settings/dvol").get_json()
        self.assertNotIn("BTC/USD",saved["available_pairs"])
        store=app.extensions["pypta_dvol_settings"]
        _,revision=store.read()
        with self.assertRaisesRegex(DataError,"available Deribit market"):
            store.save({"pairs":[{"symbol":"BTC/USD","enabled":True}]},revision)


if __name__ == "__main__":
    unittest.main()
