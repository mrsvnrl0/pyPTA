"""Minimum exit distance keeps intermediate TP1 and searches genuine liquidity."""
import copy
from dataclasses import asdict, replace
from pathlib import Path
import tempfile
import unittest

from adaptive_crypto.core import Candle, DataError, Rules, exit_distance_pct
from adaptive_crypto.gex_targets import select_target, rules_for_target
from adaptive_crypto.neural_engine import NeuralEngine
from adaptive_crypto.neural_ledger import NeuralStore, open_combined_trade
from adaptive_crypto.settings_editor import field_groups
from tests import test_pine_guidance as pine_fixture
from tests import test_combined_paper_strategy as paper_fixture
from tests.currency_fixtures import fx


class ExitDistanceTests(unittest.TestCase):
    def setUp(self):
        self.f = pine_fixture.PineGuidanceTests()
        self.f.setUp()
        self.f.rules = replace(self.f.rules, smc_min_exit_distance_pct=3)
        point = self.f.structure["liquidity"][0]
        self.f.structure["liquidity"] = [{**point, "price": p} for p in (106.5, 107, 108)]
        self.f.profile["call_wall"]["strike"] = 107.5

    def test_search_skips_close_aligned_exit_but_keeps_nearest_tp1(self):
        previous = self.f.run_market(rules=replace(self.f.rules, smc_min_exit_distance_pct=0))["suggestion"]
        self.assertEqual((previous["tp1"], previous["tp2"]), (106.5, 107))
        current = self.f.run_market()["suggestion"]
        self.assertEqual((current["tp1"], current["tp2"], current["exit_price"]), (106.5, 108, 108))
        self.assertEqual(current["minimum_exit_distance_pct"], 3)
        self.assertGreater(current["exit_distance_pct"], 3)
        self.assertEqual(current["tp2_matches"][0]["poi"]["price"], 108)
        self.assertTrue(all(c["status"] == "pass" for c in current["checks"]))

    def test_no_eligible_distant_target_withholds_setup_without_inventing_level(self):
        self.f.structure["liquidity"] = self.f.structure["liquidity"][:2]
        result = self.f.run_market()
        self.assertIsNone(result["suggestion"])
        self.assertIn("exit_distance_below_minimum", self.f.codes(result))
        self.assertEqual(result["liquidity"]["nearest_above"]["price"], 106.5)

    def test_gex_outage_or_negative_regime_cannot_approve_farther_tp2(self):
        for profile in (None, {**self.f.profile, "regime": "negative"},
                        {**self.f.profile, "asof_ms": self.f.now-300000}):
            with self.subTest(profile=profile):
                result = self.f.run_market(profile=profile)
                self.assertIsNone(result["suggestion"])
                self.assertIn("exit_distance_below_minimum", self.f.codes(result))

    def test_nearest_tp1_can_be_final_exit_when_it_itself_meets_minimum(self):
        self.f.structure["liquidity"] = [{**self.f.structure["liquidity"][0], "price": 110}]
        plan = self.f.run_market(profile=None)["suggestion"]
        self.assertEqual((plan["tp1"], plan["tp2"], plan["exit_price"]), (110, None, 110))

    def test_unconfirmed_taken_and_previously_touched_farther_liquidity_rejected(self):
        for mutation in ("future", "taken", "touched"):
            with self.subTest(mutation=mutation):
                structure = copy.deepcopy(self.f.structure)
                bars = list(self.f.bars)
                if mutation == "future":
                    structure["liquidity"][-1]["known_ms"] = self.f.now+1
                elif mutation == "taken":
                    structure["liquidity"][-1]["taken"] = True
                else:
                    bars[6] = Candle(bars[6].t, 104, 108, 103, 104, 1, self.f.interval)
                self.assertIsNone(self.f.run_market(structure=structure, bars=bars)["suggestion"])

    def test_exact_minimum_is_inclusive(self):
        target = self.f.quote["ask"]*1.03
        self.f.structure["liquidity"][-1]["price"] = target
        self.f.profile["call_wall"]["strike"] = target
        plan = self.f.run_market()["suggestion"]
        self.assertEqual(plan["exit_price"], target)
        self.assertAlmostEqual(plan["exit_distance_pct"], 3)

    def test_quality_still_blocks_even_with_distant_target(self):
        self.f.structure["settings"]["min_ob_score"] = 3
        self.assertIsNone(self.f.run_market()["suggestion"])

    def test_short_search_is_symmetric(self):
        f = self.f
        f.bars = [Candle(b.t, 210-b.o, 210-b.l, 210-b.h, 210-b.c, b.v, b.interval) for b in f.bars]
        f.structure["regime"] = "bearish"
        f.structure["events"][0]["side"] = "short"
        f.structure["zones"][0].update(side="short", low=108, high=112)
        f.structure["liquidity"] = [{**p, "side": "short", "price": 210-p["price"]} for p in f.structure["liquidity"]]
        quote = {"bid": 105.98, "ask": 106, "asof_ms": f.now}
        profile = {**f.profile, "spot":105.99, "put_wall":{"strike":102.5}}
        plan = f.run_market(quote=quote, profile=profile, rules=replace(f.rules, market_mode="margin"))["suggestion"]
        self.assertEqual((plan["tp1"],plan["tp2"]), (103.5,102))
        self.assertGreater(plan["exit_distance_pct"],3)

    def test_setting_exposed_with_percent_units_and_invalid_values_rejected(self):
        field = next(f for f in field_groups({"strategy":asdict(self.f.rules)})["Perky SMC strategy"]
                     if f["key"] == "smc_min_exit_distance_pct")
        self.assertEqual(field["value"],3)
        self.assertIn("(%)",field["label"])
        for value in (-1,101,True,float("nan"),"3"):
            with self.subTest(value=value), self.assertRaises(DataError):
                replace(Rules(),smc_min_exit_distance_pct=value).validate()
        self.assertAlmostEqual(exit_distance_pct(100,103,"long"),3)
        self.assertAlmostEqual(exit_distance_pct(100,97,"short"),3)

    def test_position_tp2_uses_recorded_entry_and_keeps_smc_price(self):
        confluence=self.f.run_market()["gex_context"]
        point=select_target([],[],rules_for_target(self.f.rules,"gex_smc"),"long",106.5,self.f.now,
                            confluence,exit_reference=self.f.quote["ask"])
        self.assertEqual(point["price"],108)
        self.assertEqual(point["target_selection"]["method"],"gex_smc")

    def test_rechecked_quote_and_actual_paper_fill_cannot_evade_minimum(self):
        p=paper_fixture
        proposed=p.market(tp1=102,tp2=103.04)
        accepted=NeuralEngine._qualified_plan(proposed,p.quote(),p.NOW,3)
        with self.assertRaisesRegex(DataError,"minimum target distance"):
            NeuralEngine._qualified_plan(proposed,p.quote(price=100.05),p.NOW,3)
        rules=replace(p.RULES,smc_min_exit_distance_pct=3)
        with tempfile.TemporaryDirectory() as temp:
            store=NeuralStore(Path(temp)/"paper.json",p.ASSETS,rules)
            signal=p.FakeParente().predict(p.bars(),"BTC")
            with self.assertRaisesRegex(DataError,"minimum final-exit distance"):
                store.transaction(lambda doc:open_combined_trade(doc,"BTC",signal,accepted,p.quote(),rules,p.NOW,fx=fx(p.NOW)))
            self.assertEqual(store.snapshot()["assets"]["BTC"]["trades"],[])


if __name__ == "__main__":
    unittest.main()
