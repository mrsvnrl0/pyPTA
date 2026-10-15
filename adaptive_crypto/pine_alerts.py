"""Deduplicated Pine-derived setup notifications through the existing outbox."""
import copy
from .storage import queue_event
from .core import utc
from .position_alerts import price, native_quote


def _temporary_observation(guide, event):
    if guide.get("status") == "unavailable":
        return True
    # Opposite-direction blockers do not invalidate this pending setup.
    side = (event.get("payload") or {}).get("side")
    codes = {b.get("code") for b in guide.get("blockers", [])
             if b.get("side") in {None, "feed", side}}
    return bool(codes) and codes <= {
        "pine_analysis_unavailable", "perky_bracket_invalid", "rejection_not_held",
        "spread_too_wide", "liquidity_missing"}


def monitor_pine_setups(document, asset, symbol, market, now):
    plan = market.get("suggestion")
    guide = market.get("guide") or {}
    current = bool(plan and guide.get("status") == "qualified" and guide.get("expires_ms", 0) > now)
    key = str(plan.get("id", plan.get("key", ""))) if current else ""
    event_id = "pine-setup:" + symbol + ":" + key
    for event in document["outbox"]:
        if event.get("alert_type") == "pine_setup" and event.get("symbol") == symbol and event["status"] == "queued":
            if not current or event["id"] != event_id:
                unavailable = not current and _temporary_observation(guide, event)
                # Suspend missing data or temporary quote blockers without losing a pending setup. A
                # fresh observation of the same plan may resume after an outage.
                event.update(status="cancelled", error="Waiting for current Pine data." if unavailable
                             else "The Pine setup is no longer qualified.")
                if not unavailable:
                    event["retired"] = True
    if not current or not key:
        return
    event = next((e for e in document["outbox"] if e["id"] == event_id), None)
    if event and (event["status"] not in {"queued", "cancelled"} or event.get("retired")):
        return
    side = plan.get("side", "long")
    title = f"{asset} · SMC ENGINE QUALIFIED {side.upper()}"
    currency = native_quote(symbol)
    lines = [title, symbol + " · " + utc(now),
             f"Perky signal: {plan.get('signal_kind', 'SMC')} · score {plan.get('score', '?')}/10.",
             f"Live entry reference: {price(plan['entry_price'], currency)} · Pine signal reference: {price(plan.get('signal_entry_price'), currency)}",
             f"Structure stop: {price(plan['stop'], currency)}",
             f"TP1: {price(plan['tp1'], currency)} · TP2: {price(plan['tp2'], currency)} · TP3: {price(plan['tp3'], currency)}"]
    if plan.get("minimum_exit_distance_pct", 0) > 0:
        lines.append(f"Final exit distance: {plan['exit_distance_pct']:.2f}% · minimum {plan['minimum_exit_distance_pct']:g}%")
    reach = plan.get("reachability") or {}
    if reach.get("mode") not in {None,"off"}:
        lines.append("Target reachability ("+reach["mode"]+"): "+reach.get("label","Unavailable"))
        lines.append("DOL: "+reach.get("cycle","Unconfirmed")+" · HTF bias: "+reach.get("bias","unavailable"))
        concerns = [c["label"] for c in reach.get("checks",[]) if c.get("required") and c["status"] != "pass"]
        if concerns: lines.append("Reachability concerns: "+", ".join(concerns))
    quality = plan.get("quality") or {}
    lines += ["Confluence: " + f"{quality.get('score', '?')}/10; score components are not probabilities.",
              "Review the SMC chart to add a prefilled position. No position or exchange order has been opened."]
    text = "\n".join(lines)
    if event is None:
        queue_event(document, event_id, "telegram", text, now, plan)
        event = document["outbox"][-1]
    event.update(status="queued", text=text, payload=copy.deepcopy(plan), error=None,
                 alert_type="pine_setup", scope="market", position_ids=[], asset=asset,
                 symbol=symbol, title=title, summary=title, observed_ms=now,
                 expires_ms=guide["expires_ms"])
