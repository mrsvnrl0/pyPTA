"""Flask routes and presentation-only measurement formatting."""
from __future__ import annotations

import copy
import os
import secrets
import time
import uuid
from flask import Flask, jsonify, render_template, request, session, redirect, url_for, g
from .core import H4, DataError, Rules, finite, utc, safe_error
from .market_data import LiveCharts
from .gex import DeribitGEX, map_smc
from .administration import apply_settings, purge_database
from .settings_editor import read_editor, field_groups, save_editor, check_restart
from .trade_records import paper_records
from .position_options import cancel_disabled_paper_alerts


def number(value, places=3):
    try:
        return f"{finite(value):,.{places}f}"
    except (ValueError, TypeError):
        return "—"


def measurement(value):
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        # General values keep small quantities legible without exponent notation.
        return number(value, 8).rstrip("0").rstrip(".") if value != 0 else "0"
    if isinstance(value, dict):
        return "; ".join(f"{str(k).replace('_', ' ').capitalize()}: {measurement(v)}" for k, v in value.items()) or "—"
    if isinstance(value, (list, tuple)):
        return " · ".join(measurement(item) for item in value) or "—"
    return str(value)


def money(value, places=2, currency=None):
    text = number(value, places)
    if text == "—":
        return text
    currency = currency or "USD"
    if currency != "USD":
        return text + " " + currency
    return f"-${text[1:]}" if text.startswith("-") else f"${text}"


def pnl_money(value, currency, places=2):
    fiat_and_stable = {"USD", "USDC", "USDT", "USDE", "FDUSD", "DAI", "TUSD", "BUSD", "USDP",
                       "PYUSD", "GUSD", "USD1", "EUR", "EURI", "AEUR", "GBP", "JPY", "AUD", "BRL",
                       "TRY", "CHF", "CAD", "NZD", "ZAR", "CNY", "CNH", "HKD", "SGD", "RUB",
                       "UAH", "PLN", "RON", "ARS", "MXN", "IDR", "INR", "NGN", "VND", "KRW"}
    return money(value, places if currency in fiat_and_stable else max(8, places), currency)


def exact_measurement(value):
    if isinstance(value, dict):
        return "; ".join(f"{str(k).replace('_', ' ').capitalize()}: {exact_measurement(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return " · ".join(exact_measurement(v) for v in value)
    return repr(value) if isinstance(value, float) else measurement(value)


def multiple(value):
    text = number(value, 2)
    return text if text == "—" else f"{text}×"


def measurement_rows(evidence, required=False, places=3, currency=None):
    """Display-only labels and units; persisted evidence and comparisons stay raw."""
    value = evidence.get("required" if required else "measured")
    key = evidence.get("key", "")
    fields = {
        **{key: (label, "price") for key, label in {
            "liquidity_price": "Liquidity level", "sweep_extreme": "Sweep extreme", "return_close": "Return close",
            "sweep": "Sweep price", "return": "Return close", "zone_low": "Block low", "zone_high": "Block high",
            "gap_low": "Gap low", "gap_high": "Gap high", "midpoint": "Gap midpoint",
            "swing_high": "4H swing high", "swing_low": "4H swing low", "protected_level": "4H protected level",
            "swing_price": "Stop basis", "stop_price": "Stop level", "entry_price": "Entry price",
            "reference_price": "Bearish reference", "buy_price": "Spot-buy limit",
            "target_price": "Sweep take-profit"}.items()},
        "sweep_buffer_bps": ("Sweep beyond liquidity", "bps"),
        "body_atr": ("Body / ATR", "atr"),
        "close_location": ("Close in range (from low)", "fraction"),
        "close": ("Close price", "price"), "low": ("Low price", "price"),
        "open": ("Open price", "price"), "high": ("High price", "price"),
        "close_change": ("Close − open", "price"),
        "rvol": ("Relative volume", "multiple"),
        "roc_percent": ("ROC", "percent"), "ema": ("ROC signal (EMA)", "percent"),
        "age_bars": ("Age", "bars"), "zone": ("Retest zone", "price_range"),
        "liquidity_low": ("Liquidity low", "price"),
        "previous_atr": ("Previous 4H ATR", "price"),
        "sweep_depth_atr": ("Sweep depth", "atr"),
        "retest_age_bars": ("Paired retest age", "bars"),
        "retest_opened_ms": ("Paired retest opened", "timestamp"),
        "prior_bars": ("Available prior candles", "bars"),
        "replacement_confirmation_allowed": ("May confirm a replacement break", "general"),
        "age_minutes": ("Age since confirmation close", "minutes"),
    }

    def formatted(item, kind):
        if item is None:
            return "—"
        if isinstance(item, bool):
            return measurement(item)
        if isinstance(item, str):
            return {"context only": "Context only", "positive width": "Upper bound > lower bound",
                    "> 0 and > signal": "> 0% and > ROC signal", "> 0": "> " + money(0, places, currency)}.get(item, item)
        if kind in {"price_range", "rsi_range"} and isinstance(item, (list, tuple)) and len(item) == 2:
            unit = "price" if kind == "price_range" else "number"
            return f"{formatted(item[0], unit)} – {formatted(item[1], unit)}" + (" (inclusive)" if kind == "rsi_range" else "")
        if kind == "price":
            return money(item, places, currency)
        if kind == "timestamp":
            return utc(item)
        if kind == "multiple":
            return multiple(item)
        if kind in {"atr", "fraction", "percent", "bars", "minutes", "number", "bps"}:
            try:
                numeric = finite(item)
            except (ValueError, TypeError):
                return "—"
            text = number(numeric * 100 if kind == "fraction" else numeric, 2)
            if kind in {"bars", "minutes"}:
                text = text.rstrip("0").rstrip(".")
            return text + {"atr": " ATR", "fraction": "%", "percent": "%", "bars": " bars", "minutes": " minutes", "number": "", "bps": " bps"}[kind]
        return measurement(item)

    if isinstance(value, dict):
        rows = []
        for field, item in value.items():
            base, operator = field, ""
            if required:
                if base.startswith("max_"):
                    base, operator = base[4:], "≤ "
                else:
                    for suffix, symbol in (("_min", "≥ "), ("_max", "≤ "), ("_below", "< "), ("_above", "> ")):
                        if base.endswith(suffix):
                            base, operator = base[:-len(suffix)], symbol
                            break
            label, kind = fields.get(base, (str(base).replace("_", " ").capitalize(), "general"))
            text = formatted(item, kind)
            rows.append((label, (operator if not isinstance(item, str) and text != "—" else "") + text))
        return rows or [("Value", "—")]

    scalar = {
        "smc_bos": ("Structure-break close", "price", ""),
        "smc_mss": ("Structure price", "price", ""),
        "smc_ifvg": ("Inversion close", "price", ""),
        "smc_target": ("Liquidity target", "price", ""),
        "liquidity": ("Liquidity low", "price", ""),
        "entry": ("Entry estimate", "price", ""),
        "body": ("Body / ATR", "atr", "≥ "),
        "close": ("Close in range (from low)", "fraction", "≥ "),
        "volume": ("Relative volume", "multiple", "≥ "),
        "break": ("Close price", "price", "> "),
        "history": ("Completed history", "bars", "≥ "),
        "cross": ("Bars since crossover", "bars", "< "),
        "trend": ("Trend conditions met", "general", ""),
        "rsi": ("RSI", "rsi_range" if required else "number", ""),
        "discount": ("Overlap zone", "price_range", ""),
        "bullish": ("Close − open", "price", ""),
        "sweep": ("Sweep depth" if required else "Liquidity low", "atr" if required else "price", "> " if required else ""),
    }
    label, kind, operator = scalar.get(key, ("Value", "general", ""))
    text = formatted(value, kind)
    return [(label, (operator if required and not isinstance(value, str) and text != "—" else "") + text)]


def create_app(runtime, chart_provider=None, gex_provider=None, restart_callback=None):
    app = Flask(__name__)
    if os.environ.get("PYPTA_HTTPS_ONLY") == "1":
        app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")
    app.secret_key = secrets.token_hex(32)
    @app.context_processor
    def market_units():
        return {"quote_currency": "USD", "account_currency": "USD",
                "market_source_label": "Binance spot" if runtime.rules.market_data_source == "binance" else
                    "Deribit first; Binance spot when the pair is unsupported", "money": money,
                "account_money": money, "pnl_money": pnl_money}
    app.config.update(MAX_CONTENT_LENGTH=65536, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")
    def chart_fallback(name):
        with runtime.lock:
            return copy.deepcopy(runtime.chart_fallback.get(name))
    charts = LiveCharts(runtime.assets, provider=chart_provider if chart_provider is not None else runtime.provider, fallback=chart_fallback, interval=H4)
    chart_generation = runtime.generation
    if gex_provider is not None:
        runtime.gex = DeribitGEX(runtime.assets, provider=gex_provider)
    @app.before_request
    def coordinate_mutations():
        if request.method == "POST" and runtime.stop.is_set():
            return jsonify({"error": "Dashboard is restarting. Wait for it to return."}), 503
        if request.method == "POST" and request.endpoint not in {"apply_strategy_settings", "purge_strategy_database", "restart_dashboard", "update_position_alerts", "update_paper_alerts", "confirm_backup_restore", "open_manual_paper_position", "delete_paper_position", "refresh_pine_targets"}:
            g.activity = runtime.activity_gate.activity()
            g.activity.__enter__()
            if runtime.stop.is_set():
                return jsonify({"error": "Dashboard is restarting. Wait for it to return."}), 503

    @app.teardown_request
    def release_mutation(_error):
        activity = g.pop("activity", None)
        if activity is not None:
            activity.__exit__(None, None, None)

    @app.get("/")
    def dashboard():
        with runtime.lock:
            return render_dashboard("market_dashboard.html")

    @app.get("/smc-chart")
    def pine_chart_page():
        with runtime.lock:
            return render_dashboard("pine_chart.html")

    @app.get("/api/smc-chart/<name>")
    def pine_chart_data(name):
        if name not in runtime.assets:
            return jsonify({"error": "Unknown asset"}), 404
        with runtime.lock:
            data = copy.deepcopy(runtime.pine_charts.get(name, {}))
        if not data:
            data = {"asset": name, "symbol": runtime.assets[name]["symbol"], "candles": [],
                    "structure": {"status": "unavailable"}, "expires_ms": 0,
                    "error": "Waiting for the first Perky SMC scan"}
        response = jsonify(data)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/smc-script")
    def pine_source_download():
        from flask import send_file
        from pathlib import Path
        return send_file(Path(__file__).resolve().parent.parent/"scripts"/"pine"/"perky_smc_ultimate_v5.pine",
                         mimetype="text/plain", as_attachment=True,
                         download_name="perky_smc_ultimate_v5.pine")

    @app.get("/paper-trading")
    def paper_page():
        with runtime.lock:
            return render_dashboard("neural_dashboard.html", records=paper_records(runtime))

    @app.get("/positions")
    def positions_page():
        with runtime.lock:
            plan = None
            asset, plan_id = request.args.get("plan_asset"), request.args.get("plan_id")
            market = runtime.market.get(asset, {})
            candidate = market.get("suggestion")
            if candidate and str(candidate.get("id", candidate.get("key"))) == plan_id and market.get("guide", {}).get("expires_ms",0) > int(time.time()*1000):
                plan = {**copy.deepcopy(candidate), "asset": asset}
            return render_dashboard("positions_page.html", prefill_plan=plan)

    @app.get("/trade-records")
    def trade_records_page():
        with runtime.lock:
            return render_dashboard("trade_records.html", records=paper_records(runtime))

    @app.get("/settings")
    def settings_page():
        with runtime.lock:
            if session.get("generation") != runtime.generation:
                session["generation"] = runtime.generation
                session["csrf_token"] = secrets.token_hex(32)
            document, revision = read_editor(runtime)
            return render_template("settings.html", s=runtime.snapshot(), document=document,
                                   revision=revision, groups=field_groups(document), time=utc,
                                   active_assets=runtime.assets,
                                   applied_model_id=runtime.rules.nn_model_id,
                                   csrf_token=session.setdefault("csrf_token", secrets.token_hex(32)),
                                   restart_available=restart_callback is not None)

    @app.post("/api/settings/save")
    def save_settings():
        try:
            body = position_payload({"settings", "revision"})
            return jsonify(save_editor(runtime, body["settings"], body["revision"]))
        except (DataError, ValueError, TypeError, OSError) as exc:
            return jsonify({"error": safe_error(exc)}), 400

    @app.post("/api/dashboard/restart")
    def restart_dashboard():
        try:
            position_payload(set())
            if restart_callback is None:
                return jsonify({"error": "Restart is unavailable with this launcher."}), 503
            with runtime.activity_gate.maintenance(), runtime.lock:
                position_payload(set())
                if runtime.stop.is_set():
                    raise DataError("A restart is already in progress.")
                check_restart(runtime)
                runtime.stop.set()
                runtime.wake.set()
                restart_callback()
            return jsonify({"message": "Restarting dashboard…", "generation": runtime.generation}), 202
        except (DataError, ValueError, TypeError, OSError) as exc:
            return jsonify({"error": safe_error(exc)}), 400

    @app.get("/api/dashboard/status")
    def dashboard_status():
        return jsonify({"generation": runtime.generation, "restarting": runtime.stop.is_set(),
                        "strategy_model": runtime.rules.strategy_model})

    def render_dashboard(template=None, **context):
        if session.get("generation") != runtime.generation:
            session["generation"] = runtime.generation
            session["csrf_token"] = secrets.token_hex(32)
        csrf = session.setdefault("csrf_token", secrets.token_hex(32))
        return render_template(template or "neural_dashboard.html", s=runtime.snapshot(), refresh=runtime.refresh,
                                      num=number, measure=measurement, time=utc, money=money, account_money=money,
                                      pnl_money=pnl_money,
                                      multiple=multiple, measurement_rows=measurement_rows, exact_measurement=exact_measurement,
                                      now=int(time.time()*1000), digits={n: c["price_decimals"] for n, c in runtime.assets.items()},
                                      assets=runtime.assets, generation=runtime.generation, csrf_token=csrf,
                                      position_request_id=uuid.uuid4().hex, paper_request_id=uuid.uuid4().hex, **context)
    def position_payload(required, optional=()):
        payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
        if not isinstance(payload, dict):
            raise DataError("Expected position fields")
        token = request.headers.get("X-CSRF-Token") or payload.get("csrf_token")
        if not isinstance(token, str) or not session.get("csrf_token") or not secrets.compare_digest(token, session["csrf_token"]):
            raise DataError("This form has expired; reload the dashboard and try again")
        if session.get("generation") != runtime.generation:
            raise DataError("Settings or the database changed. Reload before submitting this form.")
        if set(payload)-set(required)-set(optional)-{"csrf_token"} or not set(required).issubset(payload):
            raise DataError("Missing or unexpected position fields")
        return payload

    from .dvol import register_dvol_routes
    register_dvol_routes(app, runtime, render_dashboard, position_payload)
    from .restore import register_restore_routes
    register_restore_routes(app, runtime, position_payload)
    from .deribit_settings import register_deribit_routes
    register_deribit_routes(app, runtime, position_payload)
    from .connection_settings import register_connection_routes
    register_connection_routes(app, runtime, position_payload)
    from .market_brief import register_market_brief_routes
    register_market_brief_routes(app, runtime, position_payload)
    from .manual_paper import register_manual_paper_routes
    register_manual_paper_routes(app, runtime, position_payload)

    def position_response(position, status=200):
        if request.is_json or request.accept_mimetypes.best == "application/json":
            return jsonify({"position": position}), status
        return redirect(url_for("trade_records_page")+"#manual-trades" if position.get("status") == "closed" else url_for("positions_page"), code=303)

    def administration_response(action, required):
        try:
            body = position_payload(required)
            if action is purge_database and body.get("confirm") != "purge":
                raise DataError("Confirm that all positions, buy watches and history should be cleared.")
            result = action(runtime, session["generation"])
            if request.is_json or request.accept_mimetypes.best == "application/json":
                return jsonify(result)
            return redirect(url_for("settings_page")+"#settings-database", code=303)
        except (DataError, ValueError, TypeError) as exc:
            return jsonify({"error": safe_error(exc)}), 400
        except Exception as exc:
            return jsonify({"error": "Could not complete the update: "+safe_error(exc)}), 500

    @app.post("/api/settings/apply")
    def apply_strategy_settings():
        return administration_response(apply_settings, set())

    @app.post("/api/database/purge")
    def purge_strategy_database():
        return administration_response(purge_database, {"confirm"})

    @app.post("/api/positions")
    def open_position():
        try:
            body = position_payload({"asset", "entry", "quantity", "request_id"},
                {"side", "position_type", "target_mode", "nn_target_mode", "momentum_alerts", "tp_alerts", "stop_price"})
            position, created = runtime.positions.open_position(runtime.assets, body["asset"], body.get("side"),
                body["entry"], body["quantity"], body["request_id"], int(time.time()*1000), body.get("target_mode", "smc"),
                body.get("position_type"), body.get("nn_target_mode", "gex_smc"),
                alert_boolean(body.get("momentum_alerts", True)), alert_boolean(body.get("tp_alerts", True)), None if body.get("stop_price") in (None, "") else body["stop_price"])
            runtime.wake.set()
            return position_response(position, 201 if created else 200)
        except (DataError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    def alert_boolean(value):
        if not request.is_json and value in ("true", "false"):
            value = value == "true"
        if type(value) is not bool:
            raise DataError("Choose On or Off for alerts")
        return value

    @app.post("/api/paper-positions/<record_id>/alerts")
    def update_paper_alerts(record_id):
        try:
            with runtime.activity_gate.maintenance():
                body = position_payload({"momentum_alerts", "tp_alerts"})
                if runtime.stop.is_set():
                    raise DataError("Dashboard is restarting; try again when it returns.")
                if not any(t["paper_key"] == record_id for t in paper_records(runtime)["active"]):
                    return jsonify({"error":"Active paper position not found"}), 404
                options = runtime.positions.set_paper_alerts(record_id, alert_boolean(body["momentum_alerts"]), alert_boolean(body["tp_alerts"]))
                preferences = runtime.positions.snapshot().get("paper_alert_preferences", {})
                runtime.store.transaction(lambda document: cancel_disabled_paper_alerts(document, preferences, runtime.store.paper_namespace))
            if request.is_json or request.accept_mimetypes.best == "application/json":
                return jsonify({"preferences": options})
            return redirect(url_for("positions_page"), code=303)
        except (DataError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/positions/<position_id>/smc-targets")
    def refresh_pine_targets(position_id):
        try:
            position_payload(set())
            with runtime.activity_gate.maintenance(), runtime.lock:
                now_ms = int(time.time()*1000)
                from .gex_targets import select_target, rules_for_target
                from .position_guidance import _target
                from .take_profit_alerts import fresh_quote
                def update(document):
                    position = next((p for p in document["positions"] if p["id"] == position_id), None)
                    if position is None:
                        raise KeyError(position_id)
                    if position["status"] != "open":
                        raise DataError("Only an open position can refresh its targets")
                    row = runtime.market.get(position["asset"], {})
                    context = row.get("gex_context", {})
                    quote = fresh_quote(row.get("quote"), now_ms)
                    if (context.get("source") != "pine_smc" or not quote
                            or now_ms >= quote["asof_ms"]+30000
                            or now_ms >= row.get("guide", {}).get("expires_ms", 0)):
                        raise DataError("Wait for current SMC Engine structure and a fresh quote")
                    # Re-evaluate evidence at the click time. Pine liquidity stays
                    # usable when GEX expires, but expired GEX cannot support TP2.
                    context = copy.deepcopy(context)
                    if context.get("ready") and now_ms >= context.get("valid_until_ms", 0):
                        context.update(ready=False, matches=[], reason="GEX evidence expired; refresh the market scan")
                    context["selected_ms"] = now_ms
                    selected_ms = now_ms
                    long = position["side"] == "long"
                    reference = max(position["entry"], quote["ask"]) if long else min(position["entry"], quote["bid"])
                    first = select_target([], [], rules_for_target(runtime.rules, "smc"), position["side"], reference, selected_ms, context)
                    if first is None:
                        raise DataError("No current untaken SMC liquidity is available beyond this position")
                    second = select_target([], [], rules_for_target(runtime.rules, "gex_smc"), position["side"], first["price"], selected_ms, context,
                                           exit_reference=position["entry"])
                    targets = {"smc": _target(first, runtime.rules, position["side"], now_ms), "gex_smc": None}
                    if second and second["target_selection"]["method"] == "gex_smc":
                        targets["gex_smc"] = _target(second, runtime.rules, position["side"], now_ms)
                    position["position_targets"] = targets
                    position["alert_revision"] = uuid.uuid4().hex
                    for event in document["outbox"]:
                        if position_id in event.get("position_ids",[]) and event.get("alert_type") in {"near_take_profit", "target_reached"} and event["status"] == "queued":
                            event.update(status="cancelled",retired=True,error="Targets refreshed from SMC Engine")
                    return position
                position = runtime.positions.transaction(update)
                runtime.wake.set()
            return position_response(position)
        except KeyError:
            return jsonify({"error":"Position not found"}),404
        except (DataError,TypeError,ValueError) as exc:
            return jsonify({"error":safe_error(exc)}),400

    @app.post("/api/positions/<position_id>/stop")
    def update_position_stop(position_id):
        try:
            body = position_payload({"stop_price"})
            position = runtime.positions.set_stop(position_id, None if body["stop_price"] in (None, "") else body["stop_price"], int(time.time()*1000))
            runtime.wake.set()
            return position_response(position)
        except KeyError:
            return jsonify({"error": "Position not found"}), 404
        except (DataError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/positions/<position_id>/alerts")
    def update_position_alerts(position_id):
        try:
            with runtime.activity_gate.maintenance():
                body = position_payload({"momentum_alerts", "tp_alerts"})
                if runtime.stop.is_set():
                    raise DataError("Dashboard is restarting; try again when it returns.")
                position = runtime.positions.set_alerts(position_id, alert_boolean(body["momentum_alerts"]),
                    alert_boolean(body["tp_alerts"]), int(time.time()*1000))
            return position_response(position)
        except KeyError:
            return jsonify({"error": "Position not found"}), 404
        except (DataError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/positions/<position_id>/close")
    def close_position(position_id):
        try:
            body = position_payload({"close"})
            position = runtime.positions.close_position(position_id, body["close"], int(time.time()*1000))
            return position_response(position)
        except KeyError:
            return jsonify({"error": "Position not found"}), 404
        except (DataError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/buy-watches")
    def open_buy_watch():
        try:
            body = position_payload({"asset", "reference_price", "buy_price", "quantity", "request_id"}, {"target_mode"})
            watch, created = runtime.positions.open_buy_watch(runtime.assets, body["asset"], body["reference_price"],
                body["buy_price"], body["quantity"], body["request_id"], int(time.time()*1000), body.get("target_mode", "smc"))
            if request.is_json or request.accept_mimetypes.best == "application/json":
                return jsonify({"buy_watch": watch}), 201 if created else 200
            return redirect(url_for("positions_page")+"#buy-watches", code=303)
        except (DataError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/buy-watches/<watch_id>/cancel")
    def cancel_buy_watch(watch_id):
        try:
            position_payload(set())
            watch = runtime.positions.cancel_buy_watch(watch_id)
            if request.is_json or request.accept_mimetypes.best == "application/json":
                return jsonify({"buy_watch": watch})
            return redirect(url_for("positions_page")+"#buy-watches", code=303)
        except KeyError:
            return jsonify({"error": "Buy watch not found"}), 404
        except (DataError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400
    @app.get("/api/state")
    def api_state():
        return jsonify(runtime.snapshot())
    @app.get("/api/feeds")
    def api_feeds():
        from .feed_diagnostics import build_feed_status
        response = jsonify(build_feed_status(runtime))
        response.headers["Cache-Control"] = "no-store"
        return response
    @app.get("/api/chart/<name>")
    def api_chart(name):
        nonlocal charts, chart_generation
        with runtime.lock:
            if chart_generation != runtime.generation:
                charts = LiveCharts(runtime.assets, provider=chart_provider if chart_provider is not None else runtime.provider, fallback=chart_fallback, interval=H4)
                chart_generation = runtime.generation
            current_charts = charts
        try:
            chart = current_charts.get(name)
        except KeyError:
            return jsonify({"error": "Unknown asset"}), 404
        return jsonify(chart), 503 if chart.get("stale") else 200
    @app.get("/api/gex/<name>")
    def api_gex(name):
        try:
            profile = runtime.gex.get(name)
        except KeyError:
            return jsonify({"error": "Unknown asset"}), 404
        with runtime.lock:
            row = copy.deepcopy(runtime.market.get(name, {}))
        result = map_smc(profile, row, int(time.time()*1000))
        response = jsonify(result)
        response.headers["Cache-Control"] = "no-store"
        return response, 503 if result.get("stale") else 200

    @app.get("/health")
    def health():
        snapshot = runtime.snapshot()
        fresh = snapshot["updated_ms"] is not None and int(time.time()*1000)-snapshot["updated_ms"] < max(60000, runtime.refresh*3000)
        good = fresh and not snapshot["error"] and len(snapshot["data"]) == len(runtime.assets)
        good = good and all(not r.get("errors") and not r.get("scan_error") for r in snapshot["data"].values())
        good = good and all(not r.get("neural", {}).get("error") for r in snapshot["data"].values())
        paused = snapshot["administration"]["paused"]
        return jsonify({"ok": bool(good and not paused), "paused": paused,
                        "last_scan_ms": snapshot["updated_ms"]}), 200 if good and not paused else 503
    return app
