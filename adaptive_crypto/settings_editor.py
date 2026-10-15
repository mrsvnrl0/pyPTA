"""Validated, versioned editing of the dashboard's complete settings document."""
from dataclasses import asdict, replace
import copy
import hashlib
import json
from pathlib import Path
import tempfile

from .core import DEFAULT_ASSETS, DataError, Rules, load_settings
from .storage import atomic_json
from .backups import backup_runtime


CHOICES = {
    "smc_reachability_mode": [("off","Off"),("advisory","Advisory only"),("enforce","Require supported reachability for new entries")],
    "smc_reach_bias_minutes": [(240,"4 hours"),(1440,"Daily (UTC)")],
    "market_data_source": [("deribit_first", "Deribit first; Binance when the pair is unavailable"),
                           ("binance", "Binance spot")],
    "perky_stop_mode": [("Structure", "Structure stop"), ("ATR", "ATR stop")],
    "market_mode": [("spot", "Spot"), ("margin", "Margin")],
    "smc_setup_minutes": [(1, "1 minute"), (5, "5 minutes"), (15, "15 minutes"),
                          (30, "30 minutes"), (240, "4 hours")],
}


def read_editor(runtime):
    path = runtime.settings_path
    if path is None:
        raise DataError("This dashboard has no settings file configured.")
    raw = path.read_bytes() if path.exists() else b"{}"
    saved = json.loads(raw.decode("utf-8-sig"))
    load_settings(path)
    document = {"assets": [dict(enabled=True, price_decimals=3) | entry
                           for entry in saved.get("assets", DEFAULT_ASSETS)],
                "refresh_seconds": saved.get("refresh_seconds", 15),
                "strategy": asdict(replace(Rules(**saved.get("strategy", {})), strategy_model="neural_network"))}
    return document, hashlib.sha256(raw).hexdigest()


def field_groups(document):
    """Expose controls used by the Perky SMC and Parente paper workflow."""
    groups = {name: [] for name in ("Market data", "Paper account and risk", "Parente paper controls", "Perky SMC strategy", "SMC target reachability")}
    defaults = asdict(Rules())
    general = {"fee_rate", "slippage_rate", "paper_equity", "paper_floor",
               "risk_per_trade", "max_total_risk", "max_allocation", "minimum_notional", "max_spread_bps"}
    neural = {"nn_limitations", "nn_stop_loss"}
    structure = {"smc_setup_minutes", "smc_stop_buffer_bps", "smc_tp_alert_bps",
                 "smc_gex_alignment_bps", "smc_gex_max_basis_bps", "smc_min_exit_distance_pct"}
    for key, value in document["strategy"].items():
        if key == "market_data_source":
            group = "Market data"
        elif key in general:
            group = "Paper account and risk"
        elif key in neural:
            group = "Parente paper controls"
        elif key.startswith("smc_reach"):
            group = "SMC target reachability"
        elif key.startswith("perky_") or key in structure:
            group = "Perky SMC strategy"
        else:
            continue
        label = key.replace("_", " ").capitalize()
        for acronym in ("nn", "smc", "atr", "rsi", "rvol", "gex", "roc", "mss", "ltf", "ob", "tp"):
            label = " ".join(word.upper() if word.lower() == acronym else word for word in label.split())
        label = {"market_data_source": "SMC, charts and position price source",
                 "paper_equity": "Paper account equity (USD)",
                 "paper_floor": "Paper account floor (USD)",
                 "minimum_notional": "Minimum paper notional (USD)",
                 "smc_setup_minutes": "Strategy chart timeframe",
                 "smc_stop_buffer_bps": "Position structure stop buffer (bps)",
                 "smc_min_exit_distance_pct": "Minimum SMC final-exit distance (%)",
                 "smc_gex_targets": "Use GEX confluence for existing-position targets",
                 "smc_gex_alignment_bps": "Liquidity/GEX alignment tolerance (bps)",
                 "smc_gex_max_basis_bps": "Maximum options/spot basis (bps)",
                 "perky_sweep_len": "Sweep pivot length (bars)",
                 "perky_internal_len": "Internal structure length (bars)",
                 "perky_swing_len": "Swing structure length (bars)",
                 "perky_min_score": "Minimum confluence score (0–10)",
                 "perky_htf_agreement": "Required higher-timeframe agreement (0–4)",
                 "perky_cooldown_bars": "Signal cooldown (bars)",
                 "perky_gate_all": "Apply score and cooldown gates to every signal type",
                 "perky_killzone_only": "Allow entries only during London / New York kill zones",
                 "perky_require_fvg": "Require directional FVG confluence",
                 "perky_require_ob": "Require directional order-block confluence",
                 "perky_require_choch": "Require a confirmed CHoCH",
                 "perky_require_smc_bias": "Require aligned SMC bias",
                 "perky_ema_filter": "Require EMA trend alignment",
                 "perky_rsi_filter": "Block exhausted RSI entries",
                 "perky_stop_mode": "Stop calculation",
                 "perky_stop_atr_mult": "ATR stop distance multiplier",
                 "perky_structure_sl_buffer_atr": "Structure stop buffer (ATR)",
                 "perky_score_sweep": "Add sweep points to the confluence score",
                 "perky_score_choch": "Add CHoCH points to the confluence score",
                 "perky_score_fvg": "Add FVG points to the confluence score",
                 "perky_score_ob": "Add order-block points to the confluence score",
                 "perky_score_killzone": "Add kill-zone points to the confluence score",
                 "perky_score_ema": "Add EMA points to the confluence score",
                 "perky_score_rsi": "Add RSI points to the confluence score",
                 "perky_score_htf": "Add higher-timeframe points to the confluence score",
                 "perky_score_volume": "Add volume points to the confluence score",
                 "smc_reachability_mode": "Reachability mode",
                 "smc_reach_bias_minutes": "Higher timeframe for bias and range",
                 "smc_reach_lookback_bars": "Maximum higher-timeframe history (bars)",
                 "smc_reach_swing_len": "Higher-timeframe swing confirmation (left/right bars)",
                 "smc_reach_atr_period": "Reachability ATR period (bars)",
                 "smc_reach_event_bars": "Recent DOL evidence window (chart bars)",
                 "smc_reach_entry_zone_pct": "Maximum entry depth from favourable range edge (%)",
                 "smc_reach_max_atr": "Maximum target distance (higher-timeframe ATR)",
                 "smc_reach_balance_bars": "Balance window (higher-timeframe bars)",
                 "smc_reach_balance_atr": "Balanced-range maximum span (ATR)",
                 "smc_reach_equal_bps": "Equal-high/low tolerance (bps)",
                 "smc_reach_fvg_min_atr": "Minimum FVG width (chart ATR)",
                 "smc_reach_fvg_fill_pct": "FVG interaction depth (%)",
                 "smc_reach_require_bias": "Require aligned higher-timeframe bias",
                 "smc_reach_require_cycle": "Require confirmed liquidity-cycle evidence",
                 "nn_limitations": "Apply paper account limits",
                 "nn_stop_loss": "Manual paper fallback stop fraction"}.get(key, label)
        kind = type(defaults[key])
        groups[group].append({"key": key, "label": label,
                              "value": value, "kind": "bool" if kind is bool else "int" if kind is int else "number" if kind is float else "text",
                              "choices": CHOICES.get(key)})
    return groups


def validate_document(document, directory):
    if not isinstance(document, dict) or set(document) != {"assets", "strategy", "refresh_seconds"}:
        raise DataError("Expected assets, strategy and refresh_seconds.")
    if not isinstance(document["assets"], list) or not 1 <= len(document["assets"]) <= 100:
        raise DataError("Provide between 1 and 100 currency pairs.")
    names, pairs = set(), set()
    from .core import normalise_pair
    for entry in document["assets"]:
        if not isinstance(entry, dict) or set(entry) != {"name", "symbol", "enabled", "price_decimals"}:
            raise DataError("Each currency pair needs name, symbol, enabled and price_decimals.")
        if not isinstance(entry["name"], str) or not isinstance(entry["symbol"], str):
            raise DataError("Asset names and currency pairs must be text.")
        name, pair = entry["name"].strip().upper(), normalise_pair(entry["symbol"])
        if not name.isalnum() or len(name) > 30 or name in names or pair in pairs:
            raise DataError("Asset names and currency pairs must be distinct, including disabled rows.")
        if name != pair.split("/")[0]:
            raise DataError(f"{pair}: asset name must match the pair base asset.")
        from .core import validate_feed_pair
        validate_feed_pair(pair)
        if type(entry["enabled"]) is not bool or type(entry["price_decimals"]) is not int or not 0 <= entry["price_decimals"] <= 10:
            raise DataError("Choose Enabled or Disabled and a whole number of price decimals from 0 to 10.")
        names.add(name)
        pairs.add(pair)
    # Use the same validation as startup without touching the saved file.
    with tempfile.TemporaryDirectory(prefix="settings-check-", dir=directory) as folder:
        path = Path(folder)/"settings.json"
        path.write_text(json.dumps(document, allow_nan=False), encoding="utf-8")
        return load_settings(path)


def validate_provider_pairs(provider, assets, rules):
    """Resolve selected pairs without changing the running provider preference."""
    validator = getattr(provider, "validate_pair", None)
    if callable(validator):
        for cfg in assets.values():
            validator(cfg["symbol"], preference=rules.market_data_source)


def save_editor(runtime, document, revision):
    with runtime.lock:
        if runtime.stop.is_set():
            raise DataError("The dashboard is restarting. Wait for it to return before saving.")
        _, current_revision = read_editor(runtime)
        if revision != current_revision:
            raise DataError("Settings changed in another tab or on disk. Reload before saving.")
        assets, rules, _ = validate_document(document, runtime.settings_path.parent)
        if rules.strategy_model != "neural_network":
            raise DataError("Paper trading uses the combined SMC + Parente strategy.")
        validate_provider_pairs(runtime.provider, assets, rules)
        path = runtime.settings_path
        backup = backup_runtime(runtime, action="settings-edit")
        atomic_json(path, copy.deepcopy(document))
        _, revision = read_editor(runtime)
        return {"message": "Settings saved. Apply saved settings below to update the combined SMC + Parente strategy.",
                "revision": revision, "backup": str(backup)}


def check_restart(runtime):
    """Check the next model and ledger before stopping the working dashboard."""
    document, _ = read_editor(runtime)
    assets, rules, _ = validate_document(document, runtime.settings_path.parent)
    rules = replace(rules, strategy_model="neural_network")
    validate_provider_pairs(runtime.provider, assets, rules)
    from .neural_models import load_selected_model
    load_selected_model(rules)
    from .neural_ledger import prepare_neural
    from .positions import prepare_positions, validate_monitored_pairs
    namespace = "neural"
    if runtime.store.database_path:
        documents = runtime.store._backend.database.documents()
        raw = json.dumps(documents[namespace]).encode() if namespace in documents else None
    else:
        # JSON namespaces are siblings of the active store (same naming as startup).
        from .state_paths import StatePaths
        base = getattr(runtime, "state_base", runtime.store.path)
        path = StatePaths(base).sources[namespace]
        raw = path.read_bytes() if path.exists() else None
    prepared, _ = prepare_neural(raw, assets, rules)
    holdings, _ = prepare_positions(json.dumps(runtime.positions.snapshot()).encode(), rules.market_mode)
    validate_monitored_pairs(holdings, assets)
    for name, record in prepared["assets"].items():
        if any(trade["status"] == "active" for trade in record["trades"]):
            if name not in assets:
                raise DataError(f"Keep {name} enabled while its combined paper trade is active")
