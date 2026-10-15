"""Durable long-only SMC and Parente paper ledger."""
from __future__ import annotations

from dataclasses import asdict
import copy
import hashlib
import json
import re
from math import isclose
import time

from .core import DataError, H4, Rules, exit_distance_pct, finite, utc
from .storage import atomic_json, queue_event
from .persistence import DocumentStore
from .storage import portfolio
from .currency_conversion import identity_fx, validate_fx

VERSION = "smc-parente-paper-v2"
ACCOUNT_CURRENCY = "USD"


def _account_fx(symbol, fx, now):
    currency = symbol.split("/")[-1]
    if fx is None:
        fx = identity_fx(currency, now, ACCOUNT_CURRENCY)
    return validate_fx(fx, currency, now, ACCOUNT_CURRENCY)


def record(symbol):
    return {"symbol": symbol, "trades": [], "consumed": [], "last_signal_end": 0,
            "last_result": None, "model_id": None}


def validate(document):
    try:
        if document["version"] != VERSION:
            raise DataError("Paper account uses an older currency contract; clear it before starting the USD account")
        if document["account_currency"] != ACCOUNT_CURRENCY:
            raise DataError("Paper account currency must be USD")
        json.dumps(document, allow_nan=False)
        Rules(**document["settings"]).validate()
        if not isinstance(document["assets"], dict):
            raise DataError("Invalid neural assets")
        cash = finite(document["cash"], "neural cash", -1e-7)
        realized = finite(document["realized_pnl"], "neural P/L")
        initial = finite(document["initial_equity"], "initial neural equity", 1e-15)
        if not isinstance(document["warnings"], list) or not isinstance(document["outbox"], list):
            raise DataError("Invalid neural state sections")
        ids, requests, collateral, paid_fees, closed_pnl = set(), set(), 0., 0., 0.
        for name, row in document["assets"].items():
            if not isinstance(row["symbol"], str) or type(row["last_signal_end"]) is not int or row["last_signal_end"] < 0:
                raise DataError("Invalid neural candle progress")
            progress_interval = row.get("last_signal_interval_ms", H4)
            if type(progress_interval) is not int or progress_interval != H4:
                raise DataError("Invalid neural progress interval")
            if row["last_signal_end"] and (row["last_signal_end"]+1) % progress_interval:
                raise DataError("Neural progress must identify a completed decision candle")
            if not isinstance(row["trades"], list) or not isinstance(row["consumed"], list):
                raise DataError("Invalid neural history")
            if (any(not isinstance(key, str) or not key for key in row["consumed"])
                    or len(set(row["consumed"])) != len(row["consumed"])):
                raise DataError("Invalid consumed paper setup identities")
            # Mode changes never invalidate already-recorded positions. The
            # one-position restriction is an entry rule, not a ledger invariant.
            for t in row["trades"]:
                if (not isinstance(t["id"], str) or not t["id"] or t["id"] in ids or t["asset"] != name
                        or t["side"] != "long" or t["strategy"] != "neural_network" or t["symbol"] != row["symbol"]):
                    raise DataError("Invalid neural trade identity")
                ids.add(t["id"])
                origin = t["entry_origin"]
                if origin not in {"manual", "smc_parente"}:
                    raise DataError("Invalid neural entry origin")
                manual = origin == "manual"
                combined = origin == "smc_parente"
                interval = t.get("decision_interval_ms", H4)
                if type(interval) is not int or interval != H4:
                    raise DataError("Neural entries require completed 4H candles")
                limited = t.get("limitations_enabled", True)
                if type(limited) is not bool:
                    raise DataError("Invalid recorded neural limitations mode")
                for key in ("entry", "quantity", "collateral", "initial_risk_usd"):
                    finite(t[key], key, 1e-15)
                has_stop = limited or combined
                if (has_stop and not 0 < finite(t["stop"], "stop") < t["entry"]
                        or not has_stop and (t["stop"] is not None or t["status"] == "stopped")
                        or t["status"] not in {"active", "sold", "stopped"}):
                    raise DataError("Invalid neural trade levels/status")
                for key in ("fee_rate", "slippage_rate"):
                    if not 0 <= finite(t[key], key) < .05:
                        raise DataError("Invalid recorded neural costs")
                finite(t["quote_collateral"], "native collateral", 1e-300)
                finite(t["entry_fee_quote"], "native entry fee", 0)
                finite(t["entry_fee"], "USD entry fee", 0)
                fx = _account_fx(t["symbol"], t["entry_fx"], t["opened_ms"])
                quote_collateral = t["entry"]*t["quantity"]
                if (not isclose(t["quote_collateral"], quote_collateral, rel_tol=1e-10)
                        or not isclose(t["collateral"], quote_collateral*fx["buy_rate"], rel_tol=1e-10)):
                    raise DataError("Paper USD collateral mismatch")
                if (not isclose(t["entry_fee_quote"], quote_collateral*t["fee_rate"], rel_tol=1e-10, abs_tol=1e-10)
                        or not isclose(t["entry_fee"], t["collateral"]*t["fee_rate"], rel_tol=1e-10, abs_tol=1e-10)):
                    raise DataError("Paper USD entry fee mismatch")
                stop_fill = t["stop"]*(1-t["slippage_rate"]) if has_stop else 0.
                # Initial risk projects a stop using the entry-time FX spread.
                risk = t["quantity"]*(t["entry"]*(1+t["fee_rate"])*fx["buy_rate"]
                                      - stop_fill*(1-t["fee_rate"])*fx["sell_rate"])
                if not isclose(t["initial_risk_usd"], risk, rel_tol=1e-10, abs_tol=1e-10):
                    raise DataError("Neural recorded risk mismatch")
                if (type(t["opened_ms"]) is not int or t["opened_ms"] < H4 or type(t["last_bar_end"]) is not int
                        or t["last_bar_end"] < t["opened_ms"] or type(t["tracking_gap"]) is not bool):
                    raise DataError("Invalid neural chronology")
                if manual:
                    order = normalize_manual_order(**t["manual_order"])
                    if order != t["manual_order"] or order["request_id"] in requests:
                        raise DataError("Duplicate or invalid manual paper request")
                    requests.add(order["request_id"])
                    boundary = (t["opened_ms"]//interval)*interval-1
                    if (t["signal_end"] is not None or t["label"] != "MANUAL" or t["probabilities"] != {}
                            or type(t["exit_after_signal_end"]) is not int or t["exit_after_signal_end"] != boundary
                            or type(t["entry_reading"]["signal_end"]) is not int
                            or t["entry_reading"]["signal_end"] != boundary
                            or t["entry_reading"]["model_id"] != t["model_id"]
                            or not isinstance(t["model_id"], str) or not t["model_id"]):
                        raise DataError("Invalid manual paper provenance")
                    prediction = t["entry_reading"]
                    if prediction["label"] not in {"BUY", "HOLD", "SELL"}:
                        raise DataError("Invalid manual entry NN reading")
                    if order["budget_usd"] is not None and t["collateral"]+t["entry_fee"] > order["budget_usd"]+1e-7:
                        raise DataError("Manual paper spend exceeds requested budget")
                    if "manual_price" in order:
                        if (t.get("entry_price_source") != "manual_price" or t["entry"] != order["manual_price"]
                                or finite(t["entry_quote"], "observed ask", 1e-15) <= 0
                                or not -2000 <= t["opened_ms"]-finite(t["entry_quote_ms"], "quote time", 0) <= 30000):
                            raise DataError("Invalid manual paper entry price provenance")
                    elif t.get("entry_price_source", "live_ask") != "live_ask":
                        raise DataError("Manual paper price source has no requested price")
                else:
                    if any(key in t for key in ("manual_order", "entry_reading", "exit_after_signal_end")):
                        raise DataError("Automatic neural entry contains manual provenance")
                    if (type(t["signal_end"]) is not int or not 0 <= t["signal_end"] < t["opened_ms"]
                            or (t["signal_end"]+1) % interval or row["last_signal_end"] < t["signal_end"]):
                        raise DataError("Invalid neural signal chronology")
                    if t["label"] != "BUY":
                        raise DataError("Invalid recorded neural prediction")
                    prediction = t
                if (interval != H4 or not isinstance(prediction["model_id"], str) or not prediction["model_id"]
                        or set(prediction["probabilities"]) != {"BUY", "HOLD", "SELL"}
                        or any(not 0 <= finite(p) <= 1 for p in prediction["probabilities"].values())
                        or not isclose(sum(prediction["probabilities"].values()), 1., abs_tol=1e-6)):
                    raise DataError("Invalid recorded neural prediction")
                if combined:
                    plan = t["smc_entry_plan"]
                    plan_id = plan.get("id", plan.get("key"))
                    target = finite(t["target"], "SMC target", 1e-15)
                    tp1 = finite(t["tp1"], "SMC TP1", 1e-15)
                    tp2 = t.get("tp2")
                    if (plan.get("source") != "pine_smc" or plan.get("side") != "long"
                            or not isinstance(plan_id, str) or not plan_id
                            or t["smc_setup_id"] != plan_id
                            or t["smc_consumed_key"] != "smc:"+plan_id
                            or t["smc_consumed_key"] not in row["consumed"]
                            or finite(plan["stop"], "recorded SMC stop", 1e-15) != t["stop"]
                            or finite(plan.get("exit_price", plan.get("target")), "recorded SMC target", 1e-15) != target
                            or finite(plan.get("tp1", plan.get("target")), "recorded SMC TP1", 1e-15) != tp1
                            or plan.get("tp2") != tp2
                            or not t["entry"] < tp1 <= target
                            or (tp2 is not None and not tp1 < finite(tp2, "SMC TP2", 1e-15))
                            or type(plan.get("signal_end")) is not int
                            or not 0 <= plan["signal_end"] < t["opened_ms"]
                            or type(plan.get("expires_ms")) is not int
                            or plan.get("expires_ms", 0) <= t["opened_ms"]):
                        raise DataError("Invalid immutable SMC/Parente entry plan")

                if "exit_decision" in t:
                    decision = t["exit_decision"]
                    if (t["status"] == "active" or t.get("exit_reason") != "Parente SELL"
                            or not isinstance(decision, dict) or decision.get("label") != "SELL"
                            or decision.get("model_id") != t["model_id"]
                            or type(decision.get("signal_end")) is not int
                            or (decision["signal_end"]+1) % H4
                            or not t.get("exit_after_signal_end", t["signal_end"]) < decision["signal_end"] < t["closed_ms"]
                            or set(decision.get("probabilities", {})) != {"BUY", "HOLD", "SELL"}
                            or any(not 0 <= finite(p) <= 1 for p in decision["probabilities"].values())
                            or not isclose(sum(decision["probabilities"].values()), 1., abs_tol=1e-6)):
                        raise DataError("Invalid recorded Parente exit decision")

                if t["status"] == "active":
                    if finite(t["realized_pnl"]) != 0 or finite(t["realized_pnl_quote"]) != 0:
                        raise DataError("Active neural trade cannot realize P/L")
                    collateral += t["collateral"]
                    paid_fees += t["entry_fee"]
                else:
                    finite(t["exit"], "neural exit", 1e-15)
                    if type(t["closed_ms"]) is not int or t["closed_ms"] < t["opened_ms"]:
                        raise DataError("Neural exit precedes entry")
                    if "exit_price_ms" in t:
                        if (t.get("exit_price_source") != "completed_bar_trigger"
                                or type(t["exit_price_ms"]) is not int
                                or not t["opened_ms"] <= t["exit_price_ms"] <= t["closed_ms"]
                                or t["exit_reason"] not in {"stop", "target"}):
                            raise DataError("Invalid historical stop/target price provenance")
                    finite(t["exit_fee_quote"], "native exit fee", 0)
                    finite(t["exit_fee"], "USD exit fee", 0)
                    finite(t["realized_pnl_quote"], "native realized P/L")
                    exit_fx = _account_fx(t["symbol"], t["exit_fx"], t["closed_ms"])
                    exit_fee_quote = t["quantity"]*t["exit"]*t["fee_rate"]
                    expected = t["quantity"]*t["exit"]*(1-t["fee_rate"])*exit_fx["sell_rate"]-t["collateral"]-t["entry_fee"]
                    expected_quote = t["quantity"]*(t["exit"]-t["entry"])-t["entry_fee_quote"]-exit_fee_quote
                    if (not isclose(t["exit_fee_quote"], exit_fee_quote, abs_tol=1e-8)
                            or not isclose(t["exit_fee"], exit_fee_quote*exit_fx["sell_rate"], abs_tol=1e-8)
                            or not isclose(t["realized_pnl"], expected, abs_tol=1e-8)
                            or not isclose(t["realized_pnl_quote"], expected_quote, abs_tol=1e-8)):
                        raise DataError("Paper USD realized P/L mismatch")
                    closed_pnl += expected
        deleted = document.get("deleted_paper_trades", {})
        if not isinstance(deleted, dict):
            raise DataError("Invalid deleted paper history")
        from .position_options import paper_key
        for key, tombstone in deleted.items():
            if (not isinstance(tombstone, dict) or set(tombstone) not in ({"asset", "trade_id"}, {"asset", "trade_id", "manual_order"})
                    or tombstone["asset"] not in document["assets"] or not isinstance(tombstone["trade_id"], str)
                    or not tombstone["trade_id"] or tombstone["trade_id"] in ids
                    or key != paper_key("neural", tombstone["trade_id"])):
                raise DataError("Invalid deleted paper identity")
            ids.add(tombstone["trade_id"])
            if "manual_order" in tombstone:
                order = normalize_manual_order(**tombstone["manual_order"])
                if order != tombstone["manual_order"] or order["request_id"] in requests:
                    raise DataError("Invalid deleted manual paper request")
                requests.add(order["request_id"])
        if not isclose(realized, closed_pnl, abs_tol=1e-6) or not isclose(cash+collateral+paid_fees, initial+realized, abs_tol=1e-6):
            raise DataError("Neural cash does not reconcile")
        events = set()
        for event in document["outbox"]:
            if (not isinstance(event["id"], str) or event["id"] in events or event["kind"] not in {"telegram", "ai"}
                    or event["status"] not in {"queued", "running", "sent", "done", "failed", "uncertain", "cancelled"}):
                raise DataError("Invalid neural delivery record")
            if (not isinstance(event["text"], str) or any(type(event[k]) is not int or event[k] < 0
                                                        for k in ("created_ms", "retry_ms", "attempts"))):
                raise DataError("Invalid neural delivery progress")
            events.add(event["id"])
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError) as exc:
        raise DataError(f"Malformed neural ledger: {exc}") from exc


def prepare_neural(source, assets, rules):
    backups = []
    if source is None:
        data = {"version": VERSION, "account_currency": ACCOUNT_CURRENCY, "cash": rules.paper_equity, "initial_equity": rules.paper_equity,
                "realized_pnl": 0., "settings": asdict(rules), "warnings": [], "outbox": [],
                "assets": {name: record(cfg["symbol"]) for name, cfg in assets.items()}}
    else:
        data = json.loads(source.decode("utf-8-sig"))
        validate(data)
        if data["settings"] != asdict(rules):
            backups.append(f".pre-settings-{time.time_ns()}.json")
            data["settings"] = asdict(rules)
            data["warnings"].append("Settings changed; recorded neural entries, stops, costs and consumed candles remain fixed.")
        changed_pairs = [name for name, cfg in assets.items()
                         if name in data["assets"] and data["assets"][name]["symbol"] != cfg["symbol"]]
        if any(data["assets"][name]["trades"] for name in changed_pairs):
            raise DataError("Clear this asset's paper history before changing its currency pair; recorded trades cannot be relabelled")
        for name, cfg in assets.items():
            if name in changed_pairs:
                data["assets"][name] = record(cfg["symbol"])
            data["assets"].setdefault(name, record(cfg["symbol"]))
        for name, row in data["assets"].items():
            if name not in assets and any(t["status"] == "active" for t in row["trades"]):
                warning = f"{name} is disabled with an active neural trade; re-enable it to monitor exits."
                if warning not in data["warnings"]:
                    data["warnings"].append(warning)
        for event in data["outbox"]:
            if event["status"] == "running":
                event.update(status="uncertain", error="Restart interrupted delivery")
    validate(data)
    return data, backups


class NeuralStore(DocumentStore):
    def __init__(self, path, assets, rules, *, backend=None):
        self._bind(path, backend, validate, atomic_json)
        if backend is not None:
            backend.context = {"assets": assets, "rules": asdict(rules)}
        self._initialize(lambda source: prepare_neural(source, assets, rules), eager=True)


def _entry_terms(document, name, quote, rules, budget_usd=None, *, entry_price=None, fx=None, now=None):
    row = document["assets"][name]
    if rules.nn_limitations and any(t["status"] == "active" for t in row["trades"]):
        raise DataError("Neural position already open")
    entry = quote["ask"]*(1+rules.slippage_rate) if entry_price is None else finite(entry_price, "entry price", 1e-15)
    stop = entry*(1-rules.nn_stop_loss) if rules.nn_limitations else None
    stop_fill = stop*(1-rules.slippage_rate) if stop is not None else 0.
    fx = _account_fx(row["symbol"], fx, now if now is not None else quote["asof_ms"])
    unit_cost = entry*(1+rules.fee_rate)*fx["buy_rate"]
    unit_risk = unit_cost-stop_fill*(1-rules.fee_rate)*fx["sell_rate"]
    if unit_risk <= 0:
        raise DataError("Paper stop risk must be positive in USD")
    book = portfolio(document)
    available_cash = max(0., book["cash"])
    if budget_usd is not None:
        available_cash = min(available_cash, budget_usd)
    quantity = available_cash/unit_cost
    if rules.nn_limitations:
        risk = min(book["cost_equity"]*rules.risk_per_trade,
                   max(0., book["cost_equity"]*rules.max_total_risk-book["open_risk"]),
                   max(0., book["cost_equity"]-rules.paper_floor-book["open_risk"]))
        quantity = min(quantity, risk/unit_risk,
                       max(0., book["cost_equity"])*rules.max_allocation/unit_cost)
    if (rules.nn_limitations and quantity*entry*fx["buy_rate"] < rules.minimum_notional) or quantity <= 0 or available_cash <= 1e-9:
        raise DataError("Insufficient neural paper cash or risk budget")
    return entry, stop, quantity, unit_risk


def _record_entry(document, name, metadata, identity, quote, rules, now, budget_usd=None, *, entry_price=None, fx=None):
    row = document["assets"][name]
    fx = _account_fx(row["symbol"], fx, now)
    entry, stop, quantity, unit_risk = _entry_terms(document, name, quote, rules, budget_usd, entry_price=entry_price, fx=fx, now=now)
    trade = {"id": identity, "asset": name, "symbol": row["symbol"], "strategy": "neural_network", "side": "long",
             "status": "active", "entry": entry, "entry_quote": quote["ask"], "entry_quote_ms": quote["asof_ms"],
             "stop": stop, "limitations_enabled": rules.nn_limitations, "quantity": quantity, "quote_collateral": quantity*entry,
             "collateral": quantity*entry*fx["buy_rate"], "entry_fx": fx,
             "entry_fee_quote": quantity*entry*rules.fee_rate,
             "entry_fee": quantity*entry*rules.fee_rate*fx["buy_rate"], "initial_risk_usd": quantity*unit_risk,
             "fee_rate": rules.fee_rate, "slippage_rate": rules.slippage_rate,
             "opened_ms": now, "last_bar_end": now, "tracking_gap": False, "realized_pnl": 0., "realized_pnl_quote": 0., **metadata}
    row["trades"].append(trade)
    document["cash"] -= trade["collateral"]+trade["entry_fee"]
    if abs(document["cash"]) < 1e-9:
        document["cash"] = 0.
    return trade





def open_combined_trade(document, name, signal, plan, quote, rules, now, *, fx=None):
    """Fund one qualified SMC setup, retaining its entry-time bracket forever."""
    row = document["assets"][name]
    plan_id = plan.get("id", plan.get("key"))
    if not isinstance(plan_id, str) or not plan_id:
        raise DataError("SMC setup has no durable identity")
    consumed = "smc:"+plan_id
    if consumed in row["consumed"]:
        raise DataError("SMC setup already consumed")
    identity = hashlib.sha256((name+":"+row["symbol"]+":"+consumed).encode()).hexdigest()[:24]
    if (any(t["id"] == identity for t in row["trades"])
            or any(t["trade_id"] == identity for t in document.get("deleted_paper_trades", {}).values())):
        raise DataError("SMC setup already traded or removed")
    if rules.nn_limitations and any(t["status"] == "active" for t in row["trades"]):
        raise DataError("Combined paper position already open")
    if (signal.get("label") != "BUY" or signal.get("signal_end") != (now//H4)*H4-1
            or plan.get("source") != "pine_smc" or plan.get("side") != "long"
            or type(plan.get("signal_end")) is not int or not 0 <= plan["signal_end"] < now
            or plan.get("expires_ms", 0) <= now or quote["asof_ms"] <= plan["signal_end"]):
        raise DataError("SMC setup and current Parente BUY confirmation are required")
    entry = finite(quote["ask"], "SMC entry quote", 1e-15)*(1+rules.slippage_rate)
    stop = finite(plan.get("stop"), "SMC stop", 1e-15)
    tp1 = finite(plan.get("tp1", plan.get("target")), "SMC TP1", 1e-15)
    target = finite(plan.get("exit_price", plan.get("target")), "SMC execution target", 1e-15)
    if not 0 < stop < entry < tp1 <= target:
        raise DataError("Actual filled entry is outside the SMC stop/target bracket")
    if exit_distance_pct(entry, target, "long") + 1e-9 < rules.smc_min_exit_distance_pct:
        raise DataError("Filled SMC entry leaves less than the minimum final-exit distance")
    from .smc_reachability import require_target
    require_target(plan, entry, rules, now, row["symbol"])
    stop_fill, target_fill = stop*(1-rules.slippage_rate), target*(1-rules.slippage_rate)
    fx = _account_fx(row["symbol"], fx, now)
    unit_cost = entry*(1+rules.fee_rate)*fx["buy_rate"]
    unit_risk = unit_cost-stop_fill*(1-rules.fee_rate)*fx["sell_rate"]
    reward = target_fill*(1-rules.fee_rate)*fx["sell_rate"]-unit_cost
    if unit_risk <= 0 or reward <= 0:
        raise DataError("SMC target does not cover recorded trading costs")
    book = portfolio(document)
    quantity = max(0., book["cash"])/unit_cost
    if rules.nn_limitations:
        risk = min(max(0., book["cost_equity"])*rules.risk_per_trade,
                   max(0., book["cost_equity"]*rules.max_total_risk-book["open_risk"]),
                   max(0., book["cost_equity"]-rules.paper_floor-book["open_risk"]))
        quantity = min(quantity, risk/unit_risk,
                       max(0., book["cost_equity"])*rules.max_allocation/unit_cost)
    if quantity <= 0 or (rules.nn_limitations and quantity*entry*fx["buy_rate"] < rules.minimum_notional):
        raise DataError("Insufficient combined paper cash or risk budget")
    metadata = copy.deepcopy(signal)
    trade = {**metadata, "id": identity, "asset": name, "symbol": row["symbol"],
             "strategy": "neural_network", "side": "long", "entry_origin": "smc_parente",
             "status": "active", "entry": entry, "entry_quote": quote["ask"],
             "entry_quote_ms": quote["asof_ms"], "stop": stop, "target": target,
             "tp1": tp1, "tp2": plan.get("tp2"), "smc_entry_plan": copy.deepcopy(plan),
             "smc_setup_id": plan_id, "smc_consumed_key": consumed,
             "decision_interval_ms": H4, "limitations_enabled": rules.nn_limitations,
             "quantity": quantity, "quote_collateral": quantity*entry,
             "collateral": quantity*entry*fx["buy_rate"], "entry_fx": fx,
             "entry_fee_quote": quantity*entry*rules.fee_rate,
             "entry_fee": quantity*entry*rules.fee_rate*fx["buy_rate"], "initial_risk_usd": quantity*unit_risk,
             "fee_rate": rules.fee_rate, "slippage_rate": rules.slippage_rate,
             "opened_ms": now, "last_bar_end": now, "tracking_gap": False, "realized_pnl": 0., "realized_pnl_quote": 0.}
    row["trades"].append(trade)
    row["consumed"].append(consumed)
    row["last_signal_end"] = max(row["last_signal_end"], signal["signal_end"])
    row["last_signal_interval_ms"] = H4
    document["cash"] -= trade["collateral"]+trade["entry_fee"]
    if abs(document["cash"]) < 1e-9:
        document["cash"] = 0.
    tp2_text = f"; TP2 {trade['tp2']:,.8f}" if trade["tp2"] is not None else "; TP2 pending"
    text = (f"{name} #{identity[:8]} | SMC + PARENTE PAPER BUY\n"
            f"{_entry_context(trade)}\n"
            f"Entry {entry:,.8f}; SMC stop {stop:,.8f}; TP1 {tp1:,.8f}{tp2_text}; "
            f"execution target {target:,.8f}; quantity {quantity:.8g}\n"
            f"{_entry_costs(trade)}\n"
            "Qualified SMC entry confirmed by Parente BUY. "
            "Exit at the recorded SMC stop/target or a later Parente SELL. No exchange order.")
    event_id = identity+":buy"
    queue_event(document, event_id, "telegram", text, now, trade)
    valid_until = min(signal["signal_end"]+H4+1, plan["expires_ms"])
    event = next(e for e in document["outbox"] if e["id"] == event_id)
    event.update(alert_type="paper_entry", trade_id=identity, asset=name,
                 symbol=trade["symbol"], observed_ms=now, expires_ms=valid_until,
                 valid_until_ms=valid_until)
    return trade

def normalize_manual_order(request_id, budget_mode, budget_usd=None, source_position_id=None, manual_price=None):
    if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise DataError("Invalid manual paper request ID")
    if budget_mode not in {"amount", "available"}:
        raise DataError("Choose an amount or use available cash")
    if budget_mode == "amount":
        if isinstance(budget_usd, bool):
            raise DataError("Enter a positive USD budget")
        budget_usd = finite(budget_usd, "paper budget", 1e-9)
    elif budget_usd not in (None, ""):
        raise DataError("Available-cash orders do not take a separate amount")
    else:
        budget_usd = None
    source_position_id = source_position_id or None
    if source_position_id is not None and (not isinstance(source_position_id, str)
                                          or not re.fullmatch(r"[0-9a-f]{32}", source_position_id)):
        raise DataError("Invalid source position ID")
    order = dict(request_id=request_id, budget_mode=budget_mode, budget_usd=budget_usd,
                 source_position_id=source_position_id)
    if manual_price not in (None, ""):
        if isinstance(manual_price, bool):
            raise DataError("Enter a positive manual entry price")
        order["manual_price"] = finite(manual_price, "manual entry price", 1e-15)
    return order


def find_manual_order(document, name, order):
    for tombstone in document.get("deleted_paper_trades", {}).values():
        previous = tombstone.get("manual_order", {})
        if previous.get("request_id") == order["request_id"]:
            raise DataError("This paper request was removed; reload to create a new entry")
    for row in document["assets"].values():
        for trade in row["trades"]:
            previous = trade.get("manual_order", {})
            if previous.get("request_id") == order["request_id"]:
                if trade["asset"] != name or previous != order:
                    raise DataError("This request ID already belongs to a different paper order; reload for a new order")
                return trade
    return None


def open_manual_trade(document, name, reading, quote, rules, now, order, *, fx=None):
    """An explicit cash-funded entry, separate from automatic candle progress."""
    existing = find_manual_order(document, name, order)
    if existing:
        return existing, False
    identity = hashlib.sha256(("manual:"+order["request_id"]).encode()).hexdigest()[:24]
    metadata = {"entry_origin": "manual", "manual_order": order, "label": "MANUAL",
                "signal_end": None, "probabilities": {}, "model_id": reading["model_id"],
                "entry_reading": reading, "decision_interval_ms": H4,
                "exit_after_signal_end": (now//H4)*H4-1,
                "entry_price_source": "manual_price" if "manual_price" in order else "live_ask"}
    trade = _record_entry(document, name, metadata, identity, quote, rules, now, order["budget_usd"],
                          entry_price=order.get("manual_price"), fx=fx)
    document["assets"][name]["last_result"] = "Manual paper entry opened; waiting for a later NN SELL" + (
        " or the recorded stop" if trade["stop"] is not None else "")
    # Do not consume a candle: an older pending SELL must still exit older trades.
    # No entry notification is queued for this interactive action.
    return trade, True


def _entry_decision(trade):
    return trade.get("entry_reading", trade)


def _model_description(model_id):
    return f"Parente 5/2 | model {model_id[:12]}"


def _probabilities_text(decision):
    return " | ".join(f"{name} {decision['probabilities'][name]*100:.2f}%"
                      for name in ("BUY", "HOLD", "SELL"))


def _entry_context(trade):
    decision = _entry_decision(trade)
    return (f"{trade['symbol']} | execution {utc(trade['opened_ms'])}\n"
            f"{_model_description(trade['model_id'])} | Binance spot/USDT 4H\n"
            f"Decision candle close {utc(decision['signal_end']+1)}\n"
            f"{_probabilities_text(decision)}")


def _entry_costs(trade):
    currency = trade["symbol"].split("/")[-1]
    return (f"Entry fee {trade['fee_rate']*100:.4f}% = {trade['entry_fee_quote']:.8f} {currency} / {trade['entry_fee']:.8f} USD; "
            f"slippage {trade['slippage_rate']*100:.4f}% (included in entry price).")


def close_trade(document, trade, price, now, reason, *, exit_signal=None, fx=None, price_ms=None, price_source=None):
    if trade["status"] != "active":
        return
    fill = price*(1-trade["slippage_rate"])
    fx = _account_fx(trade["symbol"], fx, now)
    fee_quote = fill*trade["quantity"]*trade["fee_rate"]
    fee = fee_quote*fx["sell_rate"]
    gross_quote = trade["quantity"]*(fill-trade["entry"])
    pnl_quote = gross_quote-trade["entry_fee_quote"]-fee_quote
    proceeds = fill*trade["quantity"]*fx["sell_rate"]
    gross = proceeds-trade["collateral"]
    pnl = gross-trade["entry_fee"]-fee
    document["cash"] += proceeds-fee
    document["realized_pnl"] += pnl
    trade.update(status="stopped" if reason == "stop" else "sold", exit=fill, closed_ms=now,
                 exit_fee=fee, exit_fee_quote=fee_quote, realized_pnl=pnl, realized_pnl_quote=pnl_quote,
                 exit_fx=fx, exit_reason=reason)
    if price_ms is not None:
        trade["exit_price_ms"] = price_ms
        trade["exit_price_source"] = price_source
    if exit_signal is not None:
        trade["exit_decision"] = copy.deepcopy(exit_signal)
    currency = trade["symbol"].split("/")[-1]
    trigger = {"stop": "SMC STOP" if trade["entry_origin"] == "smc_parente" else "RECORDED STOP",
               "target": "SMC TARGET", "Parente SELL": "PARENTE SELL"}.get(reason, reason.upper())
    decision = trade.get("exit_decision")
    model_context = (f"Exit {_model_description(decision['model_id'])} | Binance spot/USDT 4H\n"
                     f"Exit decision candle close {utc(decision['signal_end']+1)}\n"
                     f"{_probabilities_text(decision)}" if decision is not None else
                     f"Entry confirmation: {_model_description(trade['model_id'])} | Binance spot/USDT 4H\n"
                     f"Entry decision candle close {utc(_entry_decision(trade)['signal_end']+1)}")
    title = "SMC + PARENTE PAPER SELL" if trade.get("entry_origin") == "smc_parente" else "PARENTE PAPER SELL"
    text = (f"{trade['asset']} #{trade['id'][:8]} | {title} | {trigger}\n"
            f"{trade['symbol']} | execution {utc(now)}\n{model_context}\n"
            f"Exit {fill:,.8f}; quantity {trade['quantity']:.8g}\n"
            f"Native Gross P/L {gross_quote:.8f} {currency}; entry fee {trade['entry_fee_quote']:.8f} {currency}; "
            f"exit fee {fee_quote:.8f} {currency}\n"
            f"Native Net P/L {pnl_quote:.8f} {currency}\n"
            f"Account Net P/L {pnl:.8f} USD; entry fee {trade['entry_fee']:.8f} USD; exit fee {fee:.8f} USD\n"
            f"Recorded fee {trade['fee_rate']*100:.4f}% per side; "
            f"slippage {trade['slippage_rate']*100:.4f}% per fill (included in prices).\n"
            "No exchange order.")
    queue_event(document, trade["id"]+":exit", "telegram", text, now, trade)
