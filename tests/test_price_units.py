"""Rendered native pair prices stay separate from USD account amounts."""
from pathlib import Path
from types import SimpleNamespace
import unittest

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]


class PriceUnitTests(unittest.TestCase):
    def setUp(self):
        self.env = Environment(loader=FileSystemLoader(ROOT / "adaptive_crypto" / "templates"))
        def money(value, places=2, currency=None):
            if value is None:
                return "—"
            if currency is None:
                raise AssertionError("Native price must provide its quote currency")
            return f"{float(value):.{places}f} {currency}"
        from adaptive_crypto.web import pnl_money
        self.env.globals.update(money=money, pnl_money=pnl_money, account_money=lambda value, places=2: f"{float(value):.{places}f} USD",
                                measure=str, num=lambda value, places=2: str(value), time=str,
                                url_for=lambda name, **kwargs: "/" + name)
        self.assets = {"ETH": {"symbol": "ETH/BTC", "price_decimals": 8},
                       "BTC": {"symbol": "BTC/USDC", "price_decimals": 2},
                       "SOL": {"symbol": "SOL/USDT", "price_decimals": 3}}

    def test_paper_row_prices_native_fees_and_results_usd(self):
        trades=[]
        for name, row in self.assets.items():
            trades.append({"asset": name, "symbol": row["symbol"], "side": "long", "id": name*8,
                           "paper_key": name, "model": "Parente", "model_id": "fixed", "model_active": False,
                           "status": "closed", "entry": 0.045 if name=="ETH" else 100, "exit": 0.05 if name=="ETH" else 110,
                           "quantity": 1, "stop": 0.04 if name=="ETH" else 90, "target": 0.05 if name=="ETH" else 110,
                           "opened_ms": 1, "closed_ms": 2, "realized_pnl": 15,
                           "entry_origin": "manual", "manual_order": {"source_position_id": None},
                           "collateral": 100, "entry_fee": 1})
        html=self.env.get_template("paper_trade_table.html").render(paper_rows=trades,digits={name: row["price_decimals"] for name,row in self.assets.items()})
        self.assertIn("0.04500000 BTC",html)
        self.assertIn("0.05000000 BTC",html)
        self.assertIn("100.00 USDC",html)
        self.assertIn("100.000 USDT",html)
        self.assertEqual(html.count("Cash used 101.00 USD"),3)
        self.assertEqual(html.count("Includes entry fee 1.00 USD"),3)
        self.assertEqual(html.count(">15.00 USD</td>"),3)
        self.assertNotIn("0.04500000 USD",html)

    def test_position_targets_and_profit_use_recorded_native_quote(self):
        p={"asset":"ETH","symbol":"ETH/BTC","side":"long","id":"a"*32,"feed_enabled":True,
           "stop_price":0.04,"suggested_stop":0.039,"position_targets":{"smc":{"price":0.06,"selected_ms":1,"state":"watching"}},
           "nn_guidance":{"expires_ms":100,"profit_at_risk":True,"gross_pnl":0.00123456,"gross_pnl_percent":2,"light":"STOP LOSS"}}
        wrapper=self.env.from_string("{% from 'signal_lights.html' import signal_lights with context %}{% from 'target_selection.html' import target_selection with context %}{% include 'position_signals.html' %}")
        html=wrapper.render(p=p,digits={"ETH":8},now=10,s={},csrf_token="token")
        self.assertIn("0.00123456 BTC",html)
        self.assertIn("0.06000000 BTC",html)
        self.assertIn("0.03900000 BTC",html)
        self.assertNotIn(" USD",html)

    def test_manual_paper_form_usd_budget_with_each_native_choice(self):
        s={"is_neural":True,"holdings":{"positions":[]},"portfolio":{"cash":1000},"rules":{"slippage_rate":0.001,"nn_limitations":True}}
        html=self.env.get_template("manual_paper_entry.html").render(s=s,assets=self.assets,request=SimpleNamespace(args={}),
              csrf_token="token",paper_request_id="a"*32)
        self.assertIn("USD budget · includes entry fee",html)
        self.assertIn("available cash 1000.00 USD",html)
        self.assertIn('data-native-quote-label>BTC</span>',html)
        for quote in ("BTC","USDC","USDT"):
            self.assertIn('data-quote="'+quote+'"',html)
        self.assertNotIn('value="45',html)

    def test_linked_holding_native_cost_is_not_prefilled_as_usd_budget(self):
        position={"id":"a"*32,"status":"open","position_type":"spot","asset":"ETH","symbol":"ETH/BTC","entry":0.05,"quantity":2}
        s={"is_neural":True,"holdings":{"positions":[position]},"portfolio":{"cash":1000},"rules":{"slippage_rate":0.001,"nn_limitations":True}}
        html=self.env.get_template("manual_paper_entry.html").render(s=s,assets=self.assets,request=SimpleNamespace(args={"paper_source":position["id"]}),
              csrf_token="token",paper_request_id="b"*32)
        self.assertIn('data-native-quote="BTC"',html)
        self.assertIn('name="budget_usd" min="0.01" step="any" required value=""',html)
        self.assertIn("Enter its budget in USD",html)
        self.assertIn("data-native-quote-label>BTC</span>",html)

    def test_historical_btc_quote_profit_survives_current_usd_pair_precision(self):
        position={"asset":"ETH","symbol":"ETH/BTC","side_label":"SPOT BUY","id":"a"*32,"status":"closed",
                  "entry":0.05,"close":0.0501,"quantity":1,"closed_ms":2,"take_profit":None,
                  "pnl_usd":0.0001,"pnl_percent":0.2}
        context={"s":{"holdings":{"positions":[position]},"state":{"outbox":[]},
                      "portfolio":{"cash":1000,"cost_equity":1000,"open_risk":0,"realized_pnl":0,"active_positions":0}},
                 "records":{"errors":[],"closed":[]},"digits":{"ETH":2},
                 "assets":{"ETH":{"symbol":"ETH/USD","price_decimals":2}},
                 "request":SimpleNamespace(endpoint="trade_records_page")}
        html=self.env.get_template("trade_records.html").render(**context)
        self.assertIn("0.00010000 BTC",html)
        self.assertNotIn("0.00010000 USD",html)
        from adaptive_crypto.web import pnl_money
        for currency in ("USD","USDC","USDT","USDE","FDUSD","EUR","GBP","JPY"):
            with self.subTest(currency=currency):
                self.assertNotIn("0.00010000",pnl_money(0.0001,currency,2))


if __name__ == "__main__":
    unittest.main()
