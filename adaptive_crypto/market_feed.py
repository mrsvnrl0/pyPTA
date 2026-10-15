"""Settings market routing, separate from the trained neural input feed."""
from __future__ import annotations
import threading
import time
from .core import DataError, normalise_pair
from .deribit_market_data import DeribitMarketData
from .binance_spot_data import BinanceSpotMarketData

PREFERENCES = {"deribit_first", "binance"}


def validate_preference(preference):
    if preference not in PREFERENCES:
        raise DataError("Market source must be Deribit first or Binance Spot")
    return preference


class DashboardMarketData:
    """Fallback is only for a catalog-confirmed missing market, never a data failure."""
    candle_source = {"venue": "Settings market source", "completed_only": True, "volume": "base_asset"}

    def __init__(self, client=None, preference="deribit_first", *, deribit=None, binance=None, wall_clock=time.time):
        self.deribit = deribit if deribit is not None else DeribitMarketData(client)
        self.binance = binance if binance is not None else BinanceSpotMarketData()
        self.lock = threading.RLock()
        self.wall_clock = wall_clock
        self.preference = validate_preference(preference)

    def set_preference(self, preference):
        with self.lock:
            self.preference = validate_preference(preference)

    def _selection(self, pair, preference=None):
        pair = normalise_pair(pair)
        preference = validate_preference(self.preference if preference is None else preference)
        if preference == "deribit_first" and self.deribit.supports_pair(pair):
            self.deribit.validate_pair(pair)
            return pair, self.deribit, preference
        # Catalog errors are not interpreted as missing markets. Once selected,
        # a Deribit outage or missing candles cannot switch the pair's venue.
        self.binance.validate_pair(pair)
        return pair, self.binance, preference

    def validate_pair(self, pair, preference=None):
        return self._selection(pair, preference)[0]

    def available_pairs(self, preference=None):
        preference = validate_preference(self.preference if preference is None else preference)
        pairs = set(self.binance.available_pairs())
        if preference == "deribit_first":
            pairs = {pair for pair in pairs if not self.deribit.supports_pair(pair)}
            pairs.update(self.deribit.available_pairs())
        return sorted(pairs)

    def now(self):
        # Providers verify their own exchange clock against this UTC observation
        # before serving completed bars; Binance pairs have no Deribit clock dependency.
        return int(self.wall_clock() * 1000)

    def candles(self, pair, interval_ms, now):
        with self.lock:
            pair, provider, _ = self._selection(pair)
            return provider.candles(pair, interval_ms, now)

    chart_candles = candles

    def source_metadata(self, pair, interval_ms, now=None):
        with self.lock:
            pair, provider, preference = self._selection(pair)
            return {**provider.source_metadata(pair, interval_ms, now), "preference": preference}

    def quotes(self, pairs):
        with self.lock:
            groups = {}
            for raw_pair in pairs:
                pair, provider, _ = self._selection(raw_pair)
                groups.setdefault(provider, []).append((raw_pair, pair))
            result = {}
            for provider, items in groups.items():
                quotes = provider.quotes([pair for _, pair in items])
                for raw_pair, pair in items:
                    # Providers validate their exchange clock and request freshness.
                    # Timestamp this receipt on the same observation clock as SMC;
                    # small valid exchange offsets must not future-date a price.
                    quote = dict(quotes[pair])
                    quote["provider_observed_ms"] = quote["asof_ms"]
                    quote["asof_ms"] = self.now()
                    result[raw_pair] = quote
            return result

    def close(self):
        with self.lock:
            self.deribit.close()
            self.binance.close()
