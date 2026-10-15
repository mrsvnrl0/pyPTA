"""Market scan scheduling and thread-safe dashboard snapshots."""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import asdict
from pathlib import Path
import uuid
from .core import H4, M5, ENGINE_VERSION, DataError, safe_error
from .storage import portfolio
from .market_feed import DashboardMarketData
from .positions import PositionStore, position_pnl, validate_monitored_pairs
from .position_alerts import exit_action
from .position_options import POSITION_TYPES, monitored_position, position_type, target_basis
from .position_neural import PositionNeuralReader
from .take_profit_alerts import fresh_quote
from .gex import DeribitGEX
from .deribit import DeribitClient
from .deribit_settings import DeribitCredentials
from .connections import Connections
from .administration import ActivityGate
from .feed_diagnostics import record_feed_observation
from .currency_conversion import CurrencyConversions, validate_fx


class DashboardRuntime:
    def __init__(self, assets, rules, store, refresh=15, provider=None, position_store=None, gex_provider=None, settings_path=None, currency_provider=None):
        self.assets, self.rules, self.store, self.refresh = assets, rules, store, refresh
        self.is_smc = False
        self.is_neural = True
        self.is_combined = True
        self.high_interval, self.low_interval = H4, M5
        from .neural_engine import NeuralEngine
        self.engine = NeuralEngine(assets, rules, store, settings_path=settings_path)
        if getattr(store, "database_path", None) is not None and position_store is None:
            raise DataError("SQLite runtime requires explicit holdings from the same database")
        if getattr(store, "database_path", None) is not None and getattr(position_store, "database_path", None) != store.database_path:
            raise DataError("Paper and holdings must use the same SQLite database")
        self.positions = position_store or PositionStore(store.path.with_suffix(".positions.json"), market_mode=rules.market_mode)
        self.positions.market_mode = rules.market_mode
        validate_monitored_pairs(self.positions.snapshot(), assets)
        self.position_errors = {}
        self.settings_path = Path(settings_path).resolve() if settings_path is not None else None
        credential_directory = self.settings_path.parent if self.settings_path else Path(store.path).resolve().parent
        self.deribit_credentials = DeribitCredentials(credential_directory)
        self.connections = Connections(credential_directory)
        self.store.connections = self.positions.connections = self.connections
        self.deribit = DeribitClient(self.deribit_credentials.read)
        self.provider = provider if provider is not None else DashboardMarketData(self.deribit, preference=rules.market_data_source)
        self.paper_fx = currency_provider or getattr(self.provider, "fx_provider", None) or CurrencyConversions(self.provider)
        self.engine.clock = self.provider.now
        from .binance_market_data import BinanceParenteFeed
        self.neural_feature_provider = BinanceParenteFeed() if provider is None else None
        self.engine.feature_provider = self.neural_feature_provider
        self.position_neural = PositionNeuralReader(shared_engine=self.engine)
        self.paper_neural = PositionNeuralReader(shared_engine=self.engine)
        from .deribit_options import DeribitOptions
        self.deribit_options = DeribitOptions(self.deribit, currency_provider=self.paper_fx)
        self.gex = DeribitGEX(assets, provider=gex_provider if gex_provider is not None else self.deribit_options.fetch)
        self.lock = threading.RLock()
        self.activity_gate = ActivityGate()
        self.store.activity_gate = self.positions.activity_gate = self.activity_gate
        self.store.paper_alert_store = self.positions
        self.store.paper_namespace = rules.paper_namespace
        self.generation = uuid.uuid4().hex
        self.paused = bool(store.snapshot().get("monitoring_paused"))
        self.last_administration = None
        self.data = {}
        self.market = {}
        self.feed_observations = {}
        self.structure_baselined = set()
        self.chart_fallback = {}
        self.pine_charts = {}
        self.error = None
        self.updated_ms = None
        self.stop = threading.Event()
        self.wake = threading.Event()
        from .market_brief import MarketBrief
        self.market_brief = MarketBrief(self, credential_directory)

    def scan_once(self):
        with self.activity_gate.activity():
            if not self.stop.is_set():
                self._scan_once()

    def _scan_once(self):
        """One fixed-input Parente decision and selected-pair structure shared by all consumers."""
        from .perky_smc import run_perky_strategy
        from .smc_runtime import unavailable_smc
        from .pine_guidance import analyse_pine_market
        from .smc_position_context import monitor_smc_structure
        from .pine_alerts import monitor_pine_setups
        setup_interval = self.rules.smc_setup_minutes * 60000
        for name, cfg in self.assets.items():
            errors, cache = {}, {}
            try:
                now = self.provider.now()
            except Exception as exc:
                now = int(time.time() * 1000)
                errors["clock"] = safe_error(exc)
            if "clock" not in errors:
                try:
                    cache[H4] = self.provider.candles(cfg["symbol"], H4, now)
                except Exception as exc:
                    errors["4h"] = safe_error(exc)
            reading = self.engine.read(name, cache.get(H4, []), now, errors)
            for interval in dict.fromkeys((M5, setup_interval, H4)):
                if interval in cache:
                    continue
                try:
                    cache[interval] = self.provider.candles(cfg["symbol"], interval, now)
                except Exception as exc:
                    cache[interval] = []
                    errors[f"{interval // 60000}m"] = safe_error(exc)
            try:
                profile = self.gex.snapshot(name)
            except Exception as exc:
                profile = {"status": "unavailable", "error": safe_error(exc)}
            try:
                quote = self.provider.quotes([cfg["symbol"]]).get(cfg["symbol"])
                observed = self.provider.now()
                if "clock" not in errors:
                    quote = fresh_quote(quote, observed)
            except Exception as exc:
                quote, observed = None, int(time.time() * 1000)
                errors["quote"] = safe_error(exc)
            if errors.get("clock"):
                quote = None
            fx_quote = None
            fx_error = None
            if quote:
                try:
                    fx_quote = self.paper_fx.quote(cfg["symbol"].split("/")[-1], "USD", observed)
                    previous_observed = observed
                    observed = self.provider.now()
                    if observed < previous_observed:
                        raise DataError("Market clock moved backwards during currency conversion")
                    quote = fresh_quote(quote, observed)
                    fx_quote = validate_fx(fx_quote, cfg["symbol"].split("/")[-1], observed, "USD")
                except Exception as exc:
                    fx_quote = None
                    fx_error = safe_error(exc)
            reading = self.engine.read(name, cache.get(H4, []), observed, errors)
            high, low = cache.get(setup_interval, []), cache.get(M5, [])
            try:
                if errors.get("clock") or errors.get(f"{self.rules.smc_setup_minutes}m"):
                    raise DataError(errors.get("clock") or errors[f"{self.rules.smc_setup_minutes}m"])
                pine = run_perky_strategy(high, observed, self.rules)
            except Exception as exc:
                pine = unavailable_smc(self.rules, observed, safe_error(exc))
            structure = self.positions.transaction(lambda doc: monitor_smc_structure(
                doc, name, cfg["symbol"], pine, high, observed, self.rules.market_mode))
            from .smc_reachability import build_context
            reachability_context = build_context(pine, high, cache.get(H4, []), self.rules, observed,
                                                 cfg["symbol"], errors.get("clock") or errors.get("4h"))
            market = analyse_pine_market(pine, high, quote, self.rules,
                                         observed, cfg["symbol"], profile, reachability_context=reachability_context)
            declaration = getattr(self.provider, "candle_source", {})
            source = copy.deepcopy(declaration) if isinstance(declaration, dict) else {}
            metadata = getattr(self.provider, "source_metadata", None)
            if callable(metadata):
                try:
                    observation = metadata(cfg["symbol"], setup_interval, observed)
                    if isinstance(observation, dict):
                        source = copy.deepcopy(observation)
                except Exception as exc:
                    source = {"status": "unavailable", "error": safe_error(exc)}
            source.update(pair=cfg["symbol"], quote_asset=cfg["symbol"].split("/")[-1],
                          interval_ms=setup_interval, completed_only=True)
            market.update(pine_structure=pine, structure=structure, nn=reading, input_source=source,
                          account_currency="USD", account_conversion=copy.deepcopy(fx_quote),
                          account_conversion_error=fx_error)
            market["gex_context"]["pine_structure"] = pine
            self.positions.transaction(lambda doc: monitor_pine_setups(doc, name, cfg["symbol"], market, observed))
            with self.lock:
                self.pine_charts[name] = {"asset": name, "symbol": cfg["symbol"],
                    "interval_ms": setup_interval, "now_ms": observed,
                    "expires_ms": high[-1].end + high[-1].interval + 1 if high and pine.get("status") == "ready" else observed,
                    "candles": [{**asdict(b), "end": b.end} for b in high], "structure": copy.deepcopy(pine),
                    "structure_quality": copy.deepcopy(market.get("structure_quality", {})),
                    "input_source": copy.deepcopy(source), "quote": copy.deepcopy(quote),
                    "reachability": copy.deepcopy(market.get("reachability", {})),
                    "guide": copy.deepcopy(market.get("guide", {})),
                    "suggestion": copy.deepcopy(market.get("suggestion")), "error": market.get("error")}
                self.market[name] = market
                self.chart_fallback[name] = {"candles": copy.deepcopy(cache.get(H4, [])),
                                             "quote": copy.deepcopy(quote), "asof_ms": observed}
            try:
                if not self.paused:
                    result = self.engine.evaluate(name, quote, cache.get(H4, []), low, observed, errors,
                                                  smc_market=market, neural_reading=reading, fx_quote=fx_quote)
                else:
                    result = {"name": name, "symbol": cfg["symbol"], "asof_ms": observed,
                              "quote": quote, "errors": errors, "neural": {**reading, "status": "PAUSED"}}
                result.update(account_currency="USD", account_conversion=copy.deepcopy(fx_quote),
                              account_conversion_error=fx_error)
                with self.lock:
                    self.data[name] = result
                    record_feed_observation(self, name, market, errors, observed)
            except Exception as exc:
                with self.lock:
                    self.data[name] = {"name": name, "symbol": cfg["symbol"], "scan_error": safe_error(exc),
                                       "neural": {**reading, "status": "UNAVAILABLE"}}
            self._monitor_personal(name, cfg, self.positions.snapshot(), cache.get(H4, []), low,
                                   quote, observed, errors, profile, cache, reading)
        with self.lock:
            self.updated_ms = int(time.time() * 1000)

    def _monitor_personal(self, name, cfg, holdings, high, low, quote, observed, errors,
                          gex_profile=None, feed_cache=None, neural_reading=None):
        positions = [p for p in holdings["positions"] if p["asset"] == name and p["symbol"] == cfg["symbol"] and p["status"] == "open"]
        watches = [w for w in holdings.get("buy_watches", []) if w["asset"] == name and w["status"] in {"watching", "reached"}]
        if not positions and not watches:
            return
        rules = self.rules
        cache = dict(feed_cache or {self.high_interval: high, self.low_interval: low})
        def candles(interval):
            if interval not in cache:
                try:
                    cache[interval] = self.provider.candles(cfg["symbol"], interval, observed)
                except Exception:
                    cache[interval] = []
            return cache[interval]
        holding_high = candles(rules.smc_setup_minutes*60000)
        holding_low = candles(M5)
        neural_bars = candles(H4) if positions else []
        clock_error = errors.get("clock")
        try:
            now = self.provider.now()
        except Exception as exc:
            now, clock_error = int(time.time()*1000), safe_error(exc)
        quote = None if clock_error else fresh_quote(quote, now)
        try:
            profile = gex_profile if gex_profile is not None else self.gex.snapshot(name)
        except Exception as exc:
            profile = {"status": "unavailable", "error": safe_error(exc)}
        try:
            # Always calculate confluence for both target boxes, regardless of the paper model.
            from .pine_guidance import pine_confluence
            pine = self.market.get(name, {}).get("pine_structure", {})
            gex_context = pine_confluence(pine, quote, rules, profile, now, cfg["symbol"], bars=holding_high)
            gex_context["pine_structure"] = pine
            self.positions.monitor_buy_watches(name, cfg["symbol"], holding_high, holding_low, quote, rules, now, clock_error, gex_context)
            if positions:
                from .position_guidance import monitor_position_guidance
                reading = (neural_reading if neural_reading is not None else
                           self.engine.read(name, neural_bars, now, errors))
                if clock_error:
                    reading = {"signal": None, "error": clock_error, "expires_ms": now}
                self.positions.transaction(lambda doc: monitor_position_guidance(
                    doc, name, cfg["symbol"], holding_high, holding_low, quote, reading, rules, gex_context, now))
            with self.lock:
                self.position_errors.pop(name, None)
        except Exception as exc:
            with self.lock:
                self.position_errors[name] = safe_error(exc)

    def run(self):
        while not self.stop.is_set():
            start = time.monotonic()
            try:
                self.scan_once()
                self.error = None
            except Exception as exc:
                self.error = safe_error(exc)
            self.wake.wait(max(1, self.refresh-(time.monotonic()-start)))
            self.wake.clear()

    def snapshot(self):
        with self.lock:
            return self._snapshot()

    def _snapshot(self):
        state = self.store.snapshot()
        ai = self.connections.ai_configuration()
        with self.lock:
            data = copy.deepcopy(self.data)
            market = copy.deepcopy(self.market)
            updated = self.updated_ms
            position_errors = copy.deepcopy(self.position_errors)
        holdings = self.positions.snapshot()
        now = int(time.time()*1000)
        open_ids = {p["id"] for p in holdings["positions"] if p["status"] == "open"
                    and self.assets.get(p["asset"], {}).get("symbol") == p["symbol"]}
        open_watch_ids = {w["id"] for w in holdings.get("buy_watches", []) if w["status"] in {"watching", "reached"}
                          and self.assets.get(w["asset"], {}).get("symbol") == w["symbol"]}
        for event in holdings["outbox"]:
            event["is_current"] = bool(event["status"] != "cancelled" and not event.get("retired") and event.get("expires_ms", 0) > now
                                       and (open_ids.intersection(event.get("position_ids", []))
                                            or open_watch_ids.intersection(event.get("buy_watch_ids", []))
                                            or event.get("alert_type") == "pine_setup"))
        for watch in holdings.get("buy_watches", []):
            quote = fresh_quote(market.get(watch["asset"], data.get(watch["asset"], {})).get("quote"), now) if watch["id"] in open_watch_ids else None
            watch.update(current_ask=quote["ask"] if quote else None, current_quote_ms=quote["asof_ms"] if quote else None)
            watch["latest_alert"] = max((e for e in holdings["outbox"] if watch["id"] in e.get("buy_watch_ids", [])
                                          and not e.get("retired")), key=lambda e: e.get("observed_ms", e["created_ms"]), default=None)
        for position in holdings["positions"]:
            asset = position["asset"]
            position["review_required"] = not monitored_position(position, self.rules.market_mode)
            position["side_label"] = POSITION_TYPES[position_type(position, self.rules.market_mode)][1].upper()
            position["exit_action"] = "REVIEW RECORDED SHORT" if position["review_required"] else exit_action(position["side"])
            position["latest_alert"] = max((e for e in holdings["outbox"]
                                             if position["id"] in e.get("position_ids", [])
                                             and not (position.get("spot_correction") and e.get("retired"))),
                                            key=lambda e: (e.get("observed_ms", e["created_ms"]),
                                                           e.get("alert_type") == "target_reached", e["created_ms"]),
                                            default=None)
            position["feed_enabled"] = self.assets.get(asset, {}).get("symbol") == position["symbol"]
            reading = holdings.get("market_structure", {}).get(asset)
            if reading and reading.get("error"):
                reading = None
            row = market.get(asset, data.get(asset, {}))
            quote = fresh_quote(row.get("quote"), now) if position["feed_enabled"] and not row.get("errors", {}).get("clock") else None
            price = position["close"] if position["status"] == "closed" else (
                quote["bid" if position["side"] == "long" else "ask"] if quote else reading["close"] if reading else None)
            position.update(mark_price=price, mark_source="Recorded close" if position["status"] == "closed" else
                            "Live bid" if quote and position["side"] == "long" else "Live ask" if quote else f"Last completed {(reading or {}).get('interval_ms', self.low_interval)//60000}M close",
                            mark_ms=position["closed_ms"] if position["status"] == "closed" else
                            quote["asof_ms"] if quote else reading["end_ms"] if reading else None)
            from .position_guidance import target_advice
            structure = holdings.get("market_structure", {}).get(asset)
            position["structure_quality"] = copy.deepcopy(row.get("structure_quality", {}))
            position["structure_quality_current"] = bool(structure and not structure.get("error") and structure.get("expires_ms",0)>now)
            from .smc_reachability import assess_target
            position["target_reachability"] = {method: assess_target(row.get("reachability_context"),
                quote["bid" if position["side"] == "long" else "ask"] if quote else None,
                (position.get("position_targets", {}).get(method) or {}).get("price"),
                position["side"], self.rules, now, position["symbol"])
                for method in ("smc", "gex_smc")} if position["status"] == "open" else {}
            position["target_guidance"] = {method: target_advice(position, position.get("position_targets", {}).get(method), quote, now, structure)
                                           for method in ("smc", "gex_smc")}
            guidance = position.get("nn_guidance")
            if guidance and (not quote or guidance.get("expires_ms", 0) <= now):
                guidance.update(light="WAIT", action_label="WAIT FOR FRESH NN / QUOTE")
            advice = position.get("neural_advice")
            if advice:
                advice["is_current"] = bool(position["status"] == "open" and position["feed_enabled"] and advice.get("expires_ms", 0) > now and quote)
                if position["status"] == "open" and not advice["is_current"]:
                    advice.update(action="WAIT", action_label="WAIT FOR FRESH SIGNAL / QUOTE",
                                  reason="The previous action has expired. Waiting for a current NN reading and fresh exit quote.")
            if position["review_required"]:
                position.pop("take_profit", None)
                position["take_profit_error"] = "This record needs correction to a spot buy or explicit margin mode."
            if price is not None and not position["review_required"]:
                try:
                    position.update(position_pnl(position, price))
                except DataError as exc:
                    position["pnl_error"] = str(exc)
        return {"engine": ENGINE_VERSION, "data": data, "state": state,
                "is_neural": self.is_neural, "is_combined": self.is_combined, "market": market,
                "neural_model": (self._public_neural_model() if self.engine.model else None),
                "is_smc": self.is_smc, "setup_minutes": self.high_interval//60000,
                "entry_minutes": self.low_interval//60000,
                "holdings": holdings, "position_errors": position_errors,
                "portfolio": portfolio(state), "updated_ms": updated, "error": self.error,
                "telegram_configured": self.connections.status("telegram")["configured"],
                "ai_configured": ai["ready"], "ai": ai,
                "rules": asdict(self.rules),
                "quote_currency": "USD", "account_currency": "USD",
                "market_source": "Binance spot" if self.rules.market_data_source == "binance" else "Deribit first; Binance spot for unsupported pairs",
                "paper_strategy": "SMC setup and targets with Parente 5/2 confirmation",
                "administration": {"settings_file": str(self.settings_path) if self.settings_path else None,
                                   "paused": self.paused, "last_result": copy.deepcopy(self.last_administration)}}

    def _public_neural_model(self):
        model = self.engine.model
        details = {k: v for k, v in model.metadata.items() if k not in {"volume_stats", "training_assets"}}
        details.update(display_name="Parente 5/2", architecture_id="parente_mlp_v1",
                       artifact_id=model.identity, model_id="parente_mlp_v1",
                       coverage=sorted(getattr(model, "volume_stats", {})), lookback=1,
                       target_horizon=2, report_url=None,
                       input_source="Binance spot/USDT, completed 4H candles, original frozen base-volume calibration")
        return details
