"""Unavailable Perky strategy snapshots for stale or missing market data."""
from dataclasses import asdict
from .perky_smc import settings_from_rules


def unavailable_smc(settings, now, error):
    config = settings_from_rules(settings)
    perky_settings = {"mode": "Filtered", **asdict(config)}
    return {"source": "pine_smc", "script_title": "Perky SMC Ultimate V5",
            "status": "unavailable", "error": error, "asof_ms": now,
            "last_closed_ms": None, "last_bar_ms": None, "interval_ms": None,
            "close": None, "regime": "neutral", "internal_bias": 0, "swing_high": None,
            "swing_low": None, "events": [], "liquidity": [], "zones": [],
            "quality": {}, "structure_quality": {}, "signal": None,
            "signals": [], "qualified_signals": [], "settings": perky_settings,
            "perky": {"source": "perky_smc_v5", "script_title": "Perky SMC Ultimate V5",
                      "status": "unavailable", "error": error, "asof_ms": now,
                      "last_closed_ms": None, "last_bar_ms": None, "interval_ms": None,
                      "close": None, "regime": "neutral", "internal_bias": 0, "events": [],
                      "signal": None, "signals": [], "qualified_signals": [],
                      "settings": perky_settings}}


__all__ = ["unavailable_smc"]
