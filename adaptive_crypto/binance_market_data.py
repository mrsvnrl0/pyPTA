"""Read-only Binance Spot/USDT inputs for the published Parente model.

Paper reference quotes come from the dashboard's independently selected market pair. No account, signed endpoint, futures feed,
proxy credentials, or alternative-market fallback is used by this client.
"""
from __future__ import annotations

import copy
import re
import threading
import time

import requests

from .core import Candle, DataError, H4, finite, validate_candles


SOURCE_KEY = "binance-spot-usdt-4h-v1"


def _milliseconds(value, label):
    number = finite(value, label, 0)
    if not number.is_integer():
        raise DataError(f"{label} must be integer milliseconds")
    return int(number)


def parse_binance_rows(rows, now):
    """Validate Spot kline milliseconds and use field 5 (base volume)."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= 721:
        raise DataError("Binance Spot returned malformed 4H history")
    candles = []
    previous = None
    for row in rows:
        if not isinstance(row, list) or len(row) != 12:
            raise DataError("Malformed Binance Spot kline")
        stamp = _milliseconds(row[0], "Binance open time")
        close = _milliseconds(row[6], "Binance close time")
        if stamp % H4 or close != stamp + H4 - 1:
            raise DataError("Invalid Binance 4H candle boundaries")
        if previous is not None and stamp != previous + H4:
            raise DataError("Duplicate, unordered, or missing Binance candles")
        previous = stamp
        bar = Candle(stamp, *(finite(row[i], "Binance OHLC", 1e-15) for i in (1, 2, 3, 4)),
                     finite(row[5], "Binance base volume", 0), H4)
        if stamp > now // H4 * H4:
            raise DataError("Future Binance candle")
        if close < now:
            candles.append(bar)
    candles = candles[-720:]
    validate_candles(candles, H4, now, minimum=100)
    if candles[-1].t != (now // H4 - 1) * H4:
        raise DataError("Latest completed Binance Spot 4H candle is missing")
    return candles


class BinanceParenteFeed:
    source_key = SOURCE_KEY

    def __init__(self, session=None):
        self.session = requests.Session() if session is None else session
        # Public requests must never inherit proxy/.netrc credentials.
        self.session.trust_env = False
        self.session.headers["User-Agent"] = "AdaptiveCryptoDashboard/Parente"
        self._cache = {}
        self._symbols = {}
        self._clock = None
        self._lock = threading.Lock()

    def close(self):
        self.session.close()

    def _get(self, endpoint, **params):
        if endpoint not in {"time", "exchangeInfo", "klines"}:
            raise DataError("Unsupported Binance public endpoint")
        try:
            response = self.session.get("https://api.binance.com/api/v3/" + endpoint,
                                        params=params, timeout=(4, 8), allow_redirects=False)
            if response.status_code != 200:
                raise DataError("Binance Spot public data unavailable")
            return response.json()
        except Exception:
            # Never surface response bodies, URLs, headers, or network secrets.
            raise DataError("Binance Spot public data unavailable") from None

    def _server_time(self, now):
        tick = time.monotonic()
        if self._clock is None or tick - self._clock[1] >= 60:
            before = time.monotonic()
            body = self._get("time")
            after = time.monotonic()
            if not isinstance(body, dict) or "serverTime" not in body:
                raise DataError("Malformed Binance server time")
            server = _milliseconds(body["serverTime"], "Binance server time")
            self._clock = (server, (before + after) / 2)
        server = self._clock[0] + int((time.monotonic() - self._clock[1]) * 1000)
        if abs(server - now) > 30000:
            raise DataError("Binance server clock differs from observation time")
        # A boundary disagreement must not classify a still-open Binance bar.
        if server // H4 != now // H4:
            raise DataError("Waiting for Binance and observation clocks to agree on the 4H close")
        return server

    def _symbol(self, asset):
        if not isinstance(asset, str) or not re.fullmatch(r"[A-Z0-9]{2,20}", asset):
            raise DataError("Invalid Binance base asset")
        symbol = asset + "USDT"
        cached = self._symbols.get(symbol)
        if cached is not None and time.monotonic() - cached < 3600:
            return symbol
        body = self._get("exchangeInfo", symbol=symbol)
        rows = body.get("symbols") if isinstance(body, dict) else None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise DataError("Binance Spot symbol is unavailable")
        row = rows[0]
        if (row.get("symbol") != symbol or row.get("baseAsset") != asset or row.get("quoteAsset") != "USDT"
                or row.get("status") != "TRADING" or row.get("isSpotTradingAllowed") is not True):
            raise DataError("A trading Binance Spot USDT pair is required")
        self._symbols[symbol] = time.monotonic()
        return symbol

    def candles(self, asset, now):
        now = _milliseconds(now, "Observation time")
        with self._lock:
            self._server_time(now)
            symbol = self._symbol(asset)
            expected = (now // H4 - 1) * H4
            cached = self._cache.get(symbol)
            if cached is None or cached[-1].t != expected:
                rows = self._get("klines", symbol=symbol, interval="4h", limit=720,
                                 endTime=expected + H4 - 1)
                cached = parse_binance_rows(rows, now)
                self._cache[symbol] = cached
            # Return copies: consumers cannot contaminate the shared cache.
            provenance = {"source_key": self.source_key, "venue": "Binance", "market": "spot",
                          "symbol": symbol, "quote_asset": "USDT", "interval_ms": H4,
                          "volume": "base_asset", "completed_only": True, "asof_ms": now,
                          "latest_open_ms": cached[-1].t, "latest_close_ms": cached[-1].end,
                          "candle_count": len(cached)}
            return list(cached), copy.deepcopy(provenance)
