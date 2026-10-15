"""Catalog-verified Deribit perpetual/spot OHLCV and matching price references."""
from __future__ import annotations
import re
import threading
import time
from .core import Candle, DataError, H4, finite, normalise_pair, validate_candles
from .deribit import DeribitClient

HOUR = 3600000
RESOLUTIONS = {60000: "1", 300000: "5", 900000: "15", 1800000: "30", HOUR: "60"}


def parse_candles(result, interval, start, end):
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise DataError("Deribit candle history is unavailable")
    keys = ("ticks", "open", "high", "low", "close", "volume")
    columns = [result.get(key) for key in keys]
    if any(not isinstance(col, list) for col in columns) or len({len(col) for col in columns}) != 1:
        raise DataError("Invalid Deribit candle columns")
    bars = {}
    for tick, o, h, low, c, v in zip(*columns):
        stamp = finite(tick, "candle time", 0)
        if stamp != int(stamp) or int(stamp) % interval:
            raise DataError("Misaligned Deribit candle")
        stamp = int(stamp)
        if not start <= stamp < end or stamp+interval > end:
            continue
        values = [finite(value, label, 0 if label == "volume" else 1e-15)
                  for value, label in zip((o,h,low,c,v), ("open","high","low","close","volume"))]
        bar = Candle(stamp, *values, interval)
        if not bar.l <= min(bar.o, bar.c) <= max(bar.o, bar.c) <= bar.h:
            raise DataError("Invalid Deribit candle price range")
        if stamp in bars and bars[stamp] != bar:
            raise DataError("Conflicting Deribit candles")
        bars[stamp] = bar
    return [bars[key] for key in sorted(bars)]


def aggregate_four_hours(bars):
    groups = {}
    for bar in bars:
        groups.setdefault(bar.t//H4*H4, []).append(bar)
    result = []
    for start, group in sorted(groups.items()):
        if len(group) != 4 or [b.t for b in group] != [start+i*HOUR for i in range(4)]:
            raise DataError("Incomplete hourly candles for a Deribit 4H bar")
        result.append(Candle(start, group[0].o, max(b.h for b in group), min(b.l for b in group),
                             group[-1].c, sum(b.v for b in group), H4))
    return result


class DeribitMarketData:
    candle_source = {"venue": "Deribit", "market": "perpetual", "volume": "base_asset", "completed_only": True}

    def __init__(self, client=None, *, monotonic=time.monotonic):
        self.client = client or DeribitClient()
        self.monotonic = monotonic
        self.lock = threading.RLock()
        self.clock_sample = None
        self.cache = {}
        self._catalog = None
        self._indices = None

    def now(self):
        with self.lock:
            tick = self.monotonic()
            if self.clock_sample and 0 <= tick-self.clock_sample[0] < 15:
                return self.clock_sample[1]+int((tick-self.clock_sample[0])*1000)
            server = self.client.request("public/get_time", {})
            stamp = finite(server, "Deribit server time", 1)
            elapsed = self.monotonic()-tick
            if stamp != int(stamp) or elapsed > 5:
                raise DataError("Deribit clock synchronization is unavailable")
            self.clock_sample = (self.monotonic(), int(stamp))
            return int(stamp)

    def _markets(self):
        """Catalog existence establishes support; a halted book cannot trigger fallback."""
        with self.lock:
            tick = self.monotonic()
            if self._catalog is not None and 0 <= tick-self._catalog[0] < 300:
                return self._catalog[1]
            markets = {}
            for kind in ("future", "spot"):
                rows = self.client.request("public/get_instruments", {
                    "currency": "any", "kind": kind, "expired": False})
                if not isinstance(rows, list):
                    raise DataError("Deribit market catalog is unavailable")
                for row in rows:
                    if not isinstance(row, dict):
                        raise DataError("Invalid Deribit market catalog")
                    if row.get("kind") != kind or row.get("underlying_type", "crypto") != "crypto":
                        continue
                    if kind == "future" and row.get("settlement_period") != "perpetual":
                        continue
                    base, quote = row.get("base_currency"), row.get("quote_currency")
                    instrument = row.get("instrument_name")
                    if (not isinstance(base, str) or not re.fullmatch(r"[A-Z0-9]{1,30}", base)
                            or not isinstance(quote, str) or not re.fullmatch(r"[A-Z0-9]{1,30}", quote)
                            or not isinstance(instrument, str) or not re.fullmatch(r"[A-Z0-9_-]{1,80}", instrument)):
                        raise DataError("Invalid Deribit market catalog fields")
                    pair = base+"/"+quote
                    market = {"instrument": instrument, "market": "perpetual" if kind == "future" else "spot",
                              "index_name": row.get("price_index"), "pair": pair,
                              "available": row.get("is_active") is True and row.get("state") == "open"}
                    # Perpetual history is the primary feed, spot is selected only
                    # when no active perpetual exists in that exact currency.
                    previous = markets.get(pair)
                    if (previous and previous["market"] == market["market"] == "perpetual"
                            and previous["instrument"] != instrument):
                        raise DataError("Ambiguous Deribit perpetual pair")
                    if (previous is None or (market["available"] and not previous["available"])
                            or (market["available"] == previous["available"] and market["market"] == "perpetual")):
                        markets[pair] = market
            self._catalog = (self.monotonic(), markets)
            return markets

    def available_pairs(self):
        return sorted(pair for pair, market in self._markets().items() if market["available"])

    def supports_pair(self, pair):
        return normalise_pair(pair) in self._markets()

    def validate_pair(self, pair):
        pair = normalise_pair(pair)
        if pair not in self._markets():
            raise DataError(pair+": no active Deribit perpetual or spot market")
        if not self._markets()[pair]["available"]:
            raise DataError(pair+": Deribit market is halted or unavailable; no other feed is substituted")
        return pair

    def _market(self, pair):
        pair = self.validate_pair(pair)
        return dict(self._markets()[pair])

    def _index_names(self):
        with self.lock:
            tick = self.monotonic()
            if self._indices is not None and 0 <= tick-self._indices[0] < 300:
                return self._indices[1]
            rows = self.client.request("public/get_index_price_names", {})
            if not isinstance(rows, list) or any(not isinstance(name, str) for name in rows):
                raise DataError("Deribit price-index catalog is unavailable")
            self._indices = (self.monotonic(), set(rows))
            return self._indices[1]

    def supports_index(self, pair):
        pair = normalise_pair(pair)
        return pair.lower().replace("/", "_") in self._index_names()

    def available_indices(self):
        """Published reference indices, including conversion-only USD currency indices."""
        return sorted(self._index_names())

    def source_metadata(self, pair, interval_ms, now=None):
        market = self._market(pair)
        pair = market["pair"]
        return {**self.candle_source, "market": market["market"], "pair": pair, "symbol": pair,
                "quote_asset": pair.split("/")[1], "instrument": market["instrument"],
                "source_key": market["instrument"], "interval_ms": interval_ms,
                "aggregation": "four completed UTC hourly bars" if interval_ms == H4 else "native"}

    def history(self, pair, interval_ms, start_ms, end_ms):
        if interval_ms != H4 and interval_ms not in RESOLUTIONS:
            raise DataError("Unsupported Deribit candle interval")
        instrument = self._market(pair)["instrument"]
        if type(start_ms) is not int or type(end_ms) is not int or not 0 <= start_ms < end_ms:
            raise DataError("Invalid Deribit history range")
        start = (start_ms+interval_ms-1)//interval_ms*interval_ms
        end = end_ms//interval_ms*interval_ms
        if end <= start:
            return []
        unit = HOUR if interval_ms == H4 else interval_ms
        if (end-start)//unit > 15000:
            raise DataError("Deribit history request exceeds the bounded window")
        result = []
        cursor = start
        while cursor < end:
            stop = min(end, cursor+unit*1000)
            raw = self.client.request("public/get_tradingview_chart_data", {
                "instrument_name": instrument, "resolution": RESOLUTIONS[unit],
                "start_timestamp": cursor, "end_timestamp": stop-1})
            chunk = parse_candles(raw, unit, cursor, stop)
            if [bar.t for bar in chunk] != list(range(cursor, stop, unit)):
                raise DataError("Deribit candle history has a gap; no other feed is substituted")
            result.extend(chunk)
            cursor = stop
        bars = aggregate_four_hours(result) if interval_ms == H4 else result
        validate_candles(bars, interval_ms, end, minimum=1, fresh=False)
        return bars

    def candles(self, pair, interval_ms, now):
        if interval_ms != H4 and interval_ms not in RESOLUTIONS:
            raise DataError("Unsupported Deribit candle interval")
        if type(now) is not int or now < 0:
            raise DataError("Observation time must be integer milliseconds")
        pair = self.validate_pair(pair)
        server = self.now()
        if abs(server-now) > 30000 or server//interval_ms != now//interval_ms:
            raise DataError("Deribit and observation clocks must agree on the candle close")
        end = now//interval_ms*interval_ms
        key = (pair, self._market(pair)["instrument"], interval_ms)
        count = 420 if interval_ms == H4 else 600
        with self.lock:
            cached = self.cache.get(key)
            if cached and cached[-1].t+interval_ms == end:
                return list(cached)
            start = end-count*interval_ms
            if cached and cached[-1].t+interval_ms < end and cached[0].t <= start:
                additions = self.history(pair, interval_ms, cached[-1].t+interval_ms, end)
                bars = (cached+additions)[-count:]
            else:
                bars = self.history(pair, interval_ms, start, end)
            validate_candles(bars, interval_ms, now, minimum=count, fresh=True)
            if bars[-1].t+interval_ms != end:
                raise DataError("Latest completed Deribit candle is missing")
            self.cache[key] = bars
            return list(bars)

    chart_candles = candles

    def quotes(self, pairs):
        result = {}
        for raw_pair in pairs:
            market = self._market(raw_pair)
            pair = market["pair"]
            started = self.monotonic()
            index = market["index_name"]
            expected = pair.lower().replace("/", "_")
            if index == expected and index in self._index_names():
                data = self.client.request("public/get_index_price", {"index_name": index})
                price = finite(data.get("index_price") if isinstance(data, dict) else None,
                               "Deribit composite index", 1e-15)
                quote = {"bid": price, "ask": price, "last": price,
                         "source": "Deribit composite index", "synthetic": True,
                         "executable": False, "quote_kind": "index_reference", "spread_observed": False}
            else:
                data = self.client.request("public/ticker", {"instrument_name": market["instrument"]})
                if not isinstance(data, dict) or data.get("instrument_name") != market["instrument"]:
                    raise DataError("Invalid Deribit ticker")
                bid = finite(data.get("best_bid_price"), "Deribit bid", 1e-15)
                ask = finite(data.get("best_ask_price"), "Deribit ask", bid)
                last = finite(data.get("last_price"), "Deribit last price", 1e-15)
                observed = finite(data.get("timestamp"), "Deribit ticker time", 1)
                if observed != int(observed) or abs(self.now()-observed) > 10000:
                    raise DataError("Deribit ticker is stale")
                quote = {"bid": bid, "ask": ask, "last": last,
                         "source": "Deribit "+market["market"]+" ticker", "synthetic": False,
                         "executable": True, "quote_kind": "order_book", "spread_observed": True}
            if self.monotonic()-started > 5:
                raise DataError("Deribit price reference request is stale")
            quote.update(asof_ms=self.now(), symbol=pair)
            result[raw_pair] = quote
        return result

    def close(self):
        with self.lock:
            self.cache.clear()
            self._catalog = self._indices = None
