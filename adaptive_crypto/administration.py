"""Coordinated settings reload and backed-up dashboard resets."""
from contextlib import contextmanager
from dataclasses import asdict
import copy
import json
import threading
import time
import uuid

from .core import DataError, Rules, load_settings
from .backups import backup_runtime
from .gex import DeribitGEX
from .persistence import SQLiteDocument
from .positions import prepare_positions, validate_positions, validate_monitored_pairs


class ActivityGate:
    """Scans, requests and deliveries may overlap; maintenance excludes them all."""
    def __init__(self):
        self.condition = threading.Condition()
        self.active = 0
        self.waiting = 0
        self.exclusive = False

    @contextmanager
    def activity(self):
        with self.condition:
            self.condition.wait_for(lambda: not self.exclusive and not self.waiting)
            self.active += 1
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    @contextmanager
    def maintenance(self):
        with self.condition:
            self.waiting += 1
            try:
                self.condition.wait_for(lambda: not self.exclusive and not self.active)
                self.exclusive = True
            finally:
                self.waiting -= 1
        try:
            yield
        finally:
            with self.condition:
                self.exclusive = False
                self.condition.notify_all()


def _check(runtime, generation):
    if generation != runtime.generation:
        raise DataError("The dashboard changed since this page loaded. Reload before trying again.")
    if runtime.settings_path is None:
        raise DataError("Strategy controls require a configured settings file.")
    if runtime.stop.is_set():
        raise DataError("The dashboard is stopping; restart before changing its database.")


def _documents(runtime):
    backend = runtime.store._backend
    if isinstance(backend, SQLiteDocument):
        db = backend.database
        return db.documents(), db.information()["contexts"]
    namespace = runtime.rules.paper_namespace
    return {namespace: runtime.store.snapshot(), "positions": runtime.positions.snapshot()}, {
        namespace: {"assets": runtime.assets, "rules": asdict(runtime.rules)},
        "positions": {"market_mode": runtime.rules.market_mode}}


def _backup(runtime, action, documents, contexts):
    return str(backup_runtime(runtime, action=action, documents=documents, contexts=contexts))


def _save(runtime, documents, contexts, backup):
    stores = {runtime.rules.paper_namespace: runtime.store, "positions": runtime.positions}
    if runtime.store.database_path:
        runtime.store._backend.database.replace_documents(documents, contexts)
        for name, store in stores.items():
            store._backend.context = copy.deepcopy(contexts[name])
        return
    previous = {name: store.snapshot() for name, store in stores.items()}
    saved = []
    try:
        for name, store in stores.items():
            # Include the attempted write: a filesystem failure may follow replacement.
            saved.append(name)
            store._backend.save(documents[name])
    except Exception:
        try:
            for name in reversed(saved):
                stores[name]._backend.save(previous[name])
        except Exception as exc:
            runtime.paused = True
            runtime.stop.set()
            raise DataError(f"Database update and rollback failed. Monitoring stopped. Restore backup: {backup}") from exc
        raise


def _finish(runtime, message, backup):
    runtime.generation = uuid.uuid4().hex
    runtime.data = {}
    runtime.market = {}
    runtime.feed_observations = {}
    runtime.chart_fallback = {}
    runtime.pine_charts = {}
    runtime.position_errors = {}
    runtime.updated_ms = None
    runtime.error = None
    runtime.last_administration = {"message": message, "backup": backup, "at_ms": int(time.time()*1000)}
    runtime.wake.set()
    return copy.deepcopy(runtime.last_administration)


def apply_settings(runtime, generation):
    with runtime.activity_gate.maintenance(), runtime.lock, runtime.store.lock, runtime.positions.lock:
        _check(runtime, generation)
        assets, rules, refresh = load_settings(runtime.settings_path)
        from .settings_editor import validate_provider_pairs
        validate_provider_pairs(runtime.provider, assets, rules)
        from dataclasses import replace
        return _apply_neural(runtime, assets, replace(rules, strategy_model="neural_network"), refresh)


def purge_database(runtime, generation):
    with runtime.activity_gate.maintenance(), runtime.lock, runtime.store.lock, runtime.positions.lock:
        _check(runtime, generation)
        documents, contexts = _documents(runtime)
        proposed = {}
        for name in documents:
            context = contexts[name]
            if name == "positions":
                proposed[name], _ = prepare_positions(None, runtime.rules.market_mode)
            elif name == "neural":
                from .neural_ledger import prepare_neural
                proposed[name], _ = prepare_neural(None, context["assets"], Rules(**context["rules"]))
            else:
                raise DataError(f"Cannot purge unknown database section: {name}")
        from .neural_ledger import validate as validate_neural
        validate_neural(proposed["neural"])
        validate_positions(proposed["positions"])
        proposed["neural"]["monitoring_paused"] = True
        backup = _backup(runtime, "purge", documents, contexts)
        _save(runtime, proposed, contexts, backup)
        runtime.paused = True
        return _finish(runtime, "Database cleared. Monitoring is paused. Apply settings to resume.", backup)


def _apply_neural(runtime, assets, rules, refresh):
    from .neural_engine import NeuralEngine
    from .neural_ledger import prepare_neural
    documents, contexts = _documents(runtime)
    previous_contexts = copy.deepcopy(contexts)
    proposed = copy.deepcopy(documents)
    validate_monitored_pairs(proposed["positions"], assets)
    proposed["neural"], _ = prepare_neural(None if runtime.paused else json.dumps(proposed["neural"]).encode(), assets, rules)
    proposed["positions"], _ = prepare_positions(json.dumps(proposed["positions"]).encode(), rules.market_mode)
    proposed["neural"].pop("monitoring_paused", None)
    for document in (proposed["neural"], proposed["positions"]):
        for event in document["outbox"]:
            if event["status"] == "queued":
                event.update(status="cancelled", error="Settings applied; waiting for fresh evidence.")
    engine = NeuralEngine(assets, rules, runtime.store, settings_path=runtime.settings_path)
    engine.clock = runtime.provider.now
    engine.feature_provider = getattr(runtime, "neural_feature_provider", None)
    if engine.model_error:
        raise DataError(engine.model_error)
    for name, record in proposed["neural"]["assets"].items():
        if any(trade["status"] == "active" for trade in record["trades"]):
            if name not in assets:
                raise DataError(f"Keep {name} enabled while its neural paper trade is active")
            asset = assets[name]["symbol"].split("/")[0]
            if asset not in engine.model.volume_stats:
                raise DataError(f"Selected NN model has no trained artifact for {asset}")
    contexts["neural"] = {"assets": assets, "rules": asdict(rules)}
    contexts["positions"] = {"market_mode": rules.market_mode}
    backup = _backup(runtime, "settings", documents, previous_contexts)
    _save(runtime, proposed, contexts, backup)
    runtime.assets, runtime.rules, runtime.refresh = assets, rules, refresh
    change_source = getattr(runtime.provider, "set_preference", None)
    if callable(change_source):
        change_source(rules.market_data_source)
    runtime.low_interval = 300000  # NN stop monitoring uses fixed 5M candles, independently of SMC.
    runtime.position_neural.shared_engine = runtime.paper_neural.shared_engine = engine
    runtime.engine, runtime.gex = engine, DeribitGEX(assets, provider=runtime.gex.provider)
    runtime.positions.market_mode = rules.market_mode
    runtime.paused = False
    return _finish(runtime, "Combined SMC and Parente settings applied. Existing trades retain their recorded plans and costs.", backup)


