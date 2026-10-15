"""Structural scenarios and workflow contracts for advisory reachability."""
import copy
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest

from adaptive_crypto.core import Candle, DataError, H4, Rules
from adaptive_crypto.smc_reachability import (DAY, assess_target, build_context, completed_bias_bars,
                                             _internal_zones, require_target)
from adaptive_crypto.settings_editor import field_groups
from adaptive_crypto.neural_engine import NeuralEngine
from adaptive_crypto.neural_ledger import NeuralStore, open_combined_trade
from adaptive_crypto.pine_alerts import monitor_pine_setups
from tests import test_pine_guidance as pine_fixture
from tests import test_combined_paper_strategy as paper
from tests.currency_fixtures import fx

NOW=100*DAY+H4+1000
STEP=1800000


def context(now=NOW, symbol="BTC/USD", target=110):
    end=now//STEP*STEP-1
    return {"version":1,"status":"ready","symbol":symbol,"observed_ms":now,"expires_ms":end+1+STEP,
        "asof_ms":end,"bias_asof_ms":now//DAY*DAY-1,"timeframe_minutes":1440,"bias_bars":60,
        "bias":"bullish","atr":10,"dealing_range":{"low":90,"high":110,"equilibrium":100},
        "balance":{"balanced":False,"inside_bar":False,"low":92,"high":108,"span_atr":1.6},
        "events":[{"kind":"BOS","side":"long","known_ms":end-STEP,"level":101}],
        "external_sweeps":[],"internal_zones":[{"id":"z","kind":"OB","side":"long","low":92,
            "high":96,"known_ms":end-5*STEP,"interaction_ms":end-2*STEP,"filled":False}],
        "liquidity":[{"price":target,"side":"long","known_ms":end-4*STEP,"bar_ms":end-6*STEP,"taken":False}]}


class ReachabilityTests(unittest.TestCase):
    def setUp(self):
        self.rules=replace(Rules(),smc_reachability_mode="advisory")
        self.ctx=context()

    def assess(self,entry=98,target=110,side="long",ctx=None,rules=None,now=NOW,symbol="BTC/USD"):
        return assess_target(self.ctx if ctx is None else ctx,entry,target,side,rules or self.rules,now,symbol)

    def test_discount_internal_rejection_then_shift_supports_external_draw(self):
        result=self.assess()
        self.assertTrue(result["passes"])
        self.assertEqual(result["status"],"supported")
        self.assertEqual(result["cycle"],"IRL → ERL")
        self.assertEqual(result["target_type"],"ERL")
        self.assertAlmostEqual(result["range_position_pct"],40)
        self.assertAlmostEqual(result["distance_atr"],1.2)
        self.assertIsNone(result["probability"])

    def test_premium_and_outside_range_entries_receive_caution(self):
        for entry in (105,89):
            with self.subTest(entry=entry):
                self.assertIn("entry_location",self.assess(entry=entry)["blockers"])
        self.assertNotIn("entry_location",self.assess(entry=100)["blockers"])

    def test_short_symmetry_uses_premium_and_bearish_bias(self):
        ctx=copy.deepcopy(self.ctx);ctx["bias"]="bearish"
        ctx["liquidity"][0].update(price=90,side="short")
        ctx["events"][0]["side"]="short"
        ctx["internal_zones"][0].update(side="short",low=104,high=108)
        r=self.assess(entry=102,target=90,side="short",ctx=ctx)
        self.assertTrue(r["passes"])
        self.assertEqual(r["cycle"],"IRL → ERL")
        self.assertAlmostEqual(r["distance_atr"],1.2)

    def test_external_sweep_prefers_internal_draw_without_assuming_external_reach(self):
        ctx=copy.deepcopy(self.ctx);ctx["external_sweeps"]=[{"side":"long","known_ms":ctx["asof_ms"],"kind":"SWEEP"}]
        ctx["internal_zones"].append({"id":"gap","kind":"FVG","side":"short","low":103,"high":105,
            "known_ms":ctx["asof_ms"]-3*STEP,"interaction_ms":None,"filled":False})
        r=self.assess(ctx=ctx)
        self.assertEqual(r["cycle"],"ERL → IRL")
        self.assertIn("liquidity_cycle",r["blockers"])
        self.assertEqual(r["draw"]["internal"]["id"],"gap")
        ctx["liquidity"][0]["price"]=104
        r=self.assess(ctx=ctx,target=104)
        self.assertEqual(r["target_type"],"IRL zone")
        self.assertNotIn("liquidity_cycle",r["blockers"])

    def test_sweep_resets_old_internal_interaction_even_if_a_later_break_occurs(self):
        ctx=copy.deepcopy(self.ctx)
        ctx["external_sweeps"]=[{"side":"long","known_ms":ctx["asof_ms"]-STEP-1,"kind":"SWEEP"}]
        self.assertEqual(self.assess(ctx=ctx)["cycle"],"ERL → IRL")
        ctx["internal_zones"][0]["interaction_ms"]=ctx["asof_ms"]-STEP
        ctx["events"][0]["known_ms"]=ctx["asof_ms"]
        self.assertEqual(self.assess(ctx=ctx)["cycle"],"IRL → ERL")

    def test_shift_before_interaction_does_not_confirm_the_cycle(self):
        ctx=copy.deepcopy(self.ctx);ctx["events"][0]["known_ms"]=ctx["asof_ms"]-3*STEP
        self.assertEqual(self.assess(ctx=ctx)["cycle"],"Unconfirmed")

    def test_later_opposing_structure_break_invalidates_old_draw_confirmation(self):
        ctx=copy.deepcopy(self.ctx)
        ctx["events"].append({"kind":"CHoCH","side":"short","known_ms":ctx["asof_ms"],"level":97})
        self.assertEqual(self.assess(ctx=ctx)["cycle"],"Unconfirmed")
        self.assertIn("liquidity_cycle",self.assess(ctx=ctx)["blockers"])

    def test_balanced_market_and_atr_envelope_are_independent_checks(self):
        ctx=copy.deepcopy(self.ctx);ctx["balance"]["balanced"]=True
        r=self.assess(ctx=ctx)
        self.assertIn("balance",r["blockers"])
        self.assertNotIn("atr_distance",r["blockers"])
        self.assertIn("atr_distance",self.assess(rules=replace(self.rules,smc_reach_max_atr=1))["blockers"])

    def test_active_opposing_block_is_a_barrier_but_invalidation_is_not(self):
        ctx=copy.deepcopy(self.ctx)
        ctx["internal_zones"].append({"id":"resistance","kind":"OB","side":"short","low":104,"high":106,
            "known_ms":ctx["asof_ms"]-3*STEP,"interaction_ms":None,"filled":False})
        self.assertIn("opposing_blocks",self.assess(ctx=ctx)["blockers"])
        ctx["internal_zones"][-1]["invalidated"]=True
        self.assertNotIn("opposing_blocks",self.assess(ctx=ctx)["blockers"])

    def test_intermediate_equal_pools_do_not_override_failures(self):
        ctx=copy.deepcopy(self.ctx);ctx["bias"]="bearish"
        for i,price in enumerate((103,103.05)):
            ctx["liquidity"].append({**ctx["liquidity"][0],"price":price,"bar_ms":ctx["asof_ms"]-(i+3)*STEP})
        r=self.assess(ctx=ctx)
        self.assertIn("higher_bias",r["blockers"])
        self.assertEqual(len(r["equal_clusters"]),1)
        self.assertEqual(len(r["intermediate_pools"]),2)
        self.assertFalse(r["checks"][-1]["required"])
        self.assertIsNone(r["probability"])

    def test_missing_stale_future_mismatched_and_zero_inputs_stay_unavailable(self):
        mutations=[{}, {**self.ctx,"expires_ms":NOW}, {**self.ctx,"observed_ms":NOW+1},
                   {**self.ctx,"asof_ms":NOW}, {**self.ctx,"bias_asof_ms":NOW},
                   {**self.ctx,"symbol":"BTC/USDC"}]
        for ctx in mutations:
            with self.subTest(ctx=ctx): self.assertEqual(self.assess(ctx=ctx)["status"],"unavailable")
        self.assertEqual(self.assess(entry=None)["status"],"unavailable")
        for ctx in ({**self.ctx,"atr":0},{**self.ctx,"atr":float("nan")},
                    {**self.ctx,"dealing_range":{"low":100,"high":100,"equilibrium":100}}):
            self.assertEqual(self.assess(ctx=ctx)["status"],"unavailable")

    def test_taken_unconfirmed_and_wrong_direction_targets_fail(self):
        ctx=copy.deepcopy(self.ctx);ctx["liquidity"][0]["taken"]=True
        self.assertIn("confirmed_target",self.assess(ctx=ctx)["blockers"])
        ctx["liquidity"][0].update(taken=False,known_ms=NOW+1)
        self.assertIn("confirmed_target",self.assess(ctx=ctx)["blockers"])
        self.assertIn("confirmed_target",self.assess(target=97)["blockers"])

    def test_optional_requirements_and_off_mode(self):
        ctx=copy.deepcopy(self.ctx);ctx["bias"]="neutral";ctx["events"]=[]
        r=self.assess(ctx=ctx,rules=replace(self.rules,smc_reach_require_bias=False,smc_reach_require_cycle=False))
        self.assertTrue(r["passes"])
        self.assertEqual(self.assess(ctx={},rules=Rules())["status"],"off")

    def test_fvg_is_known_only_on_third_close_and_ob_closed_through_is_not_mitigation(self):
        rules=replace(self.rules,smc_reach_atr_period=1,smc_reach_fvg_min_atr=0)
        bars=[Candle(i*STEP,100,101,99,100,1,STEP) for i in range(2)]
        bars.append(Candle(2*STEP,104,105,103,104,1,STEP))
        bars.append(Candle(3*STEP,103,104,101.5,103,1,STEP))
        structure={"zones":[{"id":"invalid","side":"long","low":95,"high":99,"known_ms":bars[0].end,"status":"mitigated"}]}
        before=_internal_zones(structure,bars[:2],rules)
        self.assertEqual(before,[])
        gap=next(z for z in _internal_zones(structure,bars,rules) if z["known_ms"]==bars[2].end)
        self.assertEqual((gap["low"],gap["high"]),(101,103))
        self.assertEqual(gap["interaction_ms"],bars[3].end)
        deeper=replace(rules,smc_reach_fvg_fill_pct=100)
        gap=next(z for z in _internal_zones(structure,bars,deeper) if z["known_ms"]==bars[2].end)
        self.assertIsNone(gap["interaction_ms"])
        bars.append(Candle(4*STEP,100,101,99,100,1,STEP))
        self.assertFalse(any(z["known_ms"]==bars[2].end for z in _internal_zones(structure,bars,rules)))

    def test_daily_aggregation_discards_partial_days_and_never_fills_gaps(self):
        bars=[Candle(i*H4,100+i,102+i,99+i,101+i,i+1,H4) for i in range(15)]
        daily=completed_bias_bars(bars,1440,15*H4+1000)
        self.assertEqual(len(daily),2)
        self.assertEqual((daily[0].o,daily[0].h,daily[0].l,daily[0].c,daily[0].v),(100,107,99,106,21))
        self.assertEqual(len(completed_bias_bars(bars[2:],1440,15*H4+1000)),1)
        with self.assertRaises(DataError): completed_bias_bars(bars[:7]+bars[8:],1440,15*H4+1000)
        with self.assertRaises(DataError): completed_bias_bars(bars[:-1],1440,15*H4+1000)
        self.assertEqual(completed_bias_bars(bars,240,15*H4+1000),bars)

    def test_future_forming_daily_candle_is_never_consumed(self):
        bars=[Candle(i*H4,100,103,99,101,1,H4) for i in range(24)]
        result=completed_bias_bars(bars[:21],1440,21*H4+1000)
        self.assertEqual([b.t for b in result],[0,DAY,2*DAY])
        with self.assertRaises(DataError): completed_bias_bars(bars,1440,21*H4+1000)

    def test_bias_history_uses_selected_symbol_and_needs_a_higher_timeframe(self):
        result=build_context({},[],[],self.rules,NOW,"SOL/USDC")
        self.assertEqual(result["symbol"],"SOL/USDC")
        self.assertEqual(result["status"],"unavailable")
        bad=build_context({"interval_ms":H4},[],[],replace(self.rules,smc_reach_bias_minutes=240),NOW,"SOL/USDC")
        self.assertIn("higher than",bad["error"])

    def test_advisory_qualification_and_targets_match_disabled_mode(self):
        fixture=pine_fixture.PineGuidanceTests();fixture.setUp()
        original=fixture.run_market()
        rules=replace(fixture.rules,smc_reachability_mode="advisory")
        result=fixture.run_market(rules=rules,reachability_context={"status":"unavailable","error":"Test outage"})
        self.assertEqual(result["guide"],original["guide"])
        self.assertEqual(result["suggestion"]["checks"],original["suggestion"]["checks"])
        for key in ("id","entry_price","stop","tp1","tp2","exit_price","expires_ms"):
            self.assertEqual(result["suggestion"][key],original["suggestion"][key])
        self.assertEqual(result["suggestion"]["reachability"]["status"],"unavailable")
        self.assertIsNotNone(NeuralEngine._qualified_plan(result,fixture.quote,fixture.now,0,rules))
        holdings={"outbox":[]};monitor_pine_setups(holdings,"BTC","BTC/USD",result,fixture.now)
        self.assertIn("Target reachability (advisory)",holdings["outbox"][0]["text"])

    def test_require_mode_blocks_missing_evidence_but_advisory_does_not(self):
        fixture=pine_fixture.PineGuidanceTests();fixture.setUp()
        result=fixture.run_market(rules=replace(fixture.rules,smc_reachability_mode="enforce"))
        self.assertIsNone(result["suggestion"])
        self.assertIn("reachability_not_supported",fixture.codes(result))
        require_target({},None,self.rules,NOW)
        with self.assertRaises(DataError): require_target({},98,replace(self.rules,smc_reachability_mode="enforce"),NOW)

    def test_require_rechecks_actual_price_and_stale_context_without_mutation(self):
        rules=replace(self.rules,smc_reachability_mode="enforce")
        plan={"exit_price":110,"side":"long","reachability_context":self.ctx}
        saved=copy.deepcopy(plan)
        require_target(plan,98,rules,NOW,"BTC/USD")
        with self.assertRaisesRegex(DataError,"entry_location"): require_target(plan,101,rules,NOW,"BTC/USD")
        with self.assertRaises(DataError): require_target(plan,98,rules,self.ctx["expires_ms"],"BTC/USD")
        self.assertEqual(plan,saved)

    def test_actual_paper_fill_uses_enforcement_but_advisory_outage_can_open(self):
        plan=paper.market(tp1=102,tp2=103)["suggestion"]
        plan["reachability_context"]={"status":"unavailable","error":"outage"}
        signal=paper.FakeParente().predict(paper.bars(),"BTC")
        with tempfile.TemporaryDirectory() as folder:
            rules=replace(paper.RULES,smc_reachability_mode="advisory")
            store=NeuralStore(Path(folder)/"advisory.json",paper.ASSETS,rules)
            store.transaction(lambda doc:open_combined_trade(doc,"BTC",signal,plan,paper.quote(),rules,paper.NOW,fx=fx(paper.NOW)))
            self.assertEqual(len(store.snapshot()["assets"]["BTC"]["trades"]),1)
            rules=replace(rules,smc_reachability_mode="enforce")
            store=NeuralStore(Path(folder)/"enforce.json",paper.ASSETS,rules)
            with self.assertRaisesRegex(DataError,"reachability"):
                store.transaction(lambda doc:open_combined_trade(doc,"BTC",signal,plan,paper.quote(),rules,paper.NOW,fx=fx(paper.NOW)))
            self.assertEqual(store.snapshot()["assets"]["BTC"]["trades"],[])

    def test_context_runs_confirmed_higher_structure_on_the_selected_market_history(self):
        import math
        now=71*DAY+H4+1000
        higher=[]
        for i in range(71*6+1):
            value=100+5*math.sin((i//6)*math.pi/7)+(i//6)*.05
            higher.append(Candle(i*H4,value,value+2,value-2,value+.1,1,H4))
        end=now//STEP*STEP
        lower=[Candle(end-(100-i)*STEP,100,102,98,101,1,STEP) for i in range(100)]
        structure={"status":"ready","interval_ms":STEP,"last_closed_ms":lower[-1].end,"events":[],"liquidity":[],"zones":[]}
        c=build_context(structure,lower,higher,self.rules,now,"SOL/USDC")
        self.assertEqual(c["status"],"ready",c.get("error"))
        self.assertEqual(c["symbol"],"SOL/USDC")
        self.assertEqual(c["bias_bars"],60)
        self.assertEqual(c["bias_asof_ms"],71*DAY-1)
        self.assertIsNotNone(c["dealing_range"])
        old=c["bias"]
        higher[-1]=Candle(higher[-1].t,500,600,400,550,1,H4)
        c=build_context(structure,lower,higher,self.rules,now,"SOL/USDC")
        self.assertEqual(c["bias"],old)  # current UTC day cannot change completed daily bias

    def test_partially_filled_fvg_exposes_only_its_remaining_internal_draw(self):
        ctx=copy.deepcopy(self.ctx)
        ctx["external_sweeps"]=[{"side":"long","known_ms":ctx["asof_ms"],"kind":"SWEEP"}]
        ctx["internal_zones"].append({"id":"gap","kind":"FVG","side":"short","low":101,"high":105,
            "remaining_low":103,"remaining_high":105,"known_ms":ctx["asof_ms"]-3*STEP,"interaction_ms":None,"filled":False})
        ctx["liquidity"][0]["price"]=102
        r=self.assess(ctx=ctx,target=102)
        self.assertEqual(r["target_type"],"Internal swing liquidity")
        self.assertEqual(r["draw"]["internal"]["low"],103)
        self.assertIn("liquidity_cycle",r["blockers"])

    def test_advisory_pages_render_and_snapshot_keeps_saved_positions_unchanged(self):
        from unittest.mock import patch
        from tests import test_web_and_ui as web_fixture
        from tests.ui_helpers import DashboardHTML
        fixture=web_fixture.WebAndUiTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        runtime=fixture.runtime
        runtime.rules=replace(runtime.rules,smc_reachability_mode="advisory")
        runtime.engine.rules=runtime.rules
        fixture.settings.write_text(json.dumps({"assets":[{"name":"BTC","symbol":"BTC/USDC","enabled":True,"price_decimals":2}],
            "strategy":asdict(runtime.rules),"refresh_seconds":15}))
        runtime.scan_once()
        pos=runtime.positions.open_position(runtime.assets,"BTC","long",100,1,"f"*32,web_fixture.NOW)[0]
        runtime.positions.set_stop(pos["id"],95,web_fixture.NOW)
        before=runtime.positions.snapshot()
        with patch("adaptive_crypto.web.time.time",return_value=web_fixture.NOW/1000):
            for route in ("/","/settings","/positions","/paper-trading","/smc-chart"):
                response=fixture.client.get(route)
                self.assertEqual(response.status_code,200,route)
                parsed=DashboardHTML(response.get_data(as_text=True))
                self.assertFalse(parsed.errors,(route,parsed.errors))
            position_page=fixture.client.get("/positions").get_data(as_text=True)
            self.assertIn("Target reachability (advisory)",position_page)
            settings_page=fixture.client.get("/settings").get_data(as_text=True)
            self.assertIn('data-rule="smc_reachability_mode"',settings_page)
            self.assertIn('value="advisory" selected',settings_page)
        self.assertEqual(runtime.positions.snapshot(),before)
        self.assertTrue(all(pair=="BTC/USDC" for pair,_ in fixture.provider.requested))

    def test_settings_expose_all_controls_and_reject_invalid_contracts(self):
        fields=field_groups({"strategy":asdict(self.rules)})["SMC target reachability"]
        self.assertEqual({f["key"] for f in fields},{k for k in asdict(self.rules) if k.startswith("smc_reach")})
        with self.assertRaisesRegex(DataError,"higher than"):
            replace(self.rules,smc_setup_minutes=240,smc_reach_bias_minutes=240).validate()
        bad=[("smc_reachability_mode","magic"),("smc_reach_bias_minutes",30),("smc_reach_event_bars",True),
             ("smc_reach_entry_zone_pct",0),("smc_reach_fvg_fill_pct",101),("smc_reach_max_atr",0),
             ("smc_reach_equal_bps",501),("smc_reach_require_bias",1),("smc_reach_lookback_bars",15)]
        for key,value in bad:
            with self.subTest(key=key),self.assertRaises(DataError):
                replace(self.rules,**{key:value},**({"smc_reach_swing_len":10} if key=="smc_reach_lookback_bars" else {})).validate()


if __name__=="__main__": unittest.main()
