"""Shared atomic JSON, durable alert identities and paper account totals."""
import copy,json,os,tempfile
from pathlib import Path

def atomic_json(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=path.name+".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()

def queue_event(document, event_id, kind, text, now, trade=None):
    if any(e["id"] == event_id for e in document["outbox"]):
        return
    document["outbox"].append({"id": event_id, "kind": kind, "status": "queued", "text": text,
                               "created_ms": now, "attempts": 0, "retry_ms": 0,
                               "payload": copy.deepcopy(trade), "error": None})

def portfolio(document):
    active = [t for a in document["assets"].values() for t in a["trades"] if t["status"] == "active"]
    return {"account_currency": document["account_currency"], "cash": document["cash"], "cost_equity": document["cash"]+sum(t["collateral"] for t in active),
            "open_risk": sum(t["initial_risk_usd"] for t in active), "active_positions": len(active),
            "realized_pnl": document["realized_pnl"]}
