"""Offline delivery and audit text regressions for SMC + Parente paper trades."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adaptive_crypto.core import DataError, H4, utc
from adaptive_crypto.neural_engine import NeuralEngine
from adaptive_crypto.neural_ledger import NeuralStore, close_trade, validate
from adaptive_crypto.notifications import dispatch_once
from tests.neural_fixtures import ASSETS, NOW, RULES, FakeModel, bars, quote, qualified_market
from tests.currency_fixtures import fx
from adaptive_crypto.core import M5


class PaperAlertDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = NeuralStore(Path(self.temp.name)/"paper.json", ASSETS, RULES)
        self.engine = NeuralEngine(ASSETS, RULES, self.store, model=FakeModel())

    def open(self, plan=None):
        self.engine.evaluate("BTC", quote(), bars(), bars(interval=M5), NOW,
                             smc_market=plan or qualified_market(), fx_quote=fx(NOW))
        self.assertEqual(len(self.store.snapshot()["assets"]["BTC"]["trades"]), 1)
        return self.store.snapshot()["assets"]["BTC"]["trades"][0]

    def deliver(self, now=NOW, store=None):
        sent = []
        def sender(event):
            sent.append(copy.deepcopy(event))
            return {"status": "sent", "error": None}
        result = dispatch_once(store or self.store, "telegram", sender, now)
        return result, sent

    def close(self, reason="target", now=NOW+1000, exit_signal=None):
        return self.store.transaction(lambda d: close_trade(
            d, d["assets"]["BTC"]["trades"][0], 110., now, reason,
            exit_signal=exit_signal, fx=fx(now)))

    def test_current_buy_has_pair_utc_model_probability_cost_and_bracket(self):
        trade = self.open()
        result, sent = self.deliver()
        self.assertTrue(result)
        self.assertEqual(len(sent), 1)
        event, text = sent[0], sent[0]["text"]
        for expected in ("BTC/USDC", "Parente 5/2", "model "+trade["model_id"][:12],
                         "Binance spot/USDT 4H", "Decision candle close "+utc(trade["signal_end"]+1),
                         "execution "+utc(NOW), "BUY 80.00%", "HOLD 10.00%", "SELL 10.00%",
                         "Entry fee 0.1000%", "slippage 0.0500%", "SMC stop", "TP1",
                         "TP2 pending"):
            self.assertIn(expected, text)
        self.assertEqual(event["alert_type"], "paper_entry")
        self.assertEqual(event["trade_id"], trade["id"])
        self.assertEqual(event["asset"], "BTC")
        self.assertEqual(event["symbol"], "BTC/USDC")
        self.assertEqual(event["observed_ms"], NOW)
        self.assertEqual(event["valid_until_ms"], NOW+30000)
        self.assertEqual(event["payload"], trade)

    def test_expired_smc_buy_is_cancelled_without_sending(self):
        self.open()
        result, sent = self.deliver(NOW+30000)
        self.assertFalse(result)
        self.assertEqual(sent, [])
        self.assertEqual(self.store.snapshot()["outbox"][0]["status"], "cancelled")

    def test_entry_expiry_also_respects_next_4h_decision_boundary(self):
        plan = qualified_market()
        plan["suggestion"]["expires_ms"] = NOW+2*H4
        plan["guide"]["expires_ms"] = NOW+2*H4
        trade = self.open(plan)
        expiry = trade["signal_end"]+H4+1
        self.assertEqual(self.store.snapshot()["outbox"][0]["valid_until_ms"], expiry)
        result, sent = self.deliver(expiry)
        self.assertFalse(result)
        self.assertEqual(sent, [])

    def test_closed_buy_cancelled_but_exit_delivered_after_entry_expiry(self):
        self.open()
        original_payload = self.store.snapshot()["outbox"][0]["payload"]
        self.close()
        result, sent = self.deliver(NOW+60000)
        self.assertTrue(result)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["id"].endswith(":exit"))
        events = self.store.snapshot()["outbox"]
        self.assertEqual(events[0]["status"], "cancelled")
        self.assertEqual(events[0]["payload"], original_payload)
        self.assertEqual(events[1]["status"], "sent")

    def test_deleted_buy_cancelled_even_with_future_retry(self):
        self.open()
        def delete(document):
            trade = document["assets"]["BTC"]["trades"].pop()
            document["cash"] += trade["collateral"]+trade["entry_fee"]
            document["outbox"][0]["retry_ms"] = NOW+H4
        self.store.transaction(delete)
        result, sent = self.deliver()
        self.assertFalse(result)
        self.assertEqual(sent, [])
        event = self.store.snapshot()["outbox"][0]
        self.assertEqual(event["status"], "cancelled")
        self.assertIn("removed", event["error"])

    def test_close_between_claim_and_sender_is_rechecked(self):
        self.open()
        real, close = self.store, self.close
        class CloseAfterClaim:
            def snapshot(self):
                return real.snapshot()
            def transaction(self, operation):
                result = real.transaction(operation)
                if operation.__name__ == "claim":
                    close()
                return result
        result, sent = self.deliver(store=CloseAfterClaim())
        self.assertFalse(result)
        self.assertEqual(sent, [])
        result, sent = self.deliver()
        self.assertTrue(result)
        self.assertTrue(sent[0]["id"].endswith(":exit"))

    def test_expiry_clock_is_refreshed_after_admission_lock_wait(self):
        self.open()
        real = self.store
        clock = [NOW/1000]
        sent = []
        class DelayAdmission:
            def snapshot(self):
                return real.snapshot()
            def transaction(self, operation):
                if operation.__name__ == "admit":
                    clock[0] = (NOW+30000)/1000
                return real.transaction(operation)
        with patch("adaptive_crypto.notifications.time.time", side_effect=lambda: clock[0]):
            delivered = dispatch_once(DelayAdmission(), "telegram", lambda e: sent.append(e))
        self.assertFalse(delivered)
        self.assertEqual(sent, [])
        self.assertEqual(real.snapshot()["outbox"][0]["status"], "cancelled")

    def test_stop_and_target_are_not_reported_as_new_nn_sell(self):
        self.open()
        self.close("stop")
        trade = self.store.snapshot()["assets"]["BTC"]["trades"][0]
        text = self.store.snapshot()["outbox"][1]["text"]
        self.assertNotIn("exit_decision", trade)
        self.assertIn("SMC STOP", text)
        self.assertIn("Entry confirmation: Parente 5/2", text)
        self.assertNotIn("Exit decision candle close", text)
        self.assertIn("execution "+utc(NOW+1000), text)
        self.assertIn("Gross P/L", text)
        self.assertIn("entry fee", text)
        self.assertIn("exit fee", text)
        self.assertIn("Net P/L", text)

    def test_actual_later_sell_decision_is_copied_and_shown(self):
        trade = self.open()
        signal = {"model_id": trade["model_id"], "label": "SELL",
                  "signal_end": trade["signal_end"]+H4,
                  "probabilities": {"BUY": .1, "HOLD": .1, "SELL": .8}}
        self.close("Parente SELL", NOW+H4, signal)
        original = copy.deepcopy(signal)
        signal["label"] = "HOLD"
        document = self.store.snapshot()
        self.assertEqual(document["assets"]["BTC"]["trades"][0]["exit_decision"], original)
        text = document["outbox"][1]["text"]
        self.assertIn("PARENTE SELL", text)
        self.assertIn("Exit decision candle close "+utc(original["signal_end"]+1), text)
        self.assertIn("SELL 80.00%", text)
        self.assertNotIn("Entry confirmation:", text)
        validate(document)

    def test_exit_decision_cannot_be_attached_to_stop_or_same_entry_candle(self):
        trade = self.open()
        signal = {"model_id": trade["model_id"], "label": "SELL",
                  "signal_end": trade["signal_end"],
                  "probabilities": {"BUY": .1, "HOLD": .1, "SELL": .8}}
        with self.assertRaises(DataError):
            self.close("Parente SELL", NOW+1000, signal)
        signal["signal_end"] += H4
        with self.assertRaises(DataError):
            self.close("stop", NOW+H4, signal)

    def test_quoted_loss_has_exact_recorded_fee_breakdown(self):
        entry, exit_price, quantity, rate = 83256.4074, 82922.61795, 1.1902023, .004
        trade = {"asset": "BTC", "symbol": "BTC/USD", "id": "a9a55a23",
                 "entry_origin": "manual", "entry": entry, "quantity": quantity,
                 "entry_fee": entry*quantity*rate, "entry_fee_quote": entry*quantity*rate,
                 "collateral":entry*quantity,"fee_rate": rate,
                 "slippage_rate": 0., "status": "active", "model_id": "a"*64,
                 "entry_reading": {"signal_end": NOW//H4*H4-1,
                                   "probabilities": {"BUY": .8, "HOLD": .1, "SELL": .1}}}
        document = {"cash": 0., "realized_pnl": 0., "outbox": []}
        close_trade(document, trade, exit_price, NOW+300000, "Parente SELL")
        expected_gross = quantity*(exit_price-entry)
        expected_entry_fee, expected_exit_fee = quantity*entry*rate, quantity*exit_price*rate
        expected_net = expected_gross-expected_entry_fee-expected_exit_fee
        self.assertAlmostEqual(trade["realized_pnl"], -1188.4236, places=3)
        self.assertAlmostEqual(trade["realized_pnl"], expected_net, places=8)
        text = document["outbox"][0]["text"]
        for expected in (f"Native Gross P/L {expected_gross:.8f} USD",
                         f"entry fee {expected_entry_fee:.8f} USD",
                         f"exit fee {expected_exit_fee:.8f} USD",
                         f"Account Net P/L {expected_net:.8f} USD", "Recorded fee 0.4000% per side"):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
