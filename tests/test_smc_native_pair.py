"""Scoped SMC native-pair fixes: shared price formatter, timeframe whitelist and alert wording."""
import unittest

from adaptive_crypto.core import DataError
from adaptive_crypto.holding_targets import validate_holding_target
from adaptive_crypto.position_alerts import native_quote, price, target_reached_alert
from adaptive_crypto.pine_alerts import monitor_pine_setups
from adaptive_crypto.buy_watches import watch_alert
from adaptive_crypto.take_profit_alerts import near_take_profit


NOW = 1_700_000_000_000


class NativePairFormatterTests(unittest.TestCase):
    def test_native_quote_derives_quote_currency_from_symbol(self):
        self.assertEqual(native_quote("BTC/USDC"), "USDC")
        self.assertEqual(native_quote("ETH/BTC"), "BTC")
        self.assertEqual(native_quote("SOL/USDT"), "USDT")
        self.assertEqual(native_quote("XRP/EUR"), "EUR")
        self.assertEqual(native_quote("BTC/USD"), "USD")
        self.assertEqual(native_quote(None), "USD")
        self.assertEqual(native_quote("NOPAIR"), "USD")

    def test_price_preserves_usd_and_formats_native_quotes(self):
        self.assertEqual(price(100), "$100.00")
        self.assertEqual(price(100, "USD"), "$100.00")
        self.assertEqual(price(100, "USDC"), "100.00 USDC")
        self.assertEqual(price(100, "USDT"), "100.00 USDT")
        self.assertEqual(price(0.045, "BTC"), "0.045 BTC")
        self.assertEqual(price(0.5, "EUR"), "0.50 EUR")
        self.assertEqual(price(1.2e-9, "USD"), "$1.2e-09")


class HoldingTimeframeWhitelistTests(unittest.TestCase):
    def position(self, setup_minutes):
        level = 110.0
        buffer = 10.0
        return {
            "id": "a" * 32,
            "symbol": "BTC/USDC",
            "side": "long",
            "entry": 100.0,
            "opened_ms": 1000,
            "take_profit": {
                "state": "watching",
                "liquidity_price": level,
                "price": level * (1 + buffer / 10000),
                "sweep_buffer_bps": buffer,
                "selected_ms": 2000,
                "pivot_ms": 1500,
                "setup_minutes": setup_minutes,
                "reached_ms": None,
                "selection": {"method": "smc", "liquidity_price": level,
                              "selected_ms": 2000, "matches": []},
            },
        }

    def test_supported_timeframes_are_accepted(self):
        for minutes in (1, 5, 15, 30, 240):
            with self.subTest(minutes=minutes):
                validate_holding_target(self.position(minutes))

    def test_invalid_timeframes_are_rejected(self):
        for minutes in (10, 60, "30", True, None):
            with self.subTest(minutes=minutes):
                with self.assertRaises(DataError):
                    validate_holding_target(self.position(minutes))


class NativePairAlertTests(unittest.TestCase):
    def test_pine_setup_alert_uses_native_quote(self):
        document = {"outbox": []}
        market = {
            "suggestion": {"id": "pine:1", "side": "long", "entry_price": 0.045,
                           "stop": 0.044, "nearest_target": 0.046, "tp2": None},
            "guide": {"status": "qualified", "expires_ms": NOW + 30000},
            "structure_quality": {"break_label": "Medium", "live_score": 2, "touches": 1},
        }
        monitor_pine_setups(document, "ETH", "ETH/BTC", market, NOW)
        text = document["outbox"][0]["text"]
        self.assertIn("0.045 BTC", text)
        self.assertIn("0.044 BTC", text)
        self.assertIn("0.046 BTC", text)
        self.assertNotIn("$0.045", text)

    def test_buy_watch_alert_uses_native_quote(self):
        watch = {"id": "a" * 32, "asset": "ETH", "symbol": "ETH/BTC",
                 "buy_price": 0.035, "reference_price": 0.04, "quantity": 1,
                 "direction": "neutral"}
        document = {"outbox": []}
        watch_alert(document, watch, "buy", {"ask": 0.036, "asof_ms": NOW}, NOW)
        text = document["outbox"][0]["text"]
        self.assertIn("0.035 BTC", text)
        self.assertIn("0.04 BTC", text)
        self.assertIn("0.036 BTC", text)
        self.assertNotIn("$0.035", text)

    def test_target_reached_alert_uses_native_quote(self):
        position = {"id": "a" * 32, "asset": "ETH", "symbol": "ETH/BTC",
                    "side": "long", "quantity": 1, "entry": 0.04,
                    "take_profit": {"price": 0.05, "reached_ms": NOW - 1000}}
        document = {"outbox": []}
        target_reached_alert(document, position,
                             {"bid": 0.05, "ask": 0.051, "asof_ms": NOW},
                             NOW, newly_reached=True)
        text = document["outbox"][0]["text"]
        self.assertIn("0.05 BTC", text)
        self.assertIn("0.04 BTC", text)
        self.assertNotIn("$0.05", text)

    def test_near_take_profit_alert_uses_native_quote(self):
        position = {"id": "a" * 32, "asset": "ETH", "symbol": "ETH/BTC",
                    "side": "long", "entry": 0.04, "quantity": 1}
        document = {"outbox": []}
        near_take_profit(document, "near-tp", "ETH", "long", 0.05,
                         {"bid": 0.0496, "ask": 0.0497, "asof_ms": NOW},
                         NOW, 100, "holding", reference=0.051,
                         position_ids=["a" * 32], position=position)
        text = document["outbox"][0]["text"]
        self.assertIn("0.05 BTC", text)
        self.assertIn("0.0496 BTC", text)
        self.assertIn("0.051 BTC", text)
        self.assertNotIn("$0.05", text)


if __name__ == "__main__":
    unittest.main()
