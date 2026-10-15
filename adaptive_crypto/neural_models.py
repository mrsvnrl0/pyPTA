"""The published Parente 5/2 is pyPTA's sole neural model.

The original strict NPZ loader validates its frozen weights and scaler. Live
inputs must match the author's Binance spot/USDT, completed 4H candle contract.
"""
from __future__ import annotations

from pathlib import Path
from functools import lru_cache

from .core import DataError
from .neural import DEFAULT_MODEL, NeuralModel
from .neural_input_contract import PARENTE_BINANCE_USDT

MODEL_ID = "parente_mlp_v1"


def load_selected_model(rules, settings_path: Path | None = None):
    if rules.nn_model_id != MODEL_ID:
        raise DataError("Parente 5/2 is the only supported neural model")
    path = DEFAULT_MODEL
    return NeuralModel(path)


@lru_cache(maxsize=4)
def _catalog_metadata(path: str, modified_ns: int, size: int):
    model = NeuralModel(Path(path))
    return {"coverage": sorted(model.volume_stats), "trained_through_ms": model.trained_through_ms,
            "artifact_id": model.identity}


def model_catalog(settings_path: Path | None = None, selections=()) -> list[dict]:
    """One fixed model, with calibration coverage from the verified artifact."""
    item = {"id": MODEL_ID, "label": "Parente 5/2", "available": False,
            "status": "Unavailable", "reason": "Parente model is unavailable",
            "coverage": [], "trained_through_ms": None, "live_eligibility_after_ms": None,
            "target_horizon": 2, "input_contract": PARENTE_BINANCE_USDT.metadata(),
            "artifact_id": None, "report_url": None}
    try:
        path = DEFAULT_MODEL
        info = path.stat()
        metadata = _catalog_metadata(str(path.resolve()), info.st_mtime_ns, info.st_size)
        item.update(metadata, available=True, status="Published trained model",
                    reason="Original Parente weights and frozen Binance volume calibration",
                    live_eligibility_after_ms=metadata["trained_through_ms"])
    except (DataError, OSError, ValueError, TypeError) as exc:
        item["reason"] = str(exc)
    return [item]
