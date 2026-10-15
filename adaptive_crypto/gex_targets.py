"""Point-in-time GEX/SMC confluence and shared liquidity-target selection.

GEX ranks existing untaken SMC targets. It never invents a target or moves a
saved one. The numerical proximity/basis limits are explicit model choices.
"""
import copy
from dataclasses import replace

from .core import DataError, exit_distance_pct, finite, validate_candles
from .take_profit_alerts import fresh_quote
from .gex_currency import profile_for_market, require_market_currency

MAX_AGE_MS = 300000


def validate_target_mode(mode):
    if mode not in ("smc", "gex_smc"):
        raise DataError("Choose SMC or SMC + GEX for the target method")
    return mode


def rules_for_target(rules, mode):
    return replace(rules, smc_gex_targets=validate_target_mode(mode) == "gex_smc")


def near(a, b, bps):
    return abs(a-b)/b*10000 <= bps+1e-9


def validate_selection(selection, price, side):
    """Saved targets retain the exact selection evidence."""
    if selection is None:
        return
    if (not isinstance(selection, dict) or selection.get("method") not in {"smc", "gex_smc"}
            or selection.get("liquidity_price") != price or type(selection.get("selected_ms")) is not int
            or selection["selected_ms"] < 0 or not isinstance(selection.get("matches"), list)):
        raise DataError("Invalid saved GEX/SMC target selection")
    if selection["method"] == "smc":
        if selection["matches"]:
            raise DataError("SMC fallback cannot claim GEX alignment")
        return
    stamp = finite(selection.get("gex_asof_ms"), "saved GEX observation", 0)
    if not 0 <= selection["selected_ms"]-stamp < MAX_AGE_MS or not selection["matches"]:
        raise DataError("Saved GEX evidence was not current at target selection")
    for match in selection["matches"]:
        poi = match.get("poi", {})
        if not match.get("eligible") or poi.get("side") != side:
            raise DataError("Saved GEX match has the wrong target direction")
        level = finite(match.get("gex_price"), "saved GEX level", 1e-15)
        if poi.get("kind") in {"bsl", "ssl"}:
            aligned = price == poi.get("price") and near(price, level, selection["alignment_bps"])
        elif poi.get("kind") in {"premium_ob", "discount_ob"}:
            aligned = poi["low"] <= price <= poi["high"] and poi["low"] <= level <= poi["high"]
        elif poi.get("kind") == "mss":
            aligned = price > level if side == "long" else price < level
        else:
            aligned = False
        if not aligned:
            raise DataError("Saved GEX match does not support the recorded liquidity target")




def select_target(high, low, rules, side, reference, known_ms, confluence=None, *, exit_reference=None):
    if confluence and confluence.get("source") == "pine_smc":
        # The Pine engine is authoritative; do not generate alternative targets if
        # its current state has no untaken liquidity in this direction.
        choices = []
        if confluence.get("selected_ms") == known_ms:
            for point in confluence.get("liquidity_choices", []):
                try:
                    price = finite(point.get("price"), "Pine liquidity", 1e-15)
                    formed = finite(point.get("bar_ms"), "Pine pivot time", 0)
                    confirmed = finite(point.get("known_ms"), "Pine confirmation time", formed)
                    if (point.get("side") == side and confirmed <= known_ms
                            and (price > reference if side == "long" else price < reference)):
                        if (exit_reference is None or
                                exit_distance_pct(exit_reference, price, side) + 1e-9 >= rules.smc_min_exit_distance_pct):
                            choices.append(copy.deepcopy(point))
                except (DataError, TypeError, AttributeError):
                    continue
        choices.sort(key=lambda point: point["price"], reverse=side == "short")
    else:
        return None
    if not choices:
        return None
    chosen, matches = choices[0], []
    info = {"method": "smc", "reason": "Nearest untaken SMC liquidity", "matches": []}
    if rules.smc_gex_targets:
        info["reason"] = (confluence or {}).get("reason", "GEX unavailable; nearest SMC liquidity used")
        if confluence and confluence.get("ready") and confluence.get("selected_ms") == known_ms:
            info["reason"] = "No eligible GEX alignment for this target; nearest SMC liquidity used"
            ranked = []
            for point in choices:
                evidence = []
                for match in confluence["matches"]:
                    poi = match["poi"]
                    if not match["eligible"] or poi["side"] != side:
                        continue
                    if poi["kind"] in {"bsl", "ssl"}:
                        aligned = point["bar_ms"] == poi["pivot_ms"] and point["price"] == poi["price"]
                    elif poi["kind"] == "mss":
                        aligned = point["price"] > match["gex_price"] if side == "long" else point["price"] < match["gex_price"]
                    else:
                        aligned = poi["low"] <= point["price"] <= poi["high"]
                    if aligned:
                        evidence.append(match)
                if evidence:
                    ranked.append((point, evidence))
            if ranked:
                # Nearest qualifying confluence wins; do not inflate a distant target with a score.
                chosen, matches = ranked[0]
                info.update(method="gex_smc", reason="Nearest untaken SMC target with eligible GEX confluence",
                            matches=copy.deepcopy(matches), gex_asof_ms=confluence["gex_asof_ms"],
                            regime=confluence["regime"], alignment_bps=rules.smc_gex_alignment_bps)
    if confluence and confluence.get("source") == "pine_smc":
        info["source"] = "pine_smc"
    info.update(selected_ms=known_ms, nearest_smc_price=choices[0]["price"], liquidity_price=chosen["price"])
    return {**chosen, "target_selection": info}
