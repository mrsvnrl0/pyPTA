"""Causal structural target assessment, not a calibrated hit probability.

Daily bars are made only from six consecutive completed UTC 4H bars from
the selected SMC market, never the Parente training feed. Reachability FVG
annotations and DOL rules are dashboard logic layered on Perky structure.
"""
from __future__ import annotations

import copy
import math
from dataclasses import replace

from .core import Candle, DataError, H4, atr_series, finite, validate_candles
from .perky_smc import ATR_LEN, run_perky_strategy, settings_from_rules

DAY = 86400000
LABELS = {"supported":"Structurally supported", "mixed":"Mixed structural evidence",
          "weak":"Limited structural support", "unavailable":"Assessment unavailable", "off":"Reachability off"}


def completed_bias_bars(bars, minutes, now):
    """Never fill a gap or aggregate a partial/current UTC day."""
    validate_candles(bars, H4, now, 1)
    if bars[-1].end != now//H4*H4-1:
        raise DataError("Latest completed selected-market 4H candle is missing")
    if minutes == 240:
        return list(bars)
    if minutes != 1440:
        raise DataError("Unsupported reachability bias timeframe")
    groups = {}
    for bar in bars:
        start = bar.t//DAY*DAY
        groups.setdefault(start,[]).append(bar)
    result=[]
    for start,group in sorted(groups.items()):
        if start+DAY-1 >= now:
            continue
        if [b.t for b in group] != [start+i*H4 for i in range(6)]:
            if start == bars[0].t//DAY*DAY:
                continue  # only an incomplete oldest boundary is allowed
            raise DataError("Incomplete historical UTC day in reachability history")
        result.append(Candle(start,group[0].o,max(b.h for b in group),min(b.l for b in group),
                             group[-1].c,sum(b.v for b in group),DAY))
    validate_candles(result,DAY,now,1)
    if result[-1].end != now//DAY*DAY-1:
        raise DataError("Latest completed UTC daily candle is missing")
    return result


def _internal_zones(structure, bars, rules):
    """Use active Perky order blocks and track dashboard FVG lifecycle."""
    zones=[]
    end=bars[-1].end
    for zone in structure.get("zones",[]):
        if zone.get("kind") != "OB":
            continue
        if zone.get("known_ms",end+1)>end or zone.get("status") not in {"active","tested"}:
            continue
        item={k:zone[k] for k in ("id","side","low","high","known_ms")}
        item.update(kind="OB", filled=False, invalidated=False, interaction_ms=None)
        zones.append(item)
    atrs=atr_series(bars,rules.smc_reach_atr_period)
    for i in range(2,len(bars)):
        before,bar=bars[i-2],bars[i]
        side="long" if bar.l>before.h else "short" if bar.h<before.l else None
        if side is None or atrs[i] is None:
            continue
        low,high=(before.h,bar.l) if side=="long" else (bar.h,before.l)
        if high-low+1e-12 < rules.smc_reach_fvg_min_atr*atrs[i]:
            continue
        zones.append({"id":f"fvg:{bar.end}:{side}","kind":"FVG","side":side,"low":low,"high":high,
                      "known_ms":bar.end,"filled":False,"invalidated":False,"interaction_ms":None,
                      "remaining_low":low,"remaining_high":high})
    for zone in zones:
        for bar in bars:
            if bar.t<=zone["known_ms"]:
                continue
            bull=zone["side"]=="long"
            if (bar.c<zone["low"] if bull else bar.c>zone["high"]):
                zone["invalidated"]=True
                break
            if zone["kind"]=="OB":
                touched=bar.h>=zone["low"] and bar.l<=zone["high"]
            else:
                depth=rules.smc_reach_fvg_fill_pct/100
                threshold=zone["high"]-(zone["high"]-zone["low"])*depth if bull else zone["low"]+(zone["high"]-zone["low"])*depth
                touched=(bar.l<=threshold and bar.h>=zone["low"]) if bull else (bar.h>=threshold and bar.l<=zone["high"])
                zone["filled"] |= bar.l<=zone["low"] if bull else bar.h>=zone["high"]
                if bull: zone["remaining_high"]=max(zone["low"],min(zone["remaining_high"],bar.l))
                else: zone["remaining_low"]=min(zone["high"],max(zone["remaining_low"],bar.h))
            if touched:
                zone["interaction_ms"]=bar.end
    # Invalidated blocks/gaps cannot provide current evidence or a draw.
    return [z for z in zones if not z["invalidated"]]


def build_context(structure, bars, higher_bars, rules, now, symbol, error=None):
    context={"version":1,"symbol":symbol,"mode":rules.smc_reachability_mode,"observed_ms":now,
             "expires_ms":now,"status":"off" if rules.smc_reachability_mode=="off" else "unavailable",
             "timeframe_minutes":rules.smc_reach_bias_minutes,
             "source":"selected SMC market; completed candles only"}
    if context["status"]=="off":
        return context
    try:
        if error:
            raise DataError(error)
        interval=structure.get("interval_ms")
        if type(interval) is not int or interval>=rules.smc_reach_bias_minutes*60000:
            raise DataError("Choose a reachability bias timeframe higher than the SMC chart timeframe")
        validate_candles(bars,interval,now,max(3,rules.smc_reach_atr_period))
        if (structure.get("status")!="ready" or structure.get("last_closed_ms")!=bars[-1].end or
                bars[-1].end != now//interval*interval-1):
            raise DataError("Reachability requires current matching completed SMC candles")
        history=completed_bias_bars(higher_bars,rules.smc_reach_bias_minutes,now)
        need=max(2*rules.smc_reach_swing_len+3,ATR_LEN+1,rules.smc_reach_atr_period+1,rules.smc_reach_balance_bars)
        history=history[-rules.smc_reach_lookback_bars:]
        validate_candles(history,rules.smc_reach_bias_minutes*60000,now,need)
        config=replace(settings_from_rules(rules),swing_len=rules.smc_reach_swing_len)
        htf=run_perky_strategy(history,now,config)
        if htf.get("status")!="ready":
            raise DataError(htf.get("error") or "Higher-timeframe structure unavailable")
        low,high=htf.get("swing_low"),htf.get("swing_high")
        dealing={"low":low,"high":high,"equilibrium":(low+high)/2} if low and high and low<high else None
        atr=atr_series(history,rules.smc_reach_atr_period)[-1]
        if not atr or atr<=0:
            raise DataError("Higher-timeframe ATR is unavailable or zero")
        window=history[-rules.smc_reach_balance_bars:]
        balance={"low":min(b.l for b in window),"high":max(b.h for b in window)}
        inside=history[-1].h<=history[-2].h and history[-1].l>=history[-2].l
        balance.update(inside_bar=inside,span_atr=(balance["high"]-balance["low"])/atr)
        balance["balanced"]=inside or balance["span_atr"]<=rules.smc_reach_balance_atr
        earliest=max(0,bars[-1].end-rules.smc_reach_event_bars*interval)
        events=[dict(e) for e in structure.get("events",[]) if earliest<=e.get("known_ms",now+1)<=bars[-1].end]
        sweeps=[dict(e) for e in htf.get("events",[]) if e.get("kind")=="SWEEP" and earliest<=e.get("known_ms",now+1)<=bars[-1].end]
        if dealing:
            for event in events:
                level=dealing["low" if event.get("side")=="long" else "high"]
                known=max((p.get("known_ms",now+1) for p in htf.get("liquidity",[]) if p.get("price")==level),default=now+1)
                if event.get("kind")=="SWEEP" and known<=event["known_ms"] and abs(event["level"]/level-1)*10000<=rules.smc_reach_equal_bps:
                    sweeps.append(event)
        points=[dict(p) for p in structure.get("liquidity",[]) if p.get("known_ms",now+1)<=bars[-1].end]
        for point in points:
            touched=next((b for b in bars if b.t>point["bar_ms"] and
                         (b.h>=point["price"] if point["side"]=="long" else b.l<=point["price"])),None)
            if touched: point.update(taken=True,taken_ms=touched.end)
        context.update(status="ready",expires_ms=min((bars[-1].end+1)+interval,(history[-1].end+1)+history[-1].interval),
            asof_ms=bars[-1].end,bias_asof_ms=history[-1].end,bias=htf.get("regime","neutral"),
            bias_bars=len(history),atr=atr,dealing_range=dealing,balance=balance,
            internal_zones=_internal_zones(structure,bars,rules),events=events,external_sweeps=sweeps,liquidity=points,
            external_liquidity=copy.deepcopy(htf.get("liquidity",[])))
    except (DataError,ValueError,TypeError,KeyError) as exc:
        context["error"]=str(exc)[:350]
    return context


def assess_target(context, entry, target, side, rules, now, symbol=None):
    """Evidence is evaluated at current native prices; advisory never mutates targets."""
    mode=rules.smc_reachability_mode
    result={"version":1,"mode":mode,"status":"off" if mode=="off" else "unavailable",
            "label":LABELS["off" if mode=="off" else "unavailable"],"passes":False,"observed_ms":now,
            "expires_ms":now,"target":target,"side":side,"checks":[],"blockers":[],"probability":None}
    if mode=="off":
        return result
    def evidence(code,label,passed,detail,required=True):
        state="unknown" if passed is None else "pass" if passed else "caution"
        item={"code":code,"label":label,"status":state,"detail":detail,"required":required}
        result["checks"].append(item)
        if required and passed is not True:
            result["blockers"].append(code)
    try:
        if not isinstance(context,dict) or context.get("status")!="ready":
            raise DataError((context or {}).get("error") or "Waiting for selected-market higher-timeframe history")
        if (context.get("expires_ms",0)<=now or context.get("observed_ms",now+1)>now or
                context.get("asof_ms",now+1)>=now or context.get("bias_asof_ms",now+1)>=now):
            raise DataError("Reachability context is expired or future-dated")
        if symbol is not None and context.get("symbol")!=symbol:
            raise DataError("Reachability history belongs to a different currency pair")
        if side not in {"long","short"}:
            raise DataError("Unknown reachability direction")
        if entry is None: raise DataError("Waiting for a fresh price to assess the target")
        if target is None: raise DataError("No current confirmed liquidity target to assess")
        entry=finite(entry,"reachability current price",1e-15)
        target=finite(target,"reachability target",1e-15)
        sign=1 if side=="long" else -1
        points=context["liquidity"]
        known=[p for p in points if p.get("side")==side and math.isclose(p["price"],target,rel_tol=1e-12)
               and p.get("known_ms",now+1)<=context["asof_ms"]]
        available=any(not p.get("taken") for p in known)
        evidence("confirmed_target","Confirmed untaken target",available and sign*(target-entry)>0,
                 "Target is ahead at a confirmed untaken SMC swing" if available and sign*(target-entry)>0 else
                 "Target is taken, absent from current confirmed history, or no longer ahead of price")
        bias=context["bias"]
        evidence("higher_bias","Higher-timeframe bias",bias==("bullish" if side=="long" else "bearish") if bias!="neutral" else None,
                 f"{context['timeframe_minutes']}M structure is {bias}; {context['bias_bars']} completed bars",rules.smc_reach_require_bias)
        dealing=context.get("dealing_range")
        if dealing:
            low=finite(dealing["low"],"dealing-range low",1e-15)
            high=finite(dealing["high"],"dealing-range high",1e-15)
            if low>=high: raise DataError("Invalid confirmed dealing range")
        atr=finite(context["atr"],"higher-timeframe ATR",1e-15)
        location=(entry-dealing["low"])/(dealing["high"]-dealing["low"])*100 if dealing else None
        depth=location if side=="long" else 100-location if location is not None else None
        evidence("entry_location","Premium / discount alignment",0<=depth<=rules.smc_reach_entry_zone_pct+1e-9 if depth is not None else None,
                 f"Entry is {location:.1f}% up the confirmed range; favourable-edge limit {rules.smc_reach_entry_zone_pct:g}%" if location is not None else "No confirmed higher-timeframe dealing range")
        distance=sign*(target-entry)/atr
        evidence("atr_distance","Distance within configured movement envelope",0<distance<=rules.smc_reach_max_atr+1e-9,
                 f"{distance:.2f} higher-timeframe ATR to target; limit {rules.smc_reach_max_atr:g} ATR")
        balance=context["balance"]
        inside=balance["low"]<=target<=balance["high"]
        evidence("balance","Balanced market boundary",not balance["balanced"] or inside,
                 ("Balanced / inside-bar market; target is inside its observed boundary" if inside else "Balanced / inside-bar market; target is beyond its observed boundary") if balance["balanced"] else "Higher timeframe is expanding")
        zones=[z for z in context["internal_zones"] if z.get("known_ms",now+1)<=context["asof_ms"] and not z.get("invalidated")]
        remaining=[{**z,"low":z.get("remaining_low",z["low"]),"high":z.get("remaining_high",z["high"])} for z in zones if not z.get("filled")]
        internal=[z for z in remaining if sign*((z["low"] if side=="long" else z["high"])-entry)>0]
        internal.sort(key=lambda z:z["low"] if side=="long" else -z["high"])
        external=bool(dealing and (target>=dealing["high"]*(1-rules.smc_reach_equal_bps/10000) if side=="long" else target<=dealing["low"]*(1+rules.smc_reach_equal_bps/10000)))
        at_internal=any(z["low"]<=target<=z["high"] for z in remaining)
        kind="ERL" if external else "IRL zone" if at_internal else "Internal swing liquidity"
        earliest=context["asof_ms"]-rules.smc_reach_event_bars*(rules.smc_setup_minutes*60000)
        sweeps=[e for e in context["external_sweeps"] if earliest<=e["known_ms"]<=context["asof_ms"]]
        sweep=max(sweeps,key=lambda e:e["known_ms"],default=None)
        touches=[z for z in zones if z["side"]==side and z.get("interaction_ms") is not None
                 and earliest<=z["interaction_ms"]<=context["asof_ms"]
                 and (not sweep or z["interaction_ms"]>sweep["known_ms"])
                 and dealing and dealing["low"]<=z["low"]<z["high"]<=dealing["high"]]
        shifts=[e for e in context["events"] if e["side"]==side and e["kind"] in {"BOS","CHoCH"}
                and earliest<=e["known_ms"]<=context["asof_ms"]
                and any(z["interaction_ms"]<=e["known_ms"] for z in touches)]
        shift=max(shifts,key=lambda e:e["known_ms"],default=None)
        latest_break=max((e for e in context["events"] if e.get("kind") in {"BOS","CHoCH"}
                          and earliest<=e["known_ms"]<=context["asof_ms"]),key=lambda e:e["known_ms"],default=None)
        if shift and latest_break and latest_break["side"]!=side and latest_break["known_ms"]>=shift["known_ms"]:
            shift=None
        cycle="IRL → ERL" if shift and (not sweep or shift["known_ms"]>sweep["known_ms"]) else "ERL → IRL" if sweep else "Unconfirmed"
        cycle_pass=(True if cycle=="IRL → ERL" else at_internal and sweep["side"]==side if cycle=="ERL → IRL" else None)
        evidence("liquidity_cycle","Draw on liquidity cycle",cycle_pass,
                 f"{cycle}; target type {kind}. "+("Completed internal interaction followed by BOS/CHoCH" if shift and cycle=="IRL → ERL" else "External sweep favours an internal rebalance first" if sweep else "No recent confirmed internal interaction and subsequent shift"),rules.smc_reach_require_cycle)
        obstacles=[z for z in zones if z["kind"]=="OB" and z["side"]!=side and
                   (entry<z["high"] and z["low"]<target if side=="long" else target<z["high"] and z["low"]<entry)]
        evidence("opposing_blocks","Intervening opposing order blocks",not obstacles,
                 f"{len(obstacles)} active/tested opposing block(s) before the target" if obstacles else "No current opposing block lies on the path")
        pools=[p for p in points if p.get("side")==side and not p.get("taken") and p.get("known_ms",now+1)<=context["asof_ms"] and
               0<sign*(p["price"]-entry)<sign*(target-entry)]
        groups=[]
        for point in sorted(pools,key=lambda p:p["price"]):
            if groups and abs(point["price"]/groups[-1][0]["price"]-1)*10000<=rules.smc_reach_equal_bps:
                groups[-1].append(point)
            else: groups.append([point])
        equal=[group for group in groups if len({p.get('bar_ms') for p in group})>1]
        evidence("intermediate_liquidity","Intermediate liquidity / inducement",True,
                 f"{len(pools)} untaken intermediate swing pool(s); {len(equal)} equal-high/low cluster(s). Competing draws, without a probability boost",False)
        required=[c for c in result["checks"] if c["required"]]
        passed=all(c["status"]=="pass" for c in required)
        count=sum(c["status"]=="pass" for c in required)
        status="supported" if passed else "mixed" if count>=len(required)/2 else "weak"
        external_points=[p for p in context.get("external_liquidity",[]) if p.get("side")==side and not p.get("taken")
                         and p.get("known_ms",now+1)<=context["bias_asof_ms"] and sign*(p["price"]-entry)>0]
        external_points.sort(key=lambda p:sign*p["price"])
        result.update(status=status,label=LABELS[status],passes=passed,entry=entry,expires_ms=min(context["expires_ms"],now+30000),
            timeframe_minutes=context["timeframe_minutes"],bias=bias,bias_asof_ms=context["bias_asof_ms"],
            dealing_range=copy.deepcopy(dealing),range_position_pct=location,distance_atr=distance,
            target_type=kind,cycle=cycle,draw={"phase":cycle,"internal":copy.deepcopy(internal[0]) if internal else None,
            "external":copy.deepcopy(external_points[0]) if external_points else None},opposing_blocks=copy.deepcopy(obstacles),
            intermediate_pools=copy.deepcopy(pools),equal_clusters=copy.deepcopy(equal))
    except (DataError,KeyError,TypeError,ValueError) as exc:
        result.update(error=str(exc)[:350],blockers=["reachability_unavailable"])
        evidence("current_evidence","Current reachability evidence",None,result["error"])
    return result


def require_target(plan, entry, rules, now, symbol=None):
    """Optional enforcement rechecked at execution, including the actual fill."""
    if rules.smc_reachability_mode != "enforce":
        return
    reading=assess_target(plan.get("reachability_context"),entry,plan.get("exit_price"),plan.get("side"),rules,now,symbol)
    if not reading["passes"]:
        raise DataError("SMC reachability is not supported: "+", ".join(reading["blockers"]))
