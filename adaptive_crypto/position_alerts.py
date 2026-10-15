"""Consistent, position-specific wording for deterministic holding alerts."""
from .core import DataError, finite, utc
from .storage import queue_event
from .take_profit_alerts import fresh_quote
from .position_options import alerts_enabled, monitored_position


def exit_action(side):
    return "SELL TO EXIT LONG" if side == "long" else "BUY TO COVER SHORT"


def entry_action(side):
    return "BUY TO OPEN LONG" if side == "long" else "SELL TO OPEN SHORT"


def target_event_id(position, kind):
    revision = ":"+position["alert_revision"] if position.get("alert_revision") else ""
    return f"holding:{position['id']}{revision}:{kind}"


def native_quote(symbol):
    return symbol.split("/")[-1] if isinstance(symbol, str) and "/" in symbol else "USD"


def price(value, quote="USD"):
    if 0 < abs(value) < 1e-8:
        number = f"{value:.8g}"
    else:
        text = f"{value:,.8f}".rstrip("0")
        whole, _, fraction = text.partition(".")
        number = whole + "." + fraction.ljust(2, "0")
    return "$" + number if quote == "USD" else number + " " + quote


def identity(position):
    return (f"Position #{position['id'][:8]} · {position['symbol']} · {position['side'].upper()}\n"
            f"Quantity: {position['quantity']:.8g} {position['asset']}\n"
            f"Recorded entry price: {price(position['entry'], native_quote(position.get('symbol')))}")


def target_reached_alert(document, position, quote, now, newly_reached=False):
    """One exit alert per target; never replay targets already reached before this update."""
    if not alerts_enabled(position, "target_reached"):
        return
    event_id = target_event_id(position, "target-hit")
    event = next((e for e in document["outbox"] if e["id"] == event_id), None)
    if not newly_reached and event is None:
        return
    if event and (event["status"] not in {"queued", "cancelled"} or event.get("retired")):
        return
    quote = fresh_quote(quote, now)
    target = position["take_profit"]
    long = position["side"] == "long"
    quote_side = "bid" if long else "ask"
    mark = quote[quote_side] if quote else None
    at_target = quote and (mark >= target["price"] if long else mark <= target["price"])
    if not newly_reached and not at_target:
        if event and event["status"] == "queued":
            event.update(status="cancelled", error="Target exit alert paused: no fresh quote at or beyond the target.")
        return
    action = exit_action(position["side"])
    currency = native_quote(position["symbol"])
    title = f"{position['asset']} #{position['id'][:8]} · {'TAKE-PROFIT HIT' if at_target else 'TARGET TOUCHED EARLIER'} · {action if at_target else 'REVIEW EXIT'}"
    lines = [title, identity(position),
             f"Exit action: {action}" if at_target else f"Exit side when you choose to close: {action}",
             f"Target exit price: {price(target['price'], currency)}"]
    if quote:
        lines.extend([f"Current exit quote ({quote_side}): {price(mark, currency)}", f"Quote observed: {utc(quote['asof_ms'])}"])
    else:
        lines.append("Fresh exit quote unavailable; the target is a reference price, not a current fill.")
    if not at_target:
        lines.append("The completed candle touched the target earlier. Verify the current price before exiting.")
    lines.extend([f"Target first observed reached: {utc(target['reached_ms'])}",
                  "Holding remains open; record your actual exit in the dashboard. No exchange order is submitted."])
    payload = {"position_id": position["id"], "asset": position["asset"], "side": position["side"],
               "quantity": position["quantity"], "entry_price": position["entry"],
               "action": ("sell" if long else "buy") if at_target else "review",
               "action_label": action if at_target else "REVIEW EXIT", "planned_exit_action": action,
               "target": target["price"], "exit_price": mark if quote else target["price"],
               "price_kind": "live_" + quote_side if quote else "target_reference",
               "quote_ms": quote["asof_ms"] if quote else None}
    if event is None:
        queue_event(document, event_id, "telegram", "\n".join(lines), now, payload)
        event = document["outbox"][-1]
    event.update(status="queued", observed_ms=now, title=title, action_label=action if at_target else "REVIEW EXIT", text="\n".join(lines), payload=payload,
                 summary=f"{action if at_target else 'REVIEW EXIT'} · {'Exit quote' if quote else 'Target reference'} {price(mark if quote else target['price'], currency)} · Target {price(target['price'], currency)} · Entry {price(position['entry'], currency)}",
                 alert_type="target_reached", scope="holding", position_ids=[position["id"]],
                 asset=position["asset"], symbol=position["symbol"], side=position["side"], error=None,
                 expires_ms=int(quote["asof_ms"]+30000) if at_target else target["reached_ms"]+30000)
