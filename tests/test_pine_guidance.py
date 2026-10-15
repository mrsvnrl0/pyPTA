"""Causal dashboard rules layered on the Pine indicator; no network or ledger."""
import copy
from dataclasses import replace
import unittest

from adaptive_crypto.core import Candle, Rules, passed
from adaptive_crypto.neural_engine import NeuralEngine
from adaptive_crypto.pine_guidance import analyse_pine_market, pine_confluence, pine_suggested_stop


class PineGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.interval = 300000
        self.now = 8 * self.interval + 1000
        self.bars = [Candle(i * self.interval, 104, 106, 103, 104, 1, self.interval) for i in range(8)]
        self.bars[-1] = Candle(7 * self.interval, 101, 105, 99, 104, 2, self.interval)
        self.quote = {"bid": 104, "ask": 104.02, "asof_ms": self.now}
        self.rules = replace(Rules(), smc_gex_targets=True)
        self.structure = {
            "source": "pine_smc", "status": "ready", "regime": "bullish",
            "asof_ms": self.bars[-1].end, "interval_ms": self.interval,
            "last_bar_ms": self.bars[-1].t, "last_closed_ms": self.bars[-1].end,
            "settings": {"min_strength": "Weak", "min_ob_score": 0},
            "events": [{"kind": "BOS", "side": "long", "known_ms": self.bars[5].end, "strength": 3}],
            "zones": [{"id": "block-1", "side": "long", "low": 98, "high": 102,
                       "created_ms": self.bars[2].t, "known_ms": self.bars[4].end,
                       "status": "tested", "touches": 1, "score": 2.4, "initial_score": 3}],
            "liquidity": [{"price": p, "side": side, "bar_ms": self.bars[1].t,
                           "known_ms": self.bars[3].end, "taken": False}
                          for p, side in [(110, "long"), (120, "long"), (90, "short")]]}
        self.profile = {"status": "ready", "coverage_complete": True, "base": "BTC", "price_currency":"USD", "exposure_currency":"USD", "spot": 104.01, "stale": False,
                        "asof_ms": self.now - 1000, "calculated_ms": self.now,
                        "expires_ms": self.now + 3600000, "regime": "positive",
                        "call_wall": {"strike": 120}, "put_wall": {"strike": 90}}

    def run_market(self, **overrides):
        arguments = dict(structure=self.structure, bars=self.bars, quote=self.quote,
                         rules=self.rules, now=self.now, symbol="BTC/USD", profile=self.profile)
        arguments.update(overrides)
        return analyse_pine_market(**arguments)

    def codes(self, result):
        return {b["code"] for b in result["guide"]["blockers"]}

    def test_completed_rejection_bracket_and_explicit_dashboard_provenance(self):
        result = self.run_market()
        self.assertIsNone(result["error"])
        self.assertEqual(result["guide"]["status"], "qualified")
        plan = result["suggestion"]
        self.assertEqual(plan["entry_price"], 104.02)
        self.assertAlmostEqual(plan["stop"], 98 * .9999)
        self.assertEqual((plan["tp1"], plan["tp2"]), (110, 120))
        self.assertIn("Dashboard logic", plan["rule"])
        self.assertEqual(plan["target_sweep_buffer_bps"], 0)
        self.assertLess(plan["stop"], plan["entry_price"])
        self.assertLess(plan["entry_price"], plan["tp1"])
        self.assertLess(plan["tp1"], plan["tp2"])
        self.assertFalse(result["watch_entries"][0]["actionable"])
        self.assertEqual(result["watch_entries"][0]["price"], 102)

    def test_earlier_failed_quality_does_not_disqualify_later_valid_plan(self):
        self.structure["settings"]["min_ob_score"] = 2
        failed = {**self.structure["zones"][0], "id": "newer-poor-quality",
                  "known_ms": self.bars[5].end, "score": .5}
        self.structure["zones"].append(failed)
        result = self.run_market()
        self.assertEqual(result["guide"]["status"], "qualified")
        self.assertEqual(result["suggestion"]["setup_key"], "block-1")
        self.assertIn("quality_below_threshold", self.codes(result))
        aggregate = result["smc_long"]["checks"]
        self.assertTrue(any(c["key"] == "pine_quality" and c["status"] == "wait" for c in aggregate))
        plan = result["suggestion"]
        self.assertTrue(passed(plan["checks"]))
        self.assertEqual([c["key"] for c in plan["checks"]], ["pine_regime", "pine_quality", "pine_rejection"])
        accepted = NeuralEngine._qualified_plan(result, self.quote, self.now)
        self.assertEqual(accepted["setup_key"], "block-1")
        self.assertEqual(accepted["checks"][1]["measured"]["live_score"], 2.4)

    def test_earlier_failed_rejection_does_not_disqualify_later_valid_plan(self):
        failed = {**self.structure["zones"][0], "id": "newer-unrejected-block",
                  "known_ms": self.bars[5].end, "low": 103, "high": 105}
        self.structure["zones"].append(failed)
        result = self.run_market()
        self.assertEqual(result["guide"]["status"], "qualified")
        self.assertEqual(result["suggestion"]["setup_key"], "block-1")
        self.assertIn("rejection_unconfirmed", self.codes(result))
        aggregate = result["smc_long"]["checks"]
        self.assertTrue(any(c["key"] == "pine_rejection" and c["status"] == "wait" for c in aggregate))
        plan = result["suggestion"]
        self.assertTrue(passed(plan["checks"]))
        self.assertEqual([c["key"] for c in plan["checks"]], ["pine_regime", "pine_quality", "pine_rejection"])
        accepted = NeuralEngine._qualified_plan(result, self.quote, self.now)
        self.assertEqual(accepted["setup_key"], "block-1")
        self.assertEqual(accepted["checks"][2]["measured"]["confirmed_before_ms"], self.bars[4].end)

    def test_identity_is_stable_across_quote_updates_and_inputs_unchanged(self):
        original = copy.deepcopy(self.structure)
        first = self.run_market()["suggestion"]
        later = self.run_market(now=self.now + 100, quote={**self.quote, "asof_ms": self.now + 100,
                                                        "bid": 104.01, "ask": 104.03})["suggestion"]
        self.assertEqual(first["key"], later["key"])
        self.assertNotEqual(first["entry_price"], later["entry_price"])
        self.assertEqual(self.structure, original)

    def test_same_bar_block_creation_cannot_count_as_prior_rejection(self):
        self.structure["zones"][0]["known_ms"] = self.bars[-1].end
        result = self.run_market()
        self.assertIsNone(result["suggestion"])
        self.assertIn("rejection_unconfirmed", self.codes(result))

    def test_live_score_decay_below_threshold_blocks_entry(self):
        self.structure["settings"]["min_ob_score"] = 2.5
        result = self.run_market()
        self.assertIsNone(result["suggestion"])
        self.assertIn("quality_below_threshold", self.codes(result))
        quality = result["structure_quality"]
        self.assertEqual((quality["initial_score"], quality["live_score"], quality["touches"]), (3, 2.4, 1))
        self.assertFalse(quality["qualifies"])

    def test_structure_grade_threshold_uses_confirmed_break(self):
        self.structure["events"][0]["strength"] = 1
        self.structure["settings"]["min_strength"] = "Strong"
        self.assertIsNone(self.run_market()["suggestion"])
        self.structure["events"].append({"kind": "BOS", "side": "long", "strength": 3, "known_ms": self.now + 1})
        self.assertIsNone(self.run_market()["suggestion"])

    def test_mitigated_block_and_neutral_regime_never_offer_entry(self):
        self.structure["zones"][0]["status"] = "mitigated"
        self.assertIsNone(self.run_market()["suggestion"])
        self.structure["zones"][0]["status"] = "tested"
        self.structure["regime"] = "neutral"
        result = self.run_market()
        self.assertIsNone(result["suggestion"])
        self.assertIn("structure_neutral", self.codes(result))

    def test_missing_gap_stale_future_and_preconfirmation_data_fail_closed(self):
        cases = [
            {"quote": None}, {"quote": {**self.quote, "asof_ms": self.now - 30000}},
            {"quote": {**self.quote, "asof_ms": self.now + 1}},
            {"quote": {**self.quote, "asof_ms": self.bars[-1].end}},
            {"bars": self.bars[:3] + self.bars[4:]},
            {"structure": {**self.structure, "last_closed_ms": self.bars[-2].end}},
            {"structure": {**self.structure, "asof_ms": self.now + 1}}]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_market(**arguments)
                self.assertIsNone(result["suggestion"])
                self.assertEqual(result["guide"]["status"], "unavailable")
                self.assertEqual(result["gex_context"]["liquidity_choices"], [])

    def test_confirmation_slot_boundary_expires_old_structure(self):
        result = self.run_market(now=9 * self.interval + 1,
                                 quote={**self.quote, "asof_ms": 9 * self.interval + 1})
        self.assertIsNone(result["suggestion"])
        self.assertEqual(result["guide"]["status"], "unavailable")

    def test_quote_cannot_reenter_block_or_cross_target(self):
        result = self.run_market(quote={**self.quote, "bid": 101.99, "ask": 102.01})
        self.assertIsNone(result["suggestion"])
        self.assertIn("rejection_not_held", self.codes(result))
        result = self.run_market(quote={**self.quote, "bid": 120, "ask": 120.02})
        self.assertIsNone(result["suggestion"])
        self.assertIn("liquidity_missing", self.codes(result))

    def test_rejection_wick_that_breached_stop_is_not_a_new_valid_plan(self):
        self.bars[-1] = Candle(7 * self.interval, 101, 105, 97, 104, 2, self.interval)
        result = self.run_market()
        self.assertIsNone(result["suggestion"])
        self.assertIn("stop_breached", self.codes(result))

    def test_unconfirmed_and_spent_liquidity_is_not_used(self):
        self.structure["liquidity"][0]["known_ms"] = self.now + 1
        result = self.run_market()
        self.assertEqual(result["suggestion"]["tp1"], 120)
        self.assertIsNone(result["suggestion"]["tp2"])
        self.bars[6] = Candle(6 * self.interval, 104, 121, 103, 104, 2, self.interval)
        result = self.run_market()
        self.assertIsNone(result["suggestion"])
        self.assertFalse(any(p["side"] == "long" for p in result["gex_context"]["liquidity_choices"]))

    def test_spread_limit_is_preserved(self):
        result = self.run_market(quote={**self.quote, "ask": 106})
        self.assertIsNone(result["suggestion"])
        self.assertIn("spread_too_wide", self.codes(result))

    def test_gex_outage_keeps_tp1_and_marks_tp2_pending(self):
        for profile in [None, {**self.profile, "status": "partial"},
                        {**self.profile, "asof_ms": self.now - 300000},
                        {**self.profile, "expires_ms": self.now},
                        {**self.profile, "base": "ETH"}, {**self.profile, "spot": 130}]:
            with self.subTest(profile=profile):
                result = self.run_market(profile=profile)
                self.assertEqual(result["suggestion"]["tp1"], 110)
                self.assertIsNone(result["suggestion"]["tp2"])
                self.assertIn("Pending", result["suggestion"]["tp2_reason"])
                self.assertFalse(result["gex_context"]["ready"])
                self.assertTrue(result["gex_context"]["liquidity_choices"])

    def test_tp2_requires_farther_liquidity_and_eligible_regime(self):
        self.profile["call_wall"]["strike"] = 110
        self.assertIsNone(self.run_market()["suggestion"]["tp2"])
        self.profile["call_wall"]["strike"] = 120
        self.profile["regime"] = "negative"
        self.assertIsNone(self.run_market()["suggestion"]["tp2"])

    def test_position_stop_uses_only_current_matching_unbreached_pine_block(self):
        position = {"side": "long", "stop_price": 70}
        value = pine_suggested_stop(position, self.structure, self.quote, self.rules, self.now)
        self.assertAlmostEqual(value, 98 * .9999)
        self.assertEqual(position["stop_price"], 70)
        self.assertIsNone(pine_suggested_stop({"side": "short"}, self.structure, self.quote, self.rules, self.now))
        self.assertIsNone(pine_suggested_stop(position, self.structure, None, self.rules, self.now))
        self.structure["zones"][0]["status"] = "mitigated"
        self.assertIsNone(pine_suggested_stop(position, self.structure, self.quote, self.rules, self.now))

    def test_confluence_retains_pine_source_and_does_not_leak_future_liquidity(self):
        self.structure["liquidity"][0]["known_ms"] = self.now + 1000
        value = pine_confluence(self.structure, self.quote, self.rules, self.profile, self.now, "BTC/USD")
        self.assertEqual(value["source"], "pine_smc")
        self.assertEqual(value["pine_structure"], self.structure)
        self.assertEqual(value["selected_ms"], self.now)
        self.assertFalse(any(p["price"] == 110 for p in value["liquidity_choices"]))
        stale = pine_confluence(self.structure, None, self.rules, self.profile, self.now, "BTC/USD")
        self.assertEqual(stale["liquidity_choices"], [])
        self.assertFalse(stale["ready"])

    def test_short_bracket_only_proposed_when_margin_mode_enabled(self):
        self.bars = [Candle(b.t, 210-b.o, 210-b.l, 210-b.h, 210-b.c, b.v, b.interval) for b in self.bars]
        self.structure["regime"] = "bearish"
        self.structure["events"][0]["side"] = "short"
        self.structure["zones"][0].update(side="short", low=108, high=112)
        self.structure["liquidity"] = [
            {**p, "side": "short" if p["side"] == "long" else "long", "price": 210-p["price"]}
            for p in self.structure["liquidity"]]
        quote = {"bid": 105.98, "ask": 106, "asof_ms": self.now}
        profile = {**self.profile, "spot": 105.99, "call_wall": {"strike": 120}, "put_wall": {"strike": 90}}
        self.assertIsNone(self.run_market(quote=quote, profile=profile)["suggestion"])
        result = self.run_market(quote=quote, profile=profile, rules=replace(self.rules, market_mode="margin"))
        plan = result["suggestion"]
        self.assertEqual(plan["side"], "short")
        self.assertGreater(plan["stop"], plan["entry_price"])
        self.assertGreater(plan["entry_price"], plan["tp1"])
        self.assertGreater(plan["tp1"], plan["tp2"])


if __name__ == "__main__":
    unittest.main()
