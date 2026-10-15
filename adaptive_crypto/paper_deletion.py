"""Backed-up removal of mistaken NN paper records and their cash contribution."""
import copy
import re

from .core import DataError
from .position_options import paper_key
from .neural_ledger import validate
from .positions import validate_positions
from .administration import _documents, _backup, _save


def delete_paper_trade(runtime, record_id):
    """Called under exclusive activity after checking this browser's CSRF/generation."""
    if not isinstance(record_id, str) or not re.fullmatch(r"[0-9a-f]{64}", record_id):
        raise DataError("Invalid paper position ID")
    if not runtime.is_neural or runtime.rules.paper_namespace != "neural":
        raise DataError("Only current NN paper records can be removed")
    with runtime.lock, runtime.store.lock, runtime.positions.lock:
        if runtime.stop.is_set():
            raise DataError("Dashboard is restarting; retry when it returns")
        documents, contexts = _documents(runtime)
        current = documents["neural"]
        if record_id in current.get("deleted_paper_trades", {}):
            return {"message": "Paper entry was already removed.", "deleted": False}
        match = next(((name, trade) for name, row in current["assets"].items()
                      for trade in row["trades"] if paper_key("neural", trade["id"]) == record_id), None)
        if match is None:
            raise KeyError(record_id)
        name, trade = match
        proposed = {key: copy.deepcopy(documents[key]) for key in ("neural", "positions")}
        paper = proposed["neural"]
        if trade["status"] == "active":
            paper["cash"] += trade["collateral"] + trade["entry_fee"]
        else:
            cash = paper["cash"] - trade["realized_pnl"]
            if cash < -1e-7:
                raise DataError("This trade's profit is funding other paper positions. Remove those entries first.")
            paper["cash"] = max(0., cash)
            paper["realized_pnl"] -= trade["realized_pnl"]
        paper["assets"][name]["trades"] = [t for t in paper["assets"][name]["trades"] if t["id"] != trade["id"]]
        # Keep signal progress: deleting an automatic trade must not replay its BUY.
        paper["assets"][name]["last_result"] = "Paper entry removed; waiting for the next NN decision candle"
        tombstone = {"asset": name, "trade_id": trade["id"]}
        if "manual_order" in trade:
            tombstone["manual_order"] = copy.deepcopy(trade["manual_order"])
        paper.setdefault("deleted_paper_trades", {})[record_id] = tombstone
        for event in paper["outbox"]:
            if (event["id"].startswith(trade["id"] + ":")
                    or isinstance(event.get("payload"), dict) and event["payload"].get("id") == trade["id"]):
                event["retired"] = True
                if event["status"] == "queued":
                    event.update(status="cancelled", error="Paper entry removed.")
        proposed["positions"].get("paper_alert_preferences", {}).pop(record_id, None)
        validate(paper)
        validate_positions(proposed["positions"])
        backup = _backup(runtime, "paper-entry-delete", documents, contexts)
        _save(runtime, proposed, {key: contexts[key] for key in proposed}, backup)
        # Cached presentation must not continue showing a removed trade until scan.
        row = runtime.data.get(name, {})
        if isinstance(row.get("neural"), dict):
            active = copy.deepcopy([t for t in paper["assets"][name]["trades"] if t["status"] == "active"])
            row["neural"].update(trade=active[0] if active else None, trades=active, active_count=len(active),
                                 status="ACTIVE PAPER TRADE" if len(active) == 1 else f"{len(active)} ACTIVE PAPER TRADES" if active else "WAITING",
                                 result=paper["assets"][name]["last_result"])
        runtime.wake.set()
        return {"message": "Paper entry removed and its account contribution reversed.", "deleted": True}
