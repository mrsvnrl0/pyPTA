"""Three independent exit guides for manually recorded positions."""
import copy
from dataclasses import replace

from .core import DataError, H4, finite
from .gex_targets import select_target, rules_for_target
from .holding_targets import monitor_holding_targets
from .storage import queue_event
from .position_alerts import exit_action, identity, price, native_quote
from .position_options import monitored_position, alerts_enabled
from .take_profit_alerts import fresh_quote


def _current_nn_signal(reading, now):
    reading = reading or {}
    signal = reading.get("signal")
    interval = H4
    return bool(signal and not reading.get("error") and signal.get("signal_end") == now//interval*interval-1
                and now < reading.get("expires_ms", 0))


def protective_structure(position, structure, now):
    """A current, close-confirmed protected-level break after this holding began."""
    structure = structure or {}
    key = "protected_low" if position["side"] == "long" else "protected_high"
    level = structure.get(key) or {}
    interval = structure.get("interval_ms", H4)
    label = "Perky SMC"
    current = bool(structure.get("source") == "pine_smc" and not structure.get("error") and structure.get("symbol") == position.get("symbol")
                   and structure.get("end_ms") == now//interval*interval-1
                   and structure.get("expires_ms", 0) > now)
    broken = level.get("broken_ms")
    started = max(position.get("opened_ms", 0), position.get("spot_correction", {}).get("corrected_ms", 0))
    adverse = bool(current and type(broken) is int and started < broken <= structure["end_ms"])
    reason = f"Waiting for current {label} structure context."
    if current:
        reason = (f"A completed {label} close broke the {key.replace('_', ' ')} at {price(level['price'], native_quote(position.get('symbol')))} after this position was recorded."
                  if adverse else f"No confirmed adverse {label} level break since this position was recorded.")
    return {"is_current": current, "adverse": adverse, "reason": reason,
            "level": level.get("price"), "broken_ms": broken, "end_ms": structure.get("end_ms"),
            "expires_ms": structure.get("expires_ms", now) if current else now}


def _profit_reference(position, mark, protective):
    gross = percent = None
    if mark is not None:
        try:
            change = (mark-position["entry"])*(1 if position["side"] == "long" else -1)
            gross = finite(change*position["quantity"], "position P/L")
            percent = finite(change/position["entry"]*100, "position return")
        except DataError:
            gross = percent = None
    return {"gross_pnl": gross, "gross_pnl_percent": percent,
            "profit_at_risk": bool(protective and gross is not None and gross > 0)}


def nn_advice(position, reading, quote, now, structure=None):
    reading = reading or {}
    signal = reading.get("signal")
    valid = _current_nn_signal(reading, now)
    quote = fresh_quote(quote, now)
    long = position["side"] == "long"
    mark = quote["bid" if long else "ask"] if quote else None
    against = valid and signal["label"] == ("SELL" if long else "BUY")
    context = protective_structure(position, structure, now)
    light = "WAIT"
    cause = None
    expires = now
    reason = reading.get("error") or "Waiting for the latest completed NN candle and a fresh exit quote."
    if quote and context["adverse"]:
        light, cause = "STOP LOSS", "structure_break"
        reason = context["reason"] + " Market structure now threatens this position."
        expires = min(quote["asof_ms"]+30000, context["expires_ms"])
    elif quote and valid:
        light = "STOP LOSS" if against else "HOLD"
        cause = "nn_reversal" if against else None
        reason = ("Adverse NN prediction: protect this position against the changed market context."
                  if against else "NN does not oppose this position.")
        expires = min(quote["asof_ms"]+30000, reading.get("expires_ms", now))
        if not against and structure is not None:
            if context["is_current"]:
                expires = min(expires, context["expires_ms"])
            else:
                light, expires = "WAIT", now
                reason = "NN does not oppose this position, but current structure context is unavailable."
    action = exit_action(position["side"]) if light in {"STOP LOSS", "TAKE PROFIT"} else light
    result = {"light": light, "action_label": light+" · "+action if action != light else light,
            "signal": signal["label"] if signal else None, "signal_end": signal.get("signal_end") if signal else None,
            "probabilities": copy.deepcopy(signal.get("probabilities", {})) if signal else {},
            "reason": reason, "mark": mark, "observed_ms": now, "expires_ms": expires,
            "exit_cause": cause, "structure_context": context,
            "nn_current": valid, "nn_expires_ms": reading.get("expires_ms", now) if valid else now,
            **_profit_reference(position, mark, light == "STOP LOSS")}
    return result


def target_advice(position, target, quote, now, structure=None):
    quote = fresh_quote(quote, now)
    long = position["side"] == "long"
    mark = quote["bid" if long else "ask"] if quote else None
    stop = position.get("stop_price")
    stopped = mark is not None and stop is not None and (mark <= stop if long else mark >= stop)
    hit = mark is not None and target is not None and (mark >= target["price"] if long else mark <= target["price"])
    context = protective_structure(position, structure, now)
    threatened = bool(quote and context["adverse"])
    light = "STOP LOSS" if stopped else "TAKE PROFIT" if hit else "STOP LOSS" if threatened else "HOLD" if quote and target else "WAIT"
    cause = "saved_stop" if stopped else "target_reached" if hit else "structure_break" if threatened else None
    expires = quote["asof_ms"]+30000 if quote else now
    reason = ("The fresh exit quote reached your saved stop price." if stopped else
              "The fresh exit quote reached this take-profit target." if hit else
              context["reason"] + (" The remaining take-profit target is at risk." if target else " Market structure now threatens this position.") if threatened else
              "The target has not been reached; no protective exit is currently signalled." if quote and target else
              "Waiting for a target and a fresh exit quote.")
    if quote and not stopped and not hit and structure is not None:
        if context["is_current"]:
            expires = min(expires, context["expires_ms"])
        else:
            light, expires = "WAIT", now
            reason = "Waiting for current structure context before assessing target risk."
    return {"light": light, "action_label": light+" · "+exit_action(position["side"]) if light in {"STOP LOSS", "TAKE PROFIT"} else light,
            "mark": mark, "observed_ms": now, "expires_ms": expires, "reason": reason,
            "exit_cause": cause, "structure_context": context,
            **_profit_reference(position, mark, light == "STOP LOSS")}




def _target(point, rules, side, now):
    buffer = 0
    return {"liquidity_price": point["price"], "pivot_ms": point["bar_ms"],
            "price": point["price"]*(1+(1 if side == "long" else -1)*buffer/10000),
            "sweep_buffer_bps": buffer, "selected_ms": now,
            "setup_minutes": rules.smc_setup_minutes, "state": "watching", "reached_ms": None,
            "selection": point["target_selection"]}


def _alert(document, position, kind, key, advice, now):
    if not alerts_enabled(position, kind):
        return
    event_id = f"holding:{position['id']}:{key}"
    event = next((e for e in document["outbox"] if e["id"] == event_id), None)
    if event and (event["status"] not in {"queued", "cancelled"} or event.get("retired")):
        return
    title = f"{position['asset']} #{position['id'][:8]} · {advice['action_label']}"
    currency = native_quote(position["symbol"])
    text = f"{title}\n{identity(position)}\nExit quote: {price(advice['mark'], currency)}\n{advice['reason']}"
    if advice.get("profit_at_risk"):
        text += f"\nCurrent gross profit to protect: {price(advice['gross_pnl'], currency)} ({advice['gross_pnl_percent']:.2f}%). Trading costs are not included."
    text += "\nRecord your actual exit; the holding remains open."
    if event is None:
        queue_event(document, event_id, "telegram", text, now, advice)
        event = document["outbox"][-1]
    event.update(status="queued", text=text, payload=copy.deepcopy(advice), alert_type=kind,
                 position_ids=[position["id"]], asset=position["asset"], symbol=position["symbol"],
                 title=title, summary=title, action_label=advice["action_label"],
                 observed_ms=now, expires_ms=advice["expires_ms"], error=None)


def monitor_position_guidance(document, asset, symbol, high, low, quote, reading, rules, confluence, now):
    quote = fresh_quote(quote, now)
    structure = document.get("market_structure", {}).get(asset)
    from .pine_guidance import current_pine_history
    valid_history = ((confluence or {}).get("source") == "pine_smc" and
                     current_pine_history(confluence.get("pine_structure"), high, now))
    for position in document["positions"]:
        if (position["status"] != "open" or position["asset"] != asset or position["symbol"] != symbol
                or not monitored_position(position, rules.market_mode)):
            continue
        long = position["side"] == "long"
        mark = quote["bid" if long else "ask"] if quote else None
        position.setdefault("position_targets", {"smc": None, "gex_smc": None})
        targets = position["position_targets"]
        problems = {}
        position["suggested_stop"] = None
        if valid_history:
            if (confluence or {}).get("source") == "pine_smc":
                from .pine_guidance import pine_suggested_stop
                position["suggested_stop"] = pine_suggested_stop(position, confluence.get("pine_structure", {}), quote, rules, now)
        for method in ("smc", "gex_smc"):
            if targets.get(method) is None and quote and valid_history:
                reference = max(position["entry"], mark) if long else min(position["entry"], mark)
                if method == "gex_smc" and targets.get("smc"):
                    reference = max(reference, targets["smc"]["liquidity_price"]) if long else min(reference, targets["smc"]["liquidity_price"])
                point = select_target(high, low, rules_for_target(rules, method), position["side"], reference, now, confluence,
                                      exit_reference=position["entry"] if method == "gex_smc" else None)
                if method == "smc" and point and targets.get("gex_smc"):
                    farther = (point["price"] >= targets["gex_smc"]["liquidity_price"] if long else point["price"] <= targets["gex_smc"]["liquidity_price"])
                    if farther:
                        point = None
                if point and (method == "smc" or point["target_selection"]["method"] == "gex_smc"):
                    targets[method] = _target(point, rules, position["side"], now)
                else:
                    problems[method] = ("No farther GEX-supported TP2 available. "+(confluence or {}).get("reason", "Waiting for options data.").replace("; nearest SMC liquidity used", "")
                                        if method == "gex_smc" else "Waiting for untaken SMC liquidity beyond entry and the current price.")
            elif targets.get(method) is None:
                problems[method] = "Waiting for current SMC candles and a fresh quote."
            if targets.get(method):
                shadow = {**position, "target_mode": method, "take_profit": targets[method],
                          "alert_revision": (position.get("alert_revision") or "")+":"+method}
                # Existing target chronology, proximity and hit alerts run separately for each method.
                virtual = {"positions": [shadow], "outbox": document["outbox"]}
                target_guide = target_advice(position, targets[method], quote, now, structure)
                stopped = target_guide["light"] == "STOP LOSS"
                # Structural warnings suppress competing delivery, but still track target chronology.
                if target_guide["exit_cause"] != "saved_stop":
                    monitor_holding_targets(virtual, asset, symbol, high if valid_history else [], low if valid_history else [],
                                            quote, rules, now, gex_context=confluence)
                for event in document["outbox"]:
                    if event["id"].startswith(f"holding:{position['id']}") and ":"+method+":" in event["id"]:
                        event["target_method"] = method
                        if stopped and event["status"] == "queued":
                            event.update(status="cancelled", error="A protective exit currently takes precedence.")
                        if event["status"] == "queued":
                            label = "SMC TP1" if method == "smc" else "SMC + GEX TP2"
                            if not event["text"].startswith(label+"\n"):
                                event["text"] = label+"\n"+event["text"]
        position["target_errors"] = problems
        previous = position.get("nn_guidance")
        position_reading = (reading or {}).get("holding_readings", {}).get(position["id"], reading)
        position["nn_guidance"] = nn_advice(position, position_reading, quote, now, structure)
        # NN notifications follow the NN transition only; structure has its own deduplicated alerts.
        advice = nn_advice(position, position_reading, quote, now)
        # Keep the notification baseline separate from the visible WAIT state.
        # An unavailable quote must not consume a newly adverse classification.
        previous_signal = position.get("nn_alert_signal", (previous or {}).get("signal"))
        if previous_signal is not None:
            position["nn_alert_signal"] = previous_signal
        current_signal = _current_nn_signal(position_reading, now)
        adverse_prediction = advice["signal"] == ("SELL" if long else "BUY")
        if current_signal and (previous_signal is None or not adverse_prediction or advice["light"] != "WAIT"):
            if (previous_signal is not None and previous_signal != advice["signal"]
                    and advice["light"] in {"STOP LOSS", "TAKE PROFIT"}
                    and advice["signal_end"] > max(position["opened_ms"], position.get("momentum_enabled_ms", 0))):
                _alert(document, position, "position_neural", f"independent-nn:{advice['signal_end']}", advice, now)
            position["nn_alert_signal"] = advice["signal"]
        stop_advice = target_advice(position, None, quote, now)
        if stop_advice["light"] == "STOP LOSS":
            _alert(document, position, "position_stop", f"stop:{position.get('stop_revision',0)}", stop_advice, now)
        for event in document["outbox"]:
            neural_event = event.get("alert_type") == "position_neural"
            recoverable = neural_event and event["status"] == "cancelled" and not event.get("retired")
            if position["id"] not in event.get("position_ids", []) or (event["status"] != "queued" and not recoverable):
                continue
            if event.get("alert_type") in {"position_neural", "position_stop"}:
                current = advice if neural_event else stop_advice
                same_signal = event.get("payload", {}).get("signal_end") == advice.get("signal_end") if neural_event else True
                enabled = alerts_enabled(position, event["alert_type"])
                if current["light"] not in {"STOP LOSS", "TAKE PROFIT"} or not same_signal or not enabled:
                    adverse = advice["signal"] == ("SELL" if long else "BUY")
                    retired = (not neural_event or not enabled or (current_signal and (not same_signal or not adverse)))
                    event.update(status="cancelled", retired=retired, error="This exit signal is no longer current.")
                else:
                    key = event["id"].split(f"holding:{position['id']}:", 1)[-1]
                    _alert(document, position, event["alert_type"], key, current, now)
