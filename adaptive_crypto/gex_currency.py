"""Match native GEX coordinates and validate captured currency-reference conversions."""
import copy
import math
from .core import DataError, finite, normalise_pair
from .currency_conversion import validate_fx


def price_currency(profile):
    return profile.get("price_currency")


def require_market_currency(profile, symbol, now):
    base, quote = normalise_pair(symbol).split("/")
    if (not isinstance(profile, dict) or profile.get("base") != base
            or price_currency(profile) != quote or profile.get("exposure_currency") != quote
            or profile.get("quote_currency", quote) != quote
            or (profile.get("symbol") is not None and normalise_pair(profile["symbol"]) != base+"/"+quote)):
        raise DataError("GEX asset, price and exposure currency must match the selected market pair")
    deadline = profile.get("fx_valid_until_ms")
    if deadline is not None and (type(deadline) is not int or deadline <= now):
        raise DataError("Deribit options currency conversion has expired")
    has_values = profile.get("spot") is not None or bool(profile.get("strikes")) or bool(profile.get("curve"))
    if quote not in {"USD", "USDC"} and has_values:
        fx = validate_fx(profile.get("coordinate_fx"), quote, now, "USD")
        midpoint = (fx["buy_rate"] + fx["sell_rate"]) / 2
        factor = finite(profile.get("coordinate_factor"), "GEX coordinate factor", 1e-300)
        if (profile.get("canonical_quote_currency") != "USD"
                or profile.get("quote_currency") != quote
                or profile.get("coordinate_kind") != "synthetic_currency_reference"
                or not math.isclose(factor, 1 / midpoint, rel_tol=1e-12)
                or type(deadline) is not int or deadline > fx["expires_ms"]):
            raise DataError("GEX native coordinate requires its captured USD currency-reference trace")
    return deadline


def convert_usd_profile(profile, quote_currency, fx, now):
    """Translate price and quote-delta exposure by the same observed USD-to-quote factor."""
    base, quote_currency = normalise_pair(str(profile.get("base"))+"/"+quote_currency).split("/")
    require_market_currency(profile, base+"/USD", now)
    fx = validate_fx(fx, quote_currency, now, "USD")
    factor = 1 / ((fx["buy_rate"] + fx["sell_rate"]) / 2)
    result = copy.deepcopy(profile)

    def scale(value):
        return finite(value, "GEX coordinate value") * factor

    def strike_row(row):
        if row is None:
            return None
        if not isinstance(row, dict):
            raise DataError("Invalid GEX strike coordinate")
        return {key: scale(value) if key in {"strike", "call_gex", "put_gex", "net_gex"} else value
                for key, value in row.items()}

    for key in ("spot", "net_gex", "gross_gex", "gamma_flip"):
        if result.get(key) is not None:
            result[key] = scale(result[key])
    for key in ("flip_candidates", "flip_range"):
        if key in result:
            result[key] = [scale(value) for value in result[key]]
    result["strikes"] = [strike_row(row) for row in result.get("strikes", [])]
    for key in ("call_wall", "put_wall"):
        if key in result:
            result[key] = strike_row(result[key])
    result["curve"] = [{key: scale(value) if key in {"price", "gex"} else value for key, value in row.items()}
                       for row in result.get("curve", [])]
    for source in result.get("sources", []):
        source["quote_currency"] = quote_currency
        for key in ("net_gex", "conversion_factor"):
            if source.get(key) is not None:
                source[key] = scale(source[key])
    deadline = min(profile.get("fx_valid_until_ms", fx["expires_ms"]), fx["expires_ms"])
    result.update(symbol=base+"/"+quote_currency, price_currency=quote_currency,
                  exposure_currency=quote_currency, quote_currency=quote_currency,
                  units=quote_currency+" delta per 1% index move",
                  canonical_quote_currency="USD", coordinate_fx=fx, coordinate_factor=factor,
                  coordinate_kind="synthetic_currency_reference", fx_valid_until_ms=deadline,
                  fx_asof_ms=fx["asof_ms"], calculated_ms=now,
                  reference_index="Deribit "+base+"/USD translated to "+quote_currency+" with observed currency midpoint")
    require_market_currency(result, result["symbol"], now)
    return result


def profile_for_market(profile, symbol, now):
    require_market_currency(profile, symbol, now)
    result = copy.deepcopy(profile)
    result["symbol"] = normalise_pair(symbol)
    return result
