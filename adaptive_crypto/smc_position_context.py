"""Adapt the Perky SMC strategy snapshot for existing holding risk lights."""
import copy
from .core import utc
from .storage import queue_event
from .position_options import alerts_enabled, monitored_position
from .position_alerts import price, native_quote


def monitor_smc_structure(document, asset, symbol, structure, bars, now, market_mode="spot"):
    interval = structure.get("interval_ms", bars[-1].interval if bars else 1800000)
    end = bars[-1].end if bars else None
    ready = structure.get("status") == "ready" and end == now//interval*interval-1
    previous = document.setdefault("market_structure", {}).get(asset, {})
    state = {"source": "pine_smc", "symbol": symbol, "interval_ms": interval,
             "bar_ms": bars[-1].t if bars else None, "end_ms": end,
             "close": bars[-1].c if bars else None,
             "expires_ms": end+interval+1 if ready else now,
             "error": None if ready else structure.get("error") or "Waiting for current Perky SMC candles",
             "protected_low": None, "protected_high": None,
             "swing_low": None, "swing_high": None, "regime": structure.get("regime"),
             "quality": copy.deepcopy(structure.get("quality", {}))}
    points=structure.get("liquidity", [])
    for point in points:
        key="swing_high" if point.get("side") in {"long", "bullish", "high"} else "swing_low"
        old=state[key]
        if old is None or point.get("known_ms",0)>old.get("known_ms",0): state[key]=copy.deepcopy(point)
    events=sorted((e for e in structure.get("events",[]) if e.get("kind") in {"BOS","CHoCH"}),key=lambda e:e.get("known_ms",0))
    for key,side in (("protected_low","short"),("protected_high","long")):
        matches=[e for e in events if e.get("side") in ({"short","bearish"} if side=="short" else {"long","bullish"})]
        e=matches[-1] if matches else None
        if e and events and e is events[-1]:
            broken=int(e["known_ms"])
            state[key]={"price":e["level"],"broken_ms":broken,"protected_ms":e.get("pivot_ms",e.get("time_ms",broken)),"strength":e.get("strength"),"event":e["kind"]}
        else:
            point=state["swing_low" if side=="short" else "swing_high"]
            if point: state[key]={**point,"broken_ms":None,"protected_ms":point.get("known_ms")}
    document["market_structure"][asset]=state
    for event in document["outbox"]:
        if event.get("asset")==asset and event.get("alert_type") in {"position_structure","position_momentum"} and event["status"]=="queued":
            if event.get("source")!="pine_smc" or not ready or event.get("expires_ms",0)<=now:
                event.update(status="cancelled",retired=True,error="Replaced or expired SMC structure evidence")
    # Baseline the first observation; do not broadcast historical breaks on startup.
    if ready and previous.get("source")=="pine_smc" and previous.get("bar_ms")!=state["bar_ms"]:
        for p in document["positions"]:
            if p["asset"]!=asset or p["symbol"]!=symbol or p["status"]!="open" or not monitored_position(p,market_mode) or not alerts_enabled(p,"position_structure"): continue
            level=state["protected_low" if p["side"]=="long" else "protected_high"]
            if not level or level.get("broken_ms")!=end or end<=max(p["opened_ms"],p.get("momentum_enabled_ms",0)): continue
            event_id=f"holding:{p['id']}:pine-structure:{end}:{p['side']}"
            if any(e["id"]==event_id for e in document["outbox"]): continue
            title=f"{asset} #{p['id'][:8]} · PERKY SMC STRUCTURE THREATENS {p['side'].upper()}"
            message=f"{title}\n{symbol} · {interval//60000}M close {utc(end)}\n{level['event']} through {price(level['price'], native_quote(symbol))}.\nReview the saved stop and remaining targets. Your recorded holding stays open."
            queue_event(document,event_id,"telegram",message,now,level)
            document["outbox"][-1].update(source="pine_smc",alert_type="position_structure",position_ids=[p["id"]],asset=asset,symbol=symbol,title=title,summary=title,observed_ms=now,expires_ms=min(now+300000,state["expires_ms"]))
    return copy.deepcopy(state)
