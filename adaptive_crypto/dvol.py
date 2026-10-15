"""Deribit DVOL implied-volatility index page, API and calculation helpers.

DVOL is Deribit's official annualized volatility index for BTC and ETH,
expressed in percentage points (a value of 36.65 means 36.65%). It is an
independent asset-volatility index: it is never derived from perpetual
prices, converted by quote-currency FX, or relabelled for unsupported assets.
"""
from __future__ import annotations

import math
import threading
import time

from .core import DataError
from .deribit import DeribitClient
from .take_profit_alerts import fresh_quote

_UNSET = object()

# get_volatility_index_data accepts BTC and ETH; SOL has no published index.
SUPPORTED_CURRENCIES = ("BTC", "ETH")

# Whitelisted resolution keys and their nominal bucket length in seconds.
RESOLUTIONS = {"1": 1, "60": 60, "3600": 3600, "43200": 43200, "1D": 86400}

PERIODS = {
    "24h": 24 * 3600,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "90d": 90 * 86400,
}

PERIOD_RESOLUTION = {"24h": "3600", "7d": "3600", "30d": "43200", "90d": "1D"}

DEFAULT_PERIOD = "7d"
CACHE_SECONDS = 60.0
MAX_PAGES = 8

MOVE_HORIZONS = (1, 7, 30)


def _finite(value, label, minimum=None):
    if isinstance(value, bool):
        raise DataError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise DataError(f"Invalid {label}")
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise DataError(f"Invalid {label}")
    return number


def expected_move(price, dvol, days):
    """Approximate one-standard-deviation move over ``days``.

    ``dvol`` is annualized volatility in percentage points. The result is a
    volatility range around the current price, explicitly not a price forecast.
    """
    if (isinstance(days, bool) or not isinstance(days, (int, float))
            or not math.isfinite(days) or days <= 0):
        raise DataError("Move horizon must be a positive number of days")
    price = _finite(price, "index price", 0.0)
    if price <= 0:
        raise DataError("Index price must be positive")
    dvol = _finite(dvol, "DVOL", 0.0)
    return price * (dvol / 100.0) * math.sqrt(days / 365.0)


def parse_row(row):
    """Validate one ``[timestamp, open, high, low, close]`` DVOL point."""
    if not isinstance(row, (list, tuple)) or len(row) != 5:
        raise DataError("Deribit returned an invalid DVOL data point")
    timestamp, open_, high, low, close = row
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise DataError("Invalid DVOL timestamp")
    open_ = _finite(open_, "DVOL value", 0.0)
    high = _finite(high, "DVOL value", 0.0)
    low = _finite(low, "DVOL value", 0.0)
    close = _finite(close, "DVOL value", 0.0)
    if high < max(open_, low, close) or low > min(open_, close):
        raise DataError("Inconsistent DVOL OHLC values")
    return {"t": timestamp, "o": open_, "h": high, "l": low, "c": close}


class DVOL:
    """Bounded, cached access to the official Deribit volatility index."""

    def __init__(self, client=None, assets=None, quote_provider=None,
                 clock=None, cache_seconds=CACHE_SECONDS, max_pages=MAX_PAGES):
        self._client = client if client is not None else DeribitClient()
        self._assets = assets or {}
        self._quote_provider = quote_provider
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._cache_seconds = cache_seconds
        self._max_pages = max_pages
        self._lock = threading.RLock()
        self._cache = {}

    def currency_for(self, asset_name):
        name = str(asset_name).strip().upper()
        return name if name in SUPPORTED_CURRENCIES else None

    def fetch_series(self, currency, resolution, period_seconds, now_ms=None):
        """Read the complete requested range, following backward continuations."""
        now = self._clock() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise DataError("Invalid DVOL request time")
        if currency not in SUPPORTED_CURRENCIES:
            raise DataError(f"{currency} has no Deribit volatility index")
        if resolution not in RESOLUTIONS:
            raise DataError("Unsupported DVOL resolution")
        period_seconds = _finite(period_seconds, "DVOL period", 1)
        bucket_ms = RESOLUTIONS[resolution] * 1000
        start_ms = max(0, now - int(period_seconds * 1000))
        first_bucket = start_ms // bucket_ms * bucket_ms
        cursor = now
        seen = {}
        complete = False
        for _ in range(self._max_pages):
            result = self._client.request("public/get_volatility_index_data", {
                "currency": currency, "start_timestamp": start_ms,
                "end_timestamp": cursor, "resolution": resolution})
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise DataError("Deribit returned an invalid volatility index response")
            data = result["data"]
            for raw in data:
                point = parse_row(raw)
                stamp = point["t"]
                if stamp % bucket_ms or not first_bucket <= stamp <= cursor:
                    raise DataError("Deribit returned a misaligned or out-of-range DVOL timestamp")
                if stamp in seen and seen[stamp] != point:
                    raise DataError("Deribit returned conflicting DVOL points")
                seen[stamp] = point
            continuation = result.get("continuation")
            if continuation is None:
                complete = True
                break
            if (isinstance(continuation, bool) or not isinstance(continuation, int)
                    or continuation < 0 or continuation >= cursor or not data):
                raise DataError("Deribit returned an invalid DVOL continuation")
            if continuation < start_ms:
                complete = True
                break
            cursor = continuation
        if not complete:
            raise DataError("Deribit DVOL history exceeded the page limit; the range is incomplete")
        rows = sorted(seen.values(), key=lambda point: point["t"])
        if rows and (rows[0]["t"] != first_bucket or any(
                current["t"] - previous["t"] != bucket_ms
                for previous, current in zip(rows, rows[1:]))):
            raise DataError("Deribit DVOL history contains missing intervals")
        return rows

    def _reference_price(self, asset_name, symbol, now, quote=_UNSET):
        if quote is _UNSET:
            try:
                quote = self._quote_provider(asset_name) if self._quote_provider else None
            except Exception:
                quote = None
        if (not isinstance(quote, dict) or not symbol or quote.get("symbol") != symbol
                or quote.get("source") != "Deribit composite index"
                or quote.get("quote_kind") != "index_reference"):
            return None
        current = fresh_quote(quote, now)
        return (current["bid"] / 2 + current["ask"] / 2) if current else None

    def _result(self, asset_name, currency, period, rows, fetched_ms, now,
                fresh, stale, error, asset_config=None, quote=_UNSET):
        latest = rows[-1] if rows else None
        resolution_seconds = RESOLUTIONS[PERIOD_RESOLUTION[period]]
        forming = bool(latest and latest["t"] + resolution_seconds * 1000 > now)
        symbol = (asset_config if asset_config is not None else self._assets.get(asset_name, {})).get("symbol")
        current_bucket = now // (resolution_seconds * 1000) * resolution_seconds * 1000
        source_fresh = bool(latest and current_bucket - resolution_seconds * 1000 <= latest["t"] <= now)
        fresh = bool(fresh and source_fresh and fetched_ms is not None and 0 <= now - fetched_ms < self._cache_seconds * 1000)
        stale = bool(rows and not fresh)
        if rows and not source_fresh and not error:
            error = "Deribit DVOL is stale; the latest current or completed interval is missing"
        price = self._reference_price(asset_name, symbol, now, quote)
        move = {}
        if fresh and price is not None:
            move = {f"{days}d": expected_move(price, latest["c"], days) for days in MOVE_HORIZONS}
        move_error = None
        if not fresh:
            move_error = "Expected moves require fresh Deribit DVOL data."
        elif price is None:
            move_error = "Expected moves require a fresh matching Deribit index price."
        if error:
            status = "error"
        elif not rows:
            status = "empty"
        else:
            status = "ok"
        return {
            "asset": asset_name,
            "symbol": symbol,
            "currency": currency,
            "supported": True,
            "status": status,
            "period": period,
            "resolution": PERIOD_RESOLUTION[period],
            "fresh": fresh,
            "stale": stale,
            "asof_ms": now,
            "fetched_ms": fetched_ms,
            "series": rows,
            "latest": {"t": latest["t"], "o": latest["o"], "h": latest["h"],
                       "l": latest["l"], "c": latest["c"], "forming": forming} if latest else None,
            "price": price,
            "expected_move": move,
            "expected_move_error": move_error,
            "units": "annualized volatility percentage points",
            "error": error,
        }

    def snapshot(self, asset_name, period=DEFAULT_PERIOD, now_ms=None, *, asset_config=None, quote=_UNSET):
        """Build a display/API payload without raising for expected failures."""
        now = self._clock() if now_ms is None else now_ms
        period = str(period)
        if period not in PERIODS:
            raise DataError("Unsupported DVOL period")
        config = dict(asset_config if asset_config is not None else self._assets.get(asset_name, {}))
        currency = self.currency_for(asset_name)
        if currency is None:
            return {"asset": asset_name,
                    "symbol": config.get("symbol"),
                    "currency": None, "supported": False, "status": "unsupported",
                    "period": period, "resolution": PERIOD_RESOLUTION[period],
                    "asof_ms": now, "fetched_ms": None, "series": [], "latest": None,
                    "price": None, "expected_move": {}, "fresh": False, "stale": False,
                    "units": "annualized volatility percentage points",
                    "error": f"Deribit does not publish a {str(asset_name).upper()} volatility index"}
        key = (currency, PERIOD_RESOLUTION[period], period)
        with self._lock:
            cached = self._cache.get(key)
        if cached and 0 <= (now - cached["fetched_ms"]) / 1000.0 < self._cache_seconds:
            return self._result(asset_name, currency, period, cached["rows"],
                                cached["fetched_ms"], now, fresh=True, stale=False, error=None, asset_config=config, quote=quote)
        try:
            rows = self.fetch_series(currency, PERIOD_RESOLUTION[period], PERIODS[period], now)
            fetched_ms = self._clock()
            # Cache age is wall-clock based. A backwards jump cannot make it fresh.
            result_now = max(now, fetched_ms)
            with self._lock:
                self._cache[key] = {"fetched_ms": fetched_ms, "rows": rows}
            return self._result(asset_name, currency, period, rows, fetched_ms, result_now,
                                fresh=True, stale=False, error=None, asset_config=config, quote=quote)
        except DataError as exc:
            with self._lock:
                cached = self._cache.get(key)
            if cached:
                return self._result(asset_name, currency, period, cached["rows"],
                                    cached["fetched_ms"], now, fresh=False, stale=True, error=str(exc), asset_config=config, quote=quote)
            return self._result(asset_name, currency, period, [], None, now,
                                fresh=False, stale=False, error=str(exc), asset_config=config, quote=quote)


def register_dvol_routes(app, runtime, render_dashboard, mutation_payload):
    """Independent Deribit-only DVOL pairs, page, API and Settings editor."""
    from flask import jsonify, request
    from .core import normalise_pair, safe_error
    from .deribit_market_data import DeribitMarketData
    from .dvol_settings import DVOLSettings

    provider = getattr(getattr(runtime, "provider", None), "deribit", None)
    if not isinstance(provider, DeribitMarketData):
        provider = DeribitMarketData(client=runtime.deribit)
    settings_base = getattr(runtime, "settings_path", None) or runtime.store.path
    from pathlib import Path
    settings = DVOLSettings(Path(settings_base).parent / "dvol-settings.json",
                            runtime.deribit, lambda pair: pair in provider.available_pairs())
    dvol = DVOL(client=runtime.deribit)
    app.extensions["pypta_dvol_settings"] = settings

    def no_store(payload, status=200):
        response = jsonify(payload)
        response.headers["Cache-Control"] = "no-store"
        return response, status

    @app.get("/dvol")
    def dvol_page():
        pairs, revision = settings.enabled_pairs()
        return render_dashboard("dvol.html", dvol_pairs=pairs, dvol_revision=revision)

    @app.get("/api/settings/dvol")
    def dvol_settings_data():
        try:
            document, revision = settings.read()
            try:
                available, error = settings.available_pairs(), None
            except DataError as exc:
                available, error = [], safe_error(exc)
            return no_store({"settings": document, "revision": revision,
                             "available_pairs": available, "availability_error": error})
        except (DataError, OSError) as exc:
            return no_store({"error": safe_error(exc)}, 400)

    @app.post("/api/settings/dvol")
    def save_dvol_settings():
        try:
            body = mutation_payload({"settings", "revision"})
            if runtime.stop.is_set():
                raise DataError("The dashboard is restarting. Reload before saving DVOL settings.")
            result = settings.save(body["settings"], body["revision"])
            return no_store(result)
        except (DataError, ValueError, TypeError, OSError) as exc:
            return no_store({"error": safe_error(exc)}, 400)

    @app.get("/api/dvol")
    def dvol_data():
        try:
            pair = normalise_pair(request.args.get("pair", ""))
            pairs, revision = settings.enabled_pairs()
            if pair not in pairs:
                return no_store({"error": "Choose an enabled pair from DVOL settings."}, 404)
            period = request.args.get("period", DEFAULT_PERIOD)
            if period not in PERIODS:
                return no_store({"error": "Unsupported period"}, 400)
            reference_error = None
            try:
                quote = settings.quote(pair)
            except DataError as exc:
                quote, reference_error = None, safe_error(exc)
            payload = dvol.snapshot(pair.split("/")[0], period,
                                    asset_config={"symbol": pair}, quote=quote)
            _, current_revision = settings.read()
            if current_revision != revision:
                return no_store({"error": "DVOL pair settings changed; refresh DVOL."}, 409)
            payload["settings_revision"] = revision
            if reference_error:
                payload["expected_move_error"] = reference_error
            return no_store(payload)
        except (DataError, OSError) as exc:
            return no_store({"error": safe_error(exc)}, 400)
