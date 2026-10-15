"""Explicit user-funded NN paper entries; called under exclusive runtime activity."""
from .core import DataError, H4, M5, validate_candles
from .neural_ledger import find_manual_order, normalize_manual_order, open_manual_trade
from .neural_engine import current_signal
from .take_profit_alerts import fresh_quote
from .currency_conversion import validate_fx


def create_manual_paper(runtime, asset, **options):
    order = normalize_manual_order(**options)
    if not runtime.is_neural:
        raise DataError("Manual paper entries require the NN paper strategy")
    # A lost response can be retried even after its cash is spent or its trade exits.
    existing = find_manual_order(runtime.store.snapshot(), asset, order)
    if existing:
        return existing, False
    if runtime.paused or runtime.stop.is_set():
        raise DataError("Paper monitoring is paused or restarting")
    if asset not in runtime.assets:
        raise DataError("Choose an enabled paper asset")
    symbol = runtime.assets[asset]["symbol"]
    if order["source_position_id"]:
        source = next((p for p in runtime.positions.snapshot()["positions"]
                       if p["id"] == order["source_position_id"]), None)
        if (not source or source["status"] != "open" or source["side"] != "long" or source["asset"] != asset
                or source["symbol"] != symbol or source.get("position_type", "spot") != "spot"):
            raise DataError("The matching holding must be an open spot position in this asset")
    now = runtime.provider.now()
    interval = runtime.engine.decision_interval_ms
    high = [] if runtime.engine.feature_provider is not None else runtime.provider.candles(symbol, interval, now)
    low = high if interval == M5 else runtime.provider.candles(symbol, M5, now) if runtime.rules.nn_limitations else []
    # Keep inference before the executable quote and recheck clock/candle rollover.
    runtime.engine.read(asset, high, runtime.provider.now())
    quote = runtime.provider.quotes([symbol]).get(symbol)
    now = runtime.provider.now()
    reading = runtime.engine.read(asset, high, now)
    observed = now
    fx = runtime.paper_fx.quote(symbol.split("/")[-1], "USD", observed)
    now = runtime.provider.now()
    if now < observed:
        raise DataError("Exchange clock moved backwards during manual entry")
    fx = validate_fx(fx, symbol.split("/")[-1], now, "USD")
    signal = current_signal(reading, now)
    if not signal:
        raise DataError(reading.get("error") or "Waiting for a current NN reading for this asset")
    quote = fresh_quote(quote, now)
    if not quote or quote["asof_ms"] <= signal["signal_end"]:
        raise DataError("Waiting for a fresh current pair price")
    if runtime.rules.nn_limitations:
        validate_candles(low, M5, now, fresh=True)
        if low[-1].t != (now//M5-1)*M5:
            raise DataError("Waiting for current stop-monitoring candles")
        if (quote["ask"]/quote["bid"]-1)*10000 > runtime.rules.max_spread_bps:
            raise DataError("Spread exceeds the configured maximum")
    result = runtime.store.transaction(lambda document: open_manual_trade(
        document, asset, signal, quote, runtime.rules, now, order, fx=fx))
    runtime.wake.set()
    return result


def register_manual_paper_routes(app, runtime, payload):
    from flask import jsonify, redirect, request, url_for
    from .core import safe_error

    @app.post("/api/paper-positions/<record_id>/delete")
    def delete_paper_position(record_id):
        from .paper_deletion import delete_paper_trade
        try:
            with runtime.activity_gate.maintenance():
                payload(set())
                result = delete_paper_trade(runtime, record_id)
            if request.is_json or request.accept_mimetypes.best == "application/json":
                return jsonify(result)
            return redirect(url_for("paper_page")+"#live-paper", code=303)
        except KeyError:
            return jsonify({"error": "Current NN paper position not found"}), 404
        except (DataError, ValueError, TypeError) as exc:
            return jsonify({"error": safe_error(exc)}), 400
        except Exception as exc:
            return jsonify({"error": "Paper entry could not be removed: "+safe_error(exc)}), 503

    @app.post("/api/paper-positions")
    def open_manual_paper_position():
        try:
            with runtime.activity_gate.maintenance():
                body = payload({"asset", "request_id", "budget_mode"}, {"budget_usd", "source_position_id", "manual_price"})
                asset = body.pop("asset")
                body.pop("csrf_token", None)
                trade, created = create_manual_paper(runtime, asset, **body)
            if request.is_json or request.accept_mimetypes.best == "application/json":
                return jsonify({"paper_trade": trade, "created": created}), 201 if created else 200
            return redirect(url_for("paper_page")+"#live-paper", code=303)
        except (DataError, ValueError, TypeError) as exc:
            return jsonify({"error": safe_error(exc)}), 400
        except Exception as exc:
            return jsonify({"error": "Paper entry could not be confirmed. Retry this same request safely: "+safe_error(exc)}), 503
