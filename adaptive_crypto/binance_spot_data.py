"""Exact-pair Binance Spot fallback data, independent of the Parente input client."""
from __future__ import annotations
import threading
import time
import requests
from .core import Candle, DataError, H4, finite, normalise_pair, validate_candles

INTERVALS = {60000: "1m", 180000: "3m", 300000: "5m", 900000: "15m", 1800000: "30m",
             3600000: "1h", 7200000: "2h", H4: "4h", 21600000: "6h", 28800000: "8h",
             43200000: "12h", 86400000: "1d"}


def _ms(value, label):
    stamp = finite(value, label, 0)
    if stamp != int(stamp):
        raise DataError(label + " must be integer milliseconds")
    return int(stamp)


def parse_rows(rows, interval, start, end):
    if not isinstance(rows, list) or len(rows) > 1000:
        raise DataError("Invalid Binance Spot candle response")
    bars = []
    previous = None
    for row in rows:
        if not isinstance(row, list) or len(row) != 12:
            raise DataError("Malformed Binance Spot candle")
        stamp = _ms(row[0], "Binance candle time")
        closed = _ms(row[6], "Binance candle close")
        if stamp % interval or closed != stamp + interval - 1:
            raise DataError("Misaligned Binance Spot candle")
        if previous is not None and stamp <= previous:
            raise DataError("Duplicate or unordered Binance Spot candles")
        previous = stamp
        if not start <= stamp < end or stamp + interval > end:
            continue
        bar = Candle(stamp, *(finite(row[i], "Binance OHLC", 1e-15) for i in (1, 2, 3, 4)),
                     finite(row[5], "Binance base volume", 0), interval)
        if not bar.l <= min(bar.o, bar.c) <= max(bar.o, bar.c) <= bar.h:
            raise DataError("Invalid Binance Spot candle price range")
        bars.append(bar)
    return bars


class BinanceSpotMarketData:
    candle_source = {"venue": "Binance", "market": "spot", "volume": "base_asset", "completed_only": True}

    def __init__(self, session=None, *, monotonic=time.monotonic):
        self.session = session if session is not None else requests.Session()
        self.session.trust_env = False
        self.session.headers["User-Agent"] = "AdaptiveCryptoDashboard/MarketData"
        self.monotonic = monotonic
        self.lock = threading.RLock()
        self._catalog = None
        self._clock = None
        self._cache = {}

    def _get(self, endpoint, **params):
        if endpoint not in {"time", "exchangeInfo", "klines", "ticker/bookTicker"}:
            raise DataError("Unsupported Binance public market-data endpoint")
        try:
            response = self.session.get("https://api.binance.com/api/v3/" + endpoint, params=params,
                                        timeout=(4, 8), allow_redirects=False)
            if response.status_code != 200:
                raise DataError("Binance Spot market data is unavailable")
            return response.json()
        except Exception:
            raise DataError("Binance Spot market data is unavailable") from None

    def now(self):
        with self.lock:
            tick = self.monotonic()
            if self._clock is not None and 0 <= tick - self._clock[0] < 15:
                return self._clock[1] + int((tick - self._clock[0]) * 1000)
            body = self._get("time")
            server = _ms(body.get("serverTime") if isinstance(body, dict) else None, "Binance server time")
            if self.monotonic() - tick > 5:
                raise DataError("Binance clock synchronization is unavailable")
            self._clock = (self.monotonic(), server)
            return server

    def _markets(self):
        with self.lock:
            tick = self.monotonic()
            if self._catalog is not None and 0 <= tick - self._catalog[0] < 300:
                return self._catalog[1]
            body = self._get("exchangeInfo")
            rows = body.get("symbols") if isinstance(body, dict) else None
            if not isinstance(rows, list):
                raise DataError("Binance Spot market catalog is unavailable")
            result = {}
            for row in rows:
                if not isinstance(row, dict):
                    raise DataError("Invalid Binance Spot market catalog")
                if row.get("status") != "TRADING" or row.get("isSpotTradingAllowed") is not True:
                    continue
                base, quote, symbol = row.get("baseAsset"), row.get("quoteAsset"), row.get("symbol")
                if (not isinstance(base, str) or not 1 <= len(base) <= 30 or not base.isalnum() or base.upper() != base
                        or not isinstance(quote, str) or not 1 <= len(quote) <= 30 or not quote.isalnum() or quote.upper() != quote
                        or symbol != base + quote):
                    raise DataError("Invalid Binance Spot market catalog fields")
                pair = base + "/" + quote
                if pair in result:
                    raise DataError("Ambiguous Binance Spot pair")
                result[pair] = symbol
            self._catalog = (self.monotonic(), result)
            return result

    def available_pairs(self):
        return sorted(self._markets())

    def supports_pair(self, pair):
        return normalise_pair(pair) in self._markets()

    def validate_pair(self, pair):
        pair = normalise_pair(pair)
        if pair not in self._markets():
            raise DataError(pair + ": no trading Binance Spot market")
        return pair

    def source_metadata(self, pair, interval_ms, now=None):
        pair = self.validate_pair(pair)
        symbol = self._markets()[pair]
        return {**self.candle_source, "pair": pair, "symbol": pair, "quote_asset": pair.split("/")[1],
                "instrument": symbol, "source_key": "binance-spot-" + symbol,
                "interval_ms": interval_ms, "aggregation": "native"}

    def history(self, pair, interval_ms, start_ms, end_ms):
        pair = self.validate_pair(pair)
        if interval_ms not in INTERVALS:
            raise DataError("Unsupported Binance Spot candle interval")
        if type(start_ms) is not int or type(end_ms) is not int or not 0 <= start_ms < end_ms:
            raise DataError("Invalid Binance Spot history range")
        start = (start_ms + interval_ms - 1) // interval_ms * interval_ms
        end = end_ms // interval_ms * interval_ms
        if end <= start:
            return []
        if (end - start) // interval_ms > 15000:
            raise DataError("Binance Spot history request exceeds the bounded window")
        symbol = self._markets()[pair]
        bars = []
        cursor = start
        while cursor < end:
            stop = min(end, cursor + 1000 * interval_ms)
            rows = self._get("klines", symbol=symbol, interval=INTERVALS[interval_ms],
                             startTime=cursor, endTime=stop - 1, limit=1000)
            chunk = parse_rows(rows, interval_ms, cursor, stop)
            if [bar.t for bar in chunk] != list(range(cursor, stop, interval_ms)):
                raise DataError("Binance Spot candle history has a gap; no bars are invented")
            bars.extend(chunk)
            cursor = stop
        validate_candles(bars, interval_ms, end, minimum=1, fresh=False)
        return bars

    def candles(self, pair, interval_ms, now):
        pair = self.validate_pair(pair)
        now = _ms(now, "Observation time")
        if interval_ms not in INTERVALS:
            raise DataError("Unsupported Binance Spot candle interval")
        server = self.now()
        if abs(server - now) > 30000 or server // interval_ms != now // interval_ms:
            raise DataError("Binance and observation clocks must agree on the candle close")
        end = now // interval_ms * interval_ms
        count = 420 if interval_ms == H4 else 600
        key = (pair, interval_ms)
        with self.lock:
            cached = self._cache.get(key)
            if cached and cached[-1].t + interval_ms == end:
                return list(cached)
            start = end - count * interval_ms
            if cached and cached[-1].t + interval_ms < end and cached[0].t <= start:
                bars = (cached + self.history(pair, interval_ms, cached[-1].t + interval_ms, end))[-count:]
            else:
                bars = self.history(pair, interval_ms, start, end)
            validate_candles(bars, interval_ms, now, minimum=count, fresh=True)
            if bars[-1].t + interval_ms != end:
                raise DataError("Latest completed Binance Spot candle is missing")
            self._cache[key] = bars
            return list(bars)

    chart_candles = candles

    def quotes(self, pairs):
        result = {}
        for raw_pair in pairs:
            pair = self.validate_pair(raw_pair)
            started = self.monotonic()
            symbol = self._markets()[pair]
            body = self._get("ticker/bookTicker", symbol=symbol)
            if not isinstance(body, dict) or body.get("symbol") != symbol:
                raise DataError("Invalid Binance Spot quote")
            bid = finite(body.get("bidPrice"), "Binance Spot bid", 1e-15)
            ask = finite(body.get("askPrice"), "Binance Spot ask", bid)
            # bookTicker has no exchange timestamp; stamp the bounded HTTP observation.
            stamp = self.now()
            if self.monotonic() - started > 5:
                raise DataError("Binance Spot quote request is stale")
            result[raw_pair] = {"bid": bid, "ask": ask, "last": (bid + ask) / 2, "asof_ms": stamp,
                "symbol": pair, "source": "Binance Spot book ticker", "quote_kind": "order_book",
                "synthetic": False, "executable": True, "spread_observed": True}
        return result

    def close(self):
        with self.lock:
            self._catalog = self._clock = None
            self._cache.clear()
            self.session.close()
