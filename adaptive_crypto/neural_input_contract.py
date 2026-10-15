"""Live input provenance for the published Parente 5/2 neural model.

Its frozen scaler expects Binance spot/USDT prices and base-asset volume on
completed 4H candles, independently of the dashboard's selected SMC pair.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass

from .core import DataError, H4, normalise_pair


@dataclass(frozen=True)
class CandleInputContract:
    venue: str
    quote_asset: str
    interval_ms: int
    market: str = "spot"
    volume: str = "base_asset"

    def metadata(self):
        return asdict(self)

    def validate(self, source, asset):
        if source is None:
            raise DataError("NN candle input provenance is unavailable; the actual venue and pair are required")
        expected = (self.venue.casefold(), self.market, asset + "/" + self.quote_asset,
                    self.interval_ms, self.volume)
        actual = (source["venue"].casefold(), source["market"], source["pair"],
                  source["interval_ms"], source["volume"])
        if actual != expected:
            raise DataError(
                f"NN input differs from its trained candle contract: trained {self.venue} "
                f"{asset}/{self.quote_asset} {self.interval_ms // 60000}M {self.market} "
                f"{self.volume}; received {source['venue']} {source['pair']} "
                f"{source['interval_ms'] // 60000}M {source['market']} {source['volume']}. "
                "Parente 5/2 requires its original Binance spot/USDT inputs.")


PARENTE_BINANCE_USDT = CandleInputContract("Binance", "USDT", H4)


def normalise_input_source(source, *, interval_ms, pair=None):
    """Validate explicit provenance without guessing a venue from a model ID.

    The main candle provider supplies its venue/market/volume declaration, while
    the runtime knows its requested pair. A separate feature provider must return
    its own pair or symbol. Actual candle interval/completion is checked by the
    engine before this metadata is exposed with a signal.
    """
    if not isinstance(source, dict):
        raise DataError("NN candle input source must declare its venue, market, pair and volume units")
    result = copy.deepcopy(source)
    for key in ("venue", "market", "volume"):
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise DataError("NN candle input source is missing " + key)
        result[key] = result[key].strip()
    result["market"] = result["market"].casefold()
    declared_pairs = [normalise_pair(result[key]) for key in ("pair", "symbol") if key in result]
    if pair is not None:
        declared_pairs.append(normalise_pair(pair))
    if not declared_pairs or len(set(declared_pairs)) != 1:
        raise DataError("NN candle input pair is missing or differs from the actual requested pair")
    result["pair"] = declared_pairs[0]
    quote = result["pair"].split("/")[1]
    if result.get("quote_asset", quote) != quote:
        raise DataError("NN candle input quote differs from its declared pair")
    declared_interval = result.get("interval_ms", interval_ms)
    if type(declared_interval) is not int or declared_interval != interval_ms:
        raise DataError("NN candle input interval differs from the requested decision interval")
    if result.get("completed_only", True) is not True:
        raise DataError("NN candle input must contain completed candles only")
    result.update(quote_asset=quote, interval_ms=interval_ms, completed_only=True)
    result.setdefault("symbol", result["pair"])
    return result


def input_cache_identity(source, *, pair, interval_ms):
    """Include the real pair even for deliberately injected test candles."""
    if source is None:
        return ("injected-candles", normalise_pair(pair), interval_ms)
    return (source["venue"].casefold(), source["market"], source["pair"],
            source["interval_ms"], source["volume"], source.get("source_key"))
