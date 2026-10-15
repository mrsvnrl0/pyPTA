"""Behavioral regressions for SMC entries confirmed by the fixed Parente NN."""
from dataclasses import replace
import copy
from pathlib import Path
import tempfile
import unittest

from adaptive_crypto.core import Candle, DataError, H4, M5, Rules
from tests.currency_fixtures import fx
from adaptive_crypto.neural_engine import NeuralEngine
from adaptive_crypto.neural_ledger import (NeuralStore, normalize_manual_order,
                                           open_manual_trade, validate)

ASSETS = {"BTC": {"symbol": "BTC/USDC", "price_decimals": 2}}
NOW = (1800000000000//H4)*H4+1000
RULES = Rules(strategy_model="neural_network", fee_rate=.001, paper_floor=0,
              risk_per_trade=.02, max_total_risk=.05)


def bars(now=NOW, interval=H4, count=120):
    boundary = now//interval*interval
    return [Candle(boundary-(count-i)*interval, 100., 101., 99., 100., 1000., interval)
            for i in range(count)]


def quote(now=NOW, price=100.):
    return {"bid": price, "ask": price+.01, "asof_ms": now}


def market(now=NOW, identity="pine:fixture", *, tp1=110., tp2=None):
    plan = {"id": identity, "key": identity, "setup_key": "block:1", "source": "pine_smc",
            "side": "long", "stop": 90., "target": tp1, "tp1": tp1, "tp2": tp2,
            "exit_price": tp2 or tp1, "signal_end": now//M5*M5-1,
            "expires_ms": now+30000, "checks": [{"status": "pass", "key": "pine_rejection"}]}
    return {"source": "pine_smc", "guide": {"status": "qualified", "expires_ms": now+30000},
            "suggestion": plan, "suggestions": [plan]}


class FakeParente:
    identity = "a"*64
    trained_through_ms = 0

    def __init__(self, label="BUY"):
        self.label, self.calls = label, 0

    def predict(self, candles, asset):
        self.calls += 1
        return {"label": self.label, "model_id": self.identity, "signal_end": candles[-1].end,
                "probabilities": {s: .8 if s == self.label else .1 for s in ("BUY", "HOLD", "SELL")}}


class CombinedPaperStrategyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"paper.neural.json"
        self.store = NeuralStore(self.path, ASSETS, RULES)
        self.model = FakeParente()
        self.engine = NeuralEngine(ASSETS, RULES, self.store, model=self.model)

    def evaluate(self, now=NOW, price=100., smc=None, **kwargs):
        return self.engine.evaluate("BTC", kwargs.get("quote", quote(now, price)),
            kwargs.get("high", bars(now)), kwargs.get("low", bars(now, M5)), now,
            kwargs.get("errors"), smc_market=smc, neural_reading=kwargs.get("neural_reading"), fx_quote=fx(now))

    def trades(self):
        return self.store.snapshot()["assets"]["BTC"]["trades"]

    def test_buy_without_smc_and_hold_with_smc_cannot_open(self):
        self.evaluate()
        self.assertEqual(self.trades(), [])
        self.model.label = "HOLD"
        self.engine.cache.clear()
        self.evaluate(smc=market())
        self.assertEqual(self.trades(), [])

    def test_qualified_smc_buy_opens_with_immutable_smc_bracket_and_costs(self):
        proposed = market(tp1=110., tp2=120.)
        self.evaluate(smc=proposed)
        trade = self.trades()[0]
        self.assertEqual(trade["stop"], 90.)
        self.assertEqual((trade["tp1"], trade["tp2"], trade["target"]), (110., 120., 120.))
        self.assertEqual(trade["entry_origin"], "smc_parente")
        self.assertAlmostEqual(trade["entry"], 100.01*(1+RULES.slippage_rate))
        self.assertLessEqual(trade["initial_risk_usd"], RULES.paper_equity*RULES.risk_per_trade+1e-8)
        proposed["suggestion"]["stop"] = 50.
        self.assertEqual(self.trades()[0]["smc_entry_plan"]["stop"], 90.)
        self.assertIn("SMC + PARENTE PAPER BUY", self.store.snapshot()["outbox"][0]["text"])
        validate(self.store.snapshot())

    def test_ledger_rejects_changed_saved_smc_stop_and_consumed_identity(self):
        self.evaluate(smc=market())
        original = self.store.snapshot()
        for mutate in (lambda t: t["smc_entry_plan"].update(stop=80.),
                       lambda t: t.update(smc_consumed_key="some-other-key")):
            corrupted = copy.deepcopy(original)
            mutate(corrupted["assets"]["BTC"]["trades"][0])
            with self.assertRaises(DataError):
                validate(corrupted)

    def test_same_setup_cannot_reopen_on_repeated_read_or_restart(self):
        self.evaluate(smc=market())
        self.evaluate(now=NOW+2000, smc=market(NOW+2000))
        self.store = NeuralStore(self.path, ASSETS, RULES)
        self.engine = NeuralEngine(ASSETS, RULES, self.store, model=self.model)
        self.evaluate(now=NOW+3000, smc=market(NOW+3000))
        self.assertEqual(len(self.trades()), 1)
        self.assertEqual(len(self.store.snapshot()["outbox"]), 1)

    def test_distinct_setup_can_use_same_current_4h_confirmation_after_exit(self):
        self.evaluate(smc=market())
        self.evaluate(now=NOW+2000, price=111., smc=market(NOW+2000))
        self.assertEqual(self.trades()[0]["exit_reason"], "target")
        self.evaluate(now=NOW+M5, smc=market(NOW+M5, "pine:second"))
        self.assertEqual(len(self.trades()), 2)
        self.assertEqual(self.trades()[0]["signal_end"], self.trades()[1]["signal_end"])
        self.assertEqual(self.model.calls, 1)
        validate(self.store.snapshot())

    def test_current_buy_confirmation_remains_usable_after_old_60_second_window(self):
        now = NOW+70000
        self.evaluate(now=now, smc=market(now))
        self.assertEqual(len(self.trades()), 1)

    def test_expired_guide_expired_plan_and_invalid_actual_bracket_block(self):
        for change in ("guide", "plan", "bracket"):
            with self.subTest(change=change):
                proposed = market()
                if change == "guide":
                    proposed["guide"]["expires_ms"] = NOW
                elif change == "plan":
                    proposed["suggestion"]["expires_ms"] = NOW
                else:
                    proposed["suggestion"]["stop"] = 100.005
                self.evaluate(smc=proposed)
                self.assertEqual(self.trades(), [])

    def test_slippage_cannot_fill_at_or_beyond_tp1(self):
        self.evaluate(smc=market(tp1=100.04))
        self.assertEqual(self.trades(), [])

    def test_target_must_cover_recorded_costs(self):
        self.evaluate(smc=market(tp1=100.1))
        self.assertEqual(self.trades(), [])
        self.assertIn("costs", self.store.snapshot()["assets"]["BTC"]["last_result"])

    def test_tp1_is_waypoint_when_plan_executes_tp2(self):
        self.evaluate(smc=market(tp1=105., tp2=110.))
        self.evaluate(now=NOW+2000, price=106.)
        self.assertEqual(self.trades()[0]["status"], "active")
        self.evaluate(now=NOW+3000, price=111.)
        self.assertEqual(self.trades()[0]["exit_reason"], "target")

    def test_later_parente_sell_closes_older_combined_entry(self):
        self.evaluate(smc=market())
        self.model.label = "SELL"
        self.evaluate(now=NOW+H4, price=104.)
        self.assertEqual(self.trades()[0]["exit_reason"], "Parente SELL")
        self.assertEqual(self.trades()[0]["status"], "sold")
        validate(self.store.snapshot())

    def test_stop_and_target_monitoring_survive_missing_or_stale_model_input(self):
        for price, reason in ((80., "stop"), (111., "target")):
            with self.subTest(reason=reason):
                self.setUp()
                self.evaluate(smc=market())
                self.engine.model, self.engine.model_error = None, "Model unavailable"
                self.evaluate(now=NOW+2000, price=price, high=[], low=[])
                trade = self.trades()[0]
                self.assertEqual(trade["exit_reason"], reason)
                self.assertTrue(trade["tracking_gap"])
                validate(self.store.snapshot())

    def test_full_bar_stop_has_priority_when_it_also_touches_target(self):
        self.evaluate(smc=market())
        now = NOW+2*M5
        low = bars(now, M5)
        low[-1] = replace(low[-1], o=85., l=80., h=120., c=100.)
        self.model.label = "SELL"
        self.evaluate(now=now, low=low)
        trade = self.trades()[0]
        self.assertEqual(trade["exit_reason"], "stop")
        self.assertAlmostEqual(trade["exit"], 85.*(1-RULES.slippage_rate))

    def test_missing_full_monitoring_bar_records_tracking_gap(self):
        self.evaluate(smc=market())
        now = NOW+3*M5
        low = bars(now, M5)
        first_full_start = ((NOW+M5-1)//M5)*M5
        low = [bar for bar in low if bar.t != first_full_start]
        self.evaluate(now=now, low=low)
        self.assertTrue(self.trades()[0]["tracking_gap"])

    def test_entry_containing_bar_cannot_backdate_stop_or_target(self):
        self.evaluate(smc=market())
        now = NOW+M5
        low = bars(now, M5)
        low[-1] = replace(low[-1], l=50., h=150.)
        self.evaluate(now=now, low=low)
        self.assertEqual(self.trades()[0]["status"], "active")

    def test_missing_monitoring_history_prevents_new_entry(self):
        self.evaluate(smc=market(), low=[])
        self.assertEqual(self.trades(), [])

    def test_stale_reused_neural_reading_cannot_confirm_entry(self):
        reading = self.engine.read("BTC", bars(), NOW)
        self.evaluate(now=NOW+H4, smc=market(NOW+H4), neural_reading=reading)
        self.assertEqual(self.trades(), [])

    def test_unlimited_sizing_still_uses_recorded_smc_stop(self):
        rules = replace(RULES, nn_limitations=False)
        self.store = NeuralStore(self.path, ASSETS, rules)
        self.engine = NeuralEngine(ASSETS, rules, self.store, model=self.model)
        self.evaluate(smc=market())
        self.assertEqual(self.trades()[0]["stop"], 90.)
        self.assertAlmostEqual(self.store.snapshot()["cash"], 0.)
        self.evaluate(now=NOW+2000, price=80.)
        self.assertEqual(self.trades()[0]["status"], "stopped")
        validate(self.store.snapshot())

    def test_manual_entry_is_not_reopened_by_its_current_reading_and_exits_later_sell(self):
        reading = self.engine.read("BTC", bars(), NOW)["signal"]
        order = normalize_manual_order("a"*32, "amount", 100.)
        self.store.transaction(lambda d: open_manual_trade(d, "BTC", reading, quote(), RULES, NOW, order, fx=fx(NOW)))
        self.model.label = "SELL"
        self.engine.cache.clear()
        self.evaluate(now=NOW+2000)
        self.assertEqual(self.trades()[0]["status"], "active")
        self.evaluate(now=NOW+H4)
        self.assertEqual(self.trades()[0]["status"], "sold")
        validate(self.store.snapshot())


if __name__ == "__main__":
    unittest.main()
