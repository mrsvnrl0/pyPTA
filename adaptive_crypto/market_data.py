"""Selected-pair charts cached independently from neural signal evaluation."""
from __future__ import annotations

import copy
import threading
import time

from .core import H4, Candle, DataError, finite, safe_error


class LiveCharts:
    """Serve sixteen completed candles from the selected dashboard feed."""

    def __init__(self, assets, provider=None, ttl=15, fallback=None, interval=H4):
        self.assets = {name: cfg["symbol"] for name, cfg in assets.items()}
        self.provider = provider
        self.ttl = finite(ttl, "chart cache lifetime", 0)
        self.fallback = fallback
        self.interval = interval
        self._locks = {name: threading.Lock() for name in self.assets}
        self._provider_lock = threading.Lock()
        self._cache = {}

    def _fetch(self, symbol):
        if self.provider is None or not hasattr(self.provider, "chart_candles"):
            raise DataError("A selected-pair chart provider is required")
        with self._provider_lock:
            now = int(self.provider.now())
            bars = self.provider.chart_candles(symbol, self.interval, now)
        if not bars or bars[-1].t != (now // self.interval - 1) * self.interval:
            raise DataError("Latest completed Deribit chart candle is missing")
        source = None
        metadata = getattr(self.provider, "source_metadata", None)
        if callable(metadata):
            source = metadata(symbol, self.interval, now)
        return {"symbol": symbol, "interval_ms": self.interval,
                "candles": [{"t": b.t, "o": b.o, "h": b.h, "l": b.l, "c": b.c,
                              "v": b.v, "current": False} for b in bars[-16:]],
                "asof_ms": now, "stale": False, "degraded": False, "error": None,
                "input_source": source}

    def _fallback_result(self, name, symbol):
        if self.fallback is None:
            return None
        try:
            snapshot = self.fallback(name)
        except Exception:
            return None
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("candles"), list):
            return None
        candles = []
        for bar in snapshot["candles"][-16:]:
            try:
                if isinstance(bar, Candle):
                    stamp, o, h, low, c = bar.t, bar.o, bar.h, bar.l, bar.c
                else:
                    stamp, o, h, low, c = bar["t"], bar["o"], bar["h"], bar["l"], bar["c"]
                candles.append({"t": int(stamp), "o": float(o), "h": float(h),
                                "l": float(low), "c": float(c), "current": False})
            except (KeyError, TypeError, ValueError, AttributeError):
                return None
        if not candles:
            return None
        asof_ms = snapshot.get("asof_ms")
        return {"symbol": symbol, "interval_ms": self.interval, "candles": candles,
                "asof_ms": asof_ms, "stale": True, "degraded": False, "error": None}

    def get(self, name):
        if name not in self.assets:
            raise KeyError(name)
        with self._locks[name]:
            cached = self._cache.get(name)
            if cached is not None and time.monotonic() < cached["expires"]:
                return copy.deepcopy(cached["result"])
            try:
                result = self._fetch(self.assets[name])
            except Exception as exc:
                result = self._fallback_result(name, self.assets[name])
                if result is None and cached:
                    result = copy.deepcopy(cached["result"])
                    result.update(stale=True, degraded=False)
                if result is None:
                    result = {"symbol": self.assets[name], "interval_ms": self.interval,
                              "candles": [], "asof_ms": None, "degraded": False}
                result.update(stale=not result.get("degraded", False),
                              error=safe_error(exc) or "Chart data unavailable")
            self._cache[name] = {"expires": time.monotonic() + self.ttl, "result": result}
            return copy.deepcopy(result)
