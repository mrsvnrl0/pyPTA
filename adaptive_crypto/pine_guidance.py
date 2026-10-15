"""Perky SMC strategy plans and completed-candle market context."""
import copy
import hashlib

from .core import DataError, check, exit_distance_pct, finite, validate_candles
from .take_profit_alerts import fresh_quote
from .gex_currency import profile_for_market, require_market_currency

SOURCE = "pine_smc"
MAX_GEX_AGE = 300000
RULE_LABEL = "Perky SMC Ultimate V5 configured Filtered mode on completed candles"
QUALITY_EXIT_NOTE = "Perky's confluence score is contextual evidence, not a probability or an automatic stop adjustment."


def _stamp(value, label):
    if type(value) is not int or value < 0:
        raise DataError("Invalid " + label)
    return value


def _completed_structure(structure, now):
    if not isinstance(structure, dict) or structure.get("status") != "ready":
        raise DataError("Waiting for Perky SMC's completed structure history")
    interval = _stamp(structure.get("interval_ms"), "SMC interval")
    if interval <= 0:
        raise DataError("Invalid SMC interval")
    end = _stamp(structure.get("last_closed_ms"), "SMC completed candle")
    if end != now // interval * interval - 1:
        raise DataError("Waiting for the latest completed Perky SMC candle")
    if structure.get("last_bar_ms") != end - interval + 1:
        raise DataError("Perky SMC candle timestamps disagree")
    asof = _stamp(structure.get("asof_ms"), "SMC observation")
    if not end <= asof <= now:
        raise DataError("Perky SMC observation is not causal")
    return interval, end


def current_pine_history(structure, bars, now):
    """Check that the authoritative snapshot matches current, completed source bars."""
    try:
        interval, end = _completed_structure(structure, now)
        validated = validate_candles(bars, interval, now, 1)
        return bool(validated and validated[-1].end == end)
    except (DataError, ValueError, TypeError, KeyError, AttributeError):
        return False


def _current(structure, quote, now):
    interval, end = _completed_structure(structure, now)
    current = fresh_quote(quote, now)
    if not current or not end < current["asof_ms"] <= now or current["asof_ms"] + 30000 <= now:
        raise DataError("Waiting for a fresh selected-pair price after the SMC confirmation")
    return current, interval, end


def _settings(structure):
    settings = structure.get("settings") or {}
    minimum = finite(settings.get("min_score", 4), "minimum Perky score", 0)
    if minimum > 10:
        raise DataError("Invalid Perky minimum score")
    return minimum


def _events(structure, now):
    return [e for e in structure.get("events", [])
            if e.get("kind") in {"BOS", "CHoCH"}
            and type(e.get("known_ms")) is int and e["known_ms"] <= now
            and e.get("side") in {"long", "short"}]


def _zones(structure, side, now):
    zones = []
    for zone in structure.get("zones", []):
        if zone.get("kind") != "OB" or zone.get("side") != side or zone.get("status") != "active":
            continue
        known = _stamp(zone.get("known_ms"), "order-block confirmation")
        created = _stamp(zone.get("created_ms"), "order-block origin")
        if known > now or created > known:
            continue
        low = finite(zone.get("low"), "block low", 1e-15)
        high = finite(zone.get("high"), "block high", 1e-15)
        if low > high:
            raise DataError("Invalid Perky order-block bounds")
        zones.append(zone)
    return sorted(zones, key=lambda z: z["known_ms"], reverse=True)


def _quality(structure, zone=None):
    perky = structure.get("perky") or structure
    signal = perky.get("signal") or {}
    events = _events(structure, structure.get("last_closed_ms", 0))
    event = max(events, key=lambda item: item["known_ms"], default=None)
    minimum = _settings(structure)
    return {"source": SOURCE, "break_label": event.get("kind") if event else "Pending",
            "break_ms": event.get("known_ms") if event else None,
            "live_score": signal.get("score"), "minimum_score": minimum,
            "qualifies": bool(signal and signal.get("score", 0) >= minimum),
            "note": "Perky SMC score and structure are contextual evidence, not a probability of profit.",
            "exit_note": QUALITY_EXIT_NOTE}


def _choices(structure, quote, now, bars=()):
    choices, seen = [], set()
    for point in structure.get("liquidity", []):
        if point.get("taken") or point.get("side") not in {"long", "short"}:
            continue
        stamp = _stamp(point.get("bar_ms"), "liquidity pivot")
        known = _stamp(point.get("known_ms"), "liquidity confirmation")
        if not stamp <= known <= structure["last_closed_ms"] or known > now:
            continue
        price = finite(point.get("price"), "liquidity price", 1e-15)
        long = point["side"] == "long"
        if not (price > quote["ask"] if long else price < quote["bid"]):
            continue
        if any((b.h >= price if long else b.l <= price) for b in bars if b.t > stamp and b.end <= now):
            continue
        identity = (point["side"], stamp, price)
        if identity in seen:
            continue
        seen.add(identity)
        choices.append({"side": point["side"], "price": price, "bar_ms": stamp, "known_ms": known})
    return sorted(choices, key=lambda p: abs(p["price"] - (quote["bid"] + quote["ask"]) / 2))


def pine_confluence(structure, quote, rules, profile, now, symbol, *, bars=()):
    """Expose Perky confirmed-liquidity/GEX context without changing its targets."""
    result = {"source": SOURCE, "pine_structure": copy.deepcopy(structure or {}),
              "enabled": rules.smc_gex_targets, "ready": False,
              "selected_ms": now, "alignment_bps": rules.smc_gex_alignment_bps,
              "liquidity_choices": [], "pois": [], "matches": [], "regime": None,
              "reason": "SMC + GEX target guidance disabled"}
    try:
        quote, interval, end = _current(structure, quote, now)
        choices = _choices(structure, quote, now, bars)
        result["liquidity_choices"] = choices
        result["pois"] = [{"kind": "bsl" if p["side"] == "long" else "ssl",
                           "label": "SMC Engine confirmed untaken liquidity", "price": p["price"],
                           "pivot_ms": p["bar_ms"], "known_ms": p["known_ms"], "side": p["side"]}
                          for p in choices]
        if not rules.smc_gex_targets:
            return result
        if (not isinstance(profile, dict) or profile.get("status") != "ready" or profile.get("stale")
                or profile.get("coverage_complete") is not True):
                    raise DataError("GEX unavailable, incomplete or stale; Perky liquidity remains available")
        stamp = _stamp(profile.get("asof_ms"), "GEX observation")
        calculated = _stamp(profile.get("calculated_ms"), "GEX calculation")
        expiry = _stamp(profile.get("expires_ms"), "GEX expiry")
        if not 0 <= now - stamp < MAX_GEX_AGE or not 0 <= now - calculated < MAX_GEX_AGE or expiry <= now:
            raise DataError("GEX expired or future-dated; Perky liquidity remains available")
        profile = profile_for_market(profile, symbol, now)
        fx_deadline = require_market_currency(profile, symbol, now)
        spot = finite(profile.get("spot"), "GEX index", 1e-15)
        basis = abs(spot / ((quote["bid"] + quote["ask"]) / 2) - 1) * 10000
        result["basis_bps"] = basis
        if basis > rules.smc_gex_max_basis_bps:
            raise DataError("Options/spot basis exceeds the configured limit")
        regime = profile.get("regime")
        if regime not in {"positive", "negative", "neutral"}:
            raise DataError("Invalid GEX regime")
        result.update(ready=True, regime=regime, price_currency=profile["price_currency"],
                      price_conversion=copy.deepcopy(profile.get("price_conversion")), gex_asof_ms=stamp,
                      valid_until_ms=int(min(stamp + MAX_GEX_AGE, calculated + MAX_GEX_AGE,
                                             expiry, quote["asof_ms"] + 30000, end + 1 + interval,
                                             fx_deadline if fx_deadline is not None else expiry)),
                      reason="No eligible GEX alignment with confirmed Perky liquidity")
        for poi in result["pois"]:
            key = "call" if poi["side"] == "long" else "put"
            wall = profile.get(key + "_wall") or {}
            if wall.get("strike") is None:
                continue
            level = finite(wall["strike"], "GEX wall", 1e-15)
            aligned = abs(poi["price"] / level - 1) * 10000 <= rules.smc_gex_alignment_bps + 1e-9
            if aligned:
                eligible = regime == "positive"
                result["matches"].append({"key": key, "gex_price": level, "poi": copy.deepcopy(poi),
                    "eligible": eligible, "reason": "Positive-gamma wall aligns with Perky liquidity" if eligible
                    else "Wall alignment is context only outside positive gamma"})
        if any(m["eligible"] for m in result["matches"]):
            result["reason"] = "Eligible GEX alignment with confirmed untaken Perky liquidity"
    except (DataError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        result.update(ready=False, reason=str(exc), matches=[])
    return result


def pine_suggested_stop(position, structure, quote, rules, now):
    """Nearest unbreached Perky block; suggestions never move saved stops."""
    try:
        quote, _, _ = _current(structure, quote, now)
        side = position.get("side", "long")
        if side not in {"long", "short"}:
            return None
        if structure.get("regime") != ("bullish" if side == "long" else "bearish"):
            return None
        stops = []
        for zone in _zones(structure, side, now):
            edge = zone["low" if side == "long" else "high"]
            stop = edge * (1 + (-1 if side == "long" else 1) * rules.smc_stop_buffer_bps / 10000)
            if 0 < stop and (stop < quote["bid"] if side == "long" else stop > quote["ask"]):
                stops.append(stop)
        return (max(stops) if side == "long" else min(stops)) if stops else None
    except (DataError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        return None


def analyse_pine_market(structure, bars, quote, rules, now, symbol, profile=None, *, reachability_context=None):
    """Return current Perky SMC signals and source-derived brackets."""
    history = {"minutes": bars[-1].interval // 60000 if bars else None, "bars": len(bars),
               "first_ms": bars[0].t if bars else None, "last_ms": bars[-1].t if bars else None}
    result = {"source": SOURCE, "symbol": symbol, "asof_ms": now, "quote": None, "structure": copy.deepcopy(structure or {}),
              "structure_quality": {}, "smc_long": None, "smc_short": None,
              "perky_strategy": copy.deepcopy((structure or {}).get("perky") or {}),
              "suggestion": None, "suggestions": [], "watch_entries": [], "momentum": None,
              "nn": {"signal": None, "error": "Neural signals are shown with paper positions", "expires_ms": now},
              "entry_conditions": RULE_LABEL, "quality_exit_note": QUALITY_EXIT_NOTE,
              "liquidity": {"status": "unavailable", "error": None, "reference_price": None,
                            "nearest_above": None, "highest_above": None, "nearest_below": None,
                            "above_count": 0, "below_count": 0, "asof_ms": now},
              "guide": {"status": "blocked", "blockers": [], "expires_ms": now + 30000,
                        "rule": RULE_LABEL, "history": {"setup": history, "entry": history}}}
    from .smc_reachability import assess_target
    result["reachability_context"] = copy.deepcopy(reachability_context)
    result["reachability"] = {side: assess_target(reachability_context, None, None, side, rules, now, symbol) for side in ("long","short") }
    guide = result["guide"]
    def block(side, code, message):
        guide["blockers"].append({"side": side, "code": code, "message": message})
    for side in ("long", "short"):
        result["smc_" + side] = {"source": SOURCE, "status": "WAITING FOR A QUALIFIED PERKY SIGNAL",
                               "setup": None, "entries": [], "checks": []}
    result["gex_context"] = pine_confluence(structure, quote, rules, profile, now, symbol, bars=bars)
    try:
        quote, interval, end = _current(structure, quote, now)
        validate_candles(bars, interval, now, 2)
        if bars[-1].end != end:
            raise DataError("SMC structure and completed chart candles do not match")
        result["quote"] = {**quote, "last": (quote["bid"] + quote["ask"]) / 2}
        guide["expires_ms"] = int(min(quote["asof_ms"] + 30000, end + 1 + interval))
        result["structure_quality"] = _quality(structure)
        choices = _choices(structure, quote, now, bars)
        result["gex_context"]["liquidity_choices"] = copy.deepcopy(choices)
        above = sorted([p for p in choices if p["side"] == "long"], key=lambda p: p["price"])
        below = sorted([p for p in choices if p["side"] == "short"], key=lambda p: p["price"], reverse=True)
        result["liquidity"].update(status="ready", reference_price=(quote["bid"] + quote["ask"]) / 2,
            nearest_above=above[0] if above else None, highest_above=above[-1] if above else None,
            nearest_below=below[0] if below else None, above_count=len(above), below_count=len(below))
        perky = structure.get("perky")
        if not isinstance(perky, dict) or perky.get("status") != "ready":
            raise DataError("Waiting for Perky SMC strategy history")
        if perky.get("last_closed_ms") != end or perky.get("interval_ms") != interval:
            raise DataError("Perky strategy and completed chart candles do not match")
        current_candidates = perky.get("signals", [])
        current_signals = perky.get("qualified_signals", [])
        for side in ("long", "short"):
            strategy = result["smc_" + side]
            current = next((item for item in current_signals if item.get("side") == side), None)
            candidate = current or next((item for item in current_candidates if item.get("side") == side), None)
            strategy["setup"] = copy.deepcopy(candidate)
            signal_check = check("perky_signal", "Current completed Perky entry signal is qualified",
                bool(current), candidate.get("kind") if candidate else "No signal on latest completed candle",
                "Sweep, CHoCH or momentum signal with Filtered-mode gates", end)
            strategy["checks"].append(signal_check)
            if candidate:
                score_check = check("perky_score", "Perky confluence score meets the configured minimum",
                    candidate.get("score", 0) >= candidate.get("minimum_score", 4),
                    {"score": candidate.get("score"), "components": candidate.get("score_parts"),
                     "htf_agreement": candidate.get("htf_agreement")}, candidate.get("minimum_score", 4), end)
                strategy["checks"].append(score_check)
                if not current:
                    if candidate.get("score", 0) < candidate.get("minimum_score", 4):
                        block(side, "perky_score_below_minimum", f"Current Perky signal is below the {candidate.get('minimum_score', 4)}/10 confluence threshold")
                    elif not candidate.get("cooldown_allowed"):
                        cooldown = (perky.get("settings") or {}).get("cooldown_bars", 25)
                        block(side, "perky_cooldown", f"Waiting for Perky's {cooldown}-bar cooldown or five-bar expansion re-entry")
                    else:
                        block(side, "perky_gates", "Perky's Filtered-mode signal gates did not qualify this entry")
                    strategy["status"] = "WAITING FOR PERKY FILTERED-MODE QUALIFICATION"
                    continue
            else:
                block(side, "perky_signal_missing", "No Perky sweep, internal CHoCH or momentum entry fired on the latest completed candle")
                strategy["status"] = "WAITING FOR A CURRENT PERKY SIGNAL"
                continue
            long = side == "long"
            price = quote["ask" if long else "bid"]
            stop_mode = (perky.get("settings") or {}).get("stop_mode", "Structure")
            stop = finite(current.get("stop"), "Perky stop", 1e-15)
            targets = [finite(level, "Perky target", 1e-15) for level in current.get("targets", [])]
            if len(targets) != 3:
                block(side, "perky_targets_missing", "Perky did not produce all three structural targets")
                strategy["status"] = "WAITING FOR PERKY BRACKET DATA"
                continue
            tp1, tp2, tp3 = targets
            valid = (0 < stop < price < tp1 < tp2 < tp3) if long else (0 < tp3 < tp2 < tp1 < price < stop)
            bracket_check = check("perky_bracket", "Perky stop, current executable entry and three targets form an ordered bracket",
                valid, {"entry": price, "stop": stop, "tp1": tp1, "tp2": tp2, "tp3": tp3},
                stop_mode + " stop and sorted structural targets", end)
            strategy["checks"].append(bracket_check)
            if not valid:
                block(side, "perky_bracket_invalid", "Current quote is outside Perky's ordered stop and three-target bracket")
                strategy["status"] = "WAITING FOR A VALID PERKY BRACKET"
                continue
            spread = (quote["ask"] / quote["bid"] - 1) * 10000
            spread_check = check("perky_spread", "Executable spread is within the configured maximum",
                                 spread <= rules.max_spread_bps, spread, rules.max_spread_bps)
            strategy["checks"].append(spread_check)
            if spread_check["status"] != "pass":
                block(side, "spread_too_wide", "The executable spread exceeds the configured maximum")
                strategy["status"] = "WAITING FOR A TIGHTER SPREAD"
                continue
            distance = exit_distance_pct(price, tp3, side)
            if rules.smc_min_exit_distance_pct > 0:
                distance_check = check("perky_exit_distance", "Perky TP3 meets the configured minimum distance",
                    distance + 1e-9 >= rules.smc_min_exit_distance_pct,
                    {"entry": price, "exit": tp3, "distance_pct": distance}, rules.smc_min_exit_distance_pct)
                strategy["checks"].append(distance_check)
                if distance_check["status"] != "pass":
                    block(side, "exit_distance_below_minimum", "Perky TP3 is below the configured minimum final-exit distance")
                    strategy["status"] = "WAITING FOR A FARTHER PERKY TP3"
                    continue
            target_reading = assess_target(reachability_context, price, tp3, side, rules, now, symbol)
            if rules.smc_reachability_mode == "enforce":
                reach_check = check("perky_reachability", "Perky TP3 has structural reachability support",
                    target_reading["passes"], target_reading["label"], "All required reachability checks pass")
                strategy["checks"].append(reach_check)
                if not target_reading["passes"]:
                    block(side, "reachability_not_supported", "Final target reachability: " + ", ".join(target_reading["blockers"]))
                    strategy["status"] = "WAITING FOR TARGET REACHABILITY"
                    continue
            quality = {"source": SOURCE, "score": current["score"], "score_parts": copy.deepcopy(current["score_parts"]),
                       "minimum_score": current["minimum_score"], "mode": "Filtered",
                       "note": "Perky SMC confluence score, not a probability of profit."}
            selection = {"source": SOURCE, "method": "perky_smc", "reason": "Perky SMC V5 sorted structural targets",
                         "matches": [], "selected_ms": now, "nearest_smc_price": tp1, "liquidity_price": tp1}
            identity = f"perky-v5:{symbol}:{side}:{current['signal_end']}:{current['kind']}"
            key = "perky:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            plan_checks = copy.deepcopy(strategy["checks"])
            kind_label = {"SWEEP": "liquidity sweep", "CHoCH": "internal CHoCH", "MOMENTUM": "momentum shift"}.get(current["kind"], current["kind"])
            plan = {"key": key, "id": key, "setup_key": key, "source": SOURCE, "side": side,
                    "method": "Perky " + kind_label, "rule": RULE_LABEL, "execution": "market",
                    "limit": price, "entry_price": price, "signal_entry_price": current["signal_price"],
                    "stop": stop, "stop_basis": "perky_atr" if stop_mode == "ATR" else "perky_structure", "target": tp1,
                    "target_ms": current["signal_end"], "target_liquidity_price": tp1,
                    "target_sweep_buffer_bps": 0, "target_selection": selection,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3, "tp2_reason": "Perky SMC V5 structural target 2",
                    "tp2_matches": [], "exit_price": tp3,
                    "reachability": target_reading, "reachability_context": copy.deepcopy(reachability_context),
                    "minimum_exit_distance_pct": rules.smc_min_exit_distance_pct, "exit_distance_pct": distance,
                    "nearest_target": tp1, "signal_ms": current["signal_ms"], "signal_end": current["signal_end"],
                    "signal_kind": current["kind"], "score": current["score"],
                    "expires_ms": guide["expires_ms"], "quality": quality, "setup": copy.deepcopy(current),
                    "gross_r": abs(tp3 - price) / abs(price - stop), "checks": plan_checks}
            strategy["entries"].append(plan)
            strategy["status"] = "PERKY FILTERED-MODE SIGNAL QUALIFIED"
            result["suggestions"].append(plan)
        eligible = [p for p in result["suggestions"] if p["side"] == "long" or rules.market_mode == "margin"]
        if eligible:
            result["suggestion"] = max(eligible, key=lambda p: (p["quality"]["score"], p["signal_end"]))
            guide["status"] = "qualified"
        elif result["suggestions"] and rules.market_mode == "spot":
            block("short", "spot_short_context_only", "A bearish Perky plan is context in spot mode; a short position is not proposed")
        for side in ("long", "short"):
            candidate = next((p for p in result["suggestions"] if p["side"] == side), None)
            points = above if side == "long" else below
            target = candidate["exit_price"] if candidate else points[0]["price"] if points else None
            price = result["quote"]["ask" if side == "long" else "bid"]
            result["reachability"][side] = assess_target(reachability_context, price, target, side, rules, now, symbol)
            result["reachability"][side]["scope"] = "Qualified final exit" if candidate else "Nearest preliminary liquidity; no qualified plan"
        result["error"] = None
    except (DataError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        result["error"] = str(exc)
        result["suggestion"], result["suggestions"] = None, []
        guide["status"] = "unavailable"
        result["gex_context"].update(ready=False, liquidity_choices=[], pois=[], matches=[], reason=str(exc))
        result["liquidity"].update(status="unavailable", error=str(exc), nearest_above=None,
                                  highest_above=None, nearest_below=None, above_count=0, below_count=0)
        block("feed", "pine_analysis_unavailable", str(exc))
        for side in ("long", "short"):
            result["smc_" + side].update(entries=[], status="WAITING FOR CURRENT PERKY DATA")
    return result
