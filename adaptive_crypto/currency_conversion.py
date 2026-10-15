"""Captured market conversions for one USD paper account; native trade prices stay native."""
from __future__ import annotations

import copy
import math
import threading
import time

from .core import DataError, finite, normalise_pair

CACHE_MS = 20000
MAX_RATE_AGE_MS = 30000
MAX_SOURCE_SKEW_MS = 5000


def _currency(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 30 or not value.isalnum():
        raise DataError("Invalid conversion currency")
    return value.upper()


def _stamp(value):
    if type(value) is not int or value < 0:
        raise DataError("Invalid conversion timestamp")
    return value


def _rate(value):
    value = finite(value, "conversion rate", 1e-300)
    if value <= 0:
        raise DataError("Conversion rate must be positive")
    return value


def identity_fx(quote_currency, now, account_currency="USD"):
    quote_currency, account_currency = _currency(quote_currency), _currency(account_currency)
    if quote_currency != account_currency:
        raise DataError("Identity conversion requires identical currencies")
    now = _stamp(now)
    return {"quote_currency": quote_currency, "account_currency": account_currency,
            "buy_rate": 1., "sell_rate": 1., "asof_ms": now,
            "expires_ms": now + CACHE_MS, "sources": []}


def validate_fx(fx, quote_currency, now, account_currency="USD"):
    """Validate a captured conversion without IO, including its currency path and age."""
    quote_currency, account_currency = _currency(quote_currency), _currency(account_currency)
    now = _stamp(now)
    if not isinstance(fx, dict) or fx.get("quote_currency") != quote_currency or fx.get("account_currency") != account_currency:
        raise DataError("Currency conversion does not match the native pair and paper account")
    buy, sell = _rate(fx.get("buy_rate")), _rate(fx.get("sell_rate"))
    asof, expires = _stamp(fx.get("asof_ms")), _stamp(fx.get("expires_ms"))
    if sell > buy or not asof <= now < expires or expires > asof + MAX_RATE_AGE_MS or now - asof > MAX_RATE_AGE_MS:
        raise DataError("Currency conversion is stale, future-dated or has invalid rates")
    sources = fx.get("sources")
    if not isinstance(sources, list):
        raise DataError("Currency conversion source trace is required")
    if quote_currency == account_currency:
        if buy != 1. or sell != 1. or sources:
            raise DataError("Identity conversion must have unit rates and no market path")
        return copy.deepcopy(fx)
    if not 1 <= len(sources) <= 3:
        raise DataError("Currency conversion requires one to three actual market sources")
    current = quote_currency
    product_buy = product_sell = 1.
    stamps, deadlines = [], []
    for source in sources:
        if not isinstance(source, dict) or source.get("from_currency") != current:
            raise DataError("Currency conversion path is inconsistent")
        target = _currency(source.get("to_currency"))
        pair = normalise_pair(source.get("pair"))
        expected = current + "/" + target if source.get("direction") == "direct" else target + "/" + current
        if source.get("direction") not in {"direct", "inverse"} or pair != expected:
            raise DataError("Currency conversion pair direction is inconsistent")
        if source.get("venue") not in {"Deribit", "Binance"}:
            raise DataError("Currency conversion must identify a supported market source")
        source_asof = _stamp(source.get("asof_ms"))
        source_expires = _stamp(source.get("expires_ms"))
        if (not source_asof <= asof <= now < source_expires or now - source_asof > MAX_RATE_AGE_MS
                or source_expires > source_asof + MAX_RATE_AGE_MS):
            raise DataError("Currency conversion contains a stale or future-dated source")
        edge_buy, edge_sell = _rate(source.get("buy_rate")), _rate(source.get("sell_rate"))
        if edge_sell > edge_buy:
            raise DataError("Currency conversion source rates are inverted")
        bid, ask = _rate(source.get("bid")), _rate(source.get("ask"))
        if ask < bid:
            raise DataError("Currency conversion source spread is inverted")
        expected_buy, expected_sell = (ask, bid) if source["direction"] == "direct" else (1. / bid, 1. / ask)
        if not math.isclose(edge_buy, expected_buy, rel_tol=1e-12) or not math.isclose(edge_sell, expected_sell, rel_tol=1e-12):
            raise DataError("Currency conversion source rates do not match its market")
        product_buy *= edge_buy
        product_sell *= edge_sell
        stamps.append(source_asof)
        deadlines.append(source_expires)
        current = target
    if (current != account_currency or asof != max(stamps) or expires != min(deadlines)
            or max(stamps) - min(stamps) > MAX_SOURCE_SKEW_MS
            or not math.isclose(buy, product_buy, rel_tol=1e-12)
            or not math.isclose(sell, product_sell, rel_tol=1e-12)):
        raise DataError("Currency conversion snapshot does not match its captured market path")
    return copy.deepcopy(fx)


class CurrencyConversions:
    """Index/spot conversions with bounded paths and no stablecoin par assumptions."""

    def __init__(self, provider, *, monotonic=time.monotonic):
        self.provider = provider
        self.monotonic = monotonic
        self.lock = threading.RLock()
        self.cache = {}

    def quote(self, quote_currency, account_currency="USD", now=None, *, refresh=False):
        if type(refresh) is not bool:
            raise DataError("Conversion refresh must be a boolean")
        quote_currency, account_currency = _currency(quote_currency), _currency(account_currency)
        now = _stamp(self.provider.now() if now is None else now)
        if quote_currency == account_currency:
            return identity_fx(quote_currency, now, account_currency)
        preference = getattr(self.provider, "preference", "deribit_first")
        if preference not in {"deribit_first", "binance"}:
            raise DataError("Invalid currency conversion source preference")
        key = (quote_currency, account_currency, preference)
        with self.lock:
            cached = self.cache.get(key)
            if cached is not None and not refresh:
                try:
                    return validate_fx(cached, quote_currency, now, account_currency)
                except DataError:
                    self.cache.pop(key, None)
            try:
                path = self._path(quote_currency, account_currency, preference)
                sources = [self._capture(edge, account_currency) for edge in path]
                buy = math.prod(source["buy_rate"] for source in sources)
                sell = math.prod(source["sell_rate"] for source in sources)
                snapshot = {"quote_currency": quote_currency, "account_currency": account_currency,
                            "buy_rate": buy, "sell_rate": sell,
                            "asof_ms": max(source["asof_ms"] for source in sources),
                            "expires_ms": min(source["expires_ms"] for source in sources), "sources": sources}
                observed = _stamp(self.provider.now())
                if abs(observed - now) > MAX_RATE_AGE_MS:
                    raise DataError("Conversion observation clock changed during capture")
                snapshot = validate_fx(snapshot, quote_currency, observed, account_currency)
                self.cache[key] = snapshot
                return copy.deepcopy(snapshot)
            except Exception:
                # Provider/network exceptions can contain credentials; only this
                # fixed message is allowed into the ledger or dashboard.
                raise DataError("Fresh market conversion to " + account_currency + " is unavailable") from None

    def _index_pairs(self):
        deribit = getattr(self.provider, "deribit", None)
        if deribit is None:
            raise DataError("A Deribit reference-index client is required for the USD account")
        names = deribit.available_indices()
        result = {}
        for name in names:
            if not isinstance(name, str):
                raise DataError("Invalid conversion index catalog")
            parts = name.split("_")
            if len(parts) != 2 or any(not part.isalnum() or not 1 <= len(part) <= 30 for part in parts):
                continue
            base, quote = (part.upper() for part in parts)
            if base == quote:
                continue
            result[base + "/" + quote] = name
        return result

    def _path(self, quote_currency, account_currency, preference):
        selected = {}
        binance = getattr(self.provider, "binance", None)
        if preference == "binance":
            if binance is None:
                raise DataError("A Binance Spot client is required for this conversion path")
            selected = {normalise_pair(pair): {"pair": normalise_pair(pair), "venue": "Binance"}
                        for pair in binance.available_pairs()}
            native_path = self._find_path(selected, quote_currency, account_currency, "Binance")
            if native_path is not None:
                return native_path
        indices = self._index_pairs()
        for pair, index in indices.items():
            base, quote = pair.split("/")
            # When Binance has no native USD terminal, the published USD
            # reference is an explicit account conversion. It does not replace
            # the configured market candles or the trained neural input feed.
            usd_terminal = "USD" in {base, quote} and ({base, quote} - {"USD"}) <= {"USDC", "USDT"}
            reverse = quote + "/" + base
            if preference == "deribit_first" or (usd_terminal and pair not in selected and reverse not in selected):
                selected[pair] = {"pair": pair, "venue": "Deribit", "index_name": index}
        direct, reverse = quote_currency + "/" + account_currency, account_currency + "/" + quote_currency
        if direct in selected:
            return [{**selected[direct], "from_currency": quote_currency, "to_currency": account_currency, "direction": "direct"}]
        if reverse in selected:
            return [{**selected[reverse], "from_currency": quote_currency, "to_currency": account_currency, "direction": "inverse"}]
        if binance is None:
            raise DataError("A Binance Spot client is required for this conversion path")
        if preference != "binance":
            for pair in binance.available_pairs():
                pair = normalise_pair(pair)
                if pair not in selected:
                    selected[pair] = {"pair": pair, "venue": "Binance"}
        path = self._find_path(selected, quote_currency, account_currency,
                               "Deribit" if preference == "deribit_first" else "Binance")
        if path is None:
            raise DataError("No actual currency-conversion path exists")
        return path

    @staticmethod
    def _find_path(selected, quote_currency, account_currency, preferred_venue):
        graph = {}
        for pair, edge in selected.items():
            base, quote = pair.split("/")
            graph.setdefault(base, []).append({**edge, "from_currency": base, "to_currency": quote, "direction": "direct"})
            graph.setdefault(quote, []).append({**edge, "from_currency": quote, "to_currency": base, "direction": "inverse"})
        for edges in graph.values():
            edges.sort(key=lambda edge: (edge["venue"] != preferred_venue, edge["to_currency"], edge["pair"]))
        queue = [(quote_currency, [], {quote_currency})]
        reached = {quote_currency}
        for currency, path, visited in queue:
            if len(path) >= 3:
                continue
            for edge in graph.get(currency, []):
                target = edge["to_currency"]
                if target in visited or target in reached:
                    continue
                extended = path + [edge]
                if target == account_currency:
                    return extended
                reached.add(target)
                queue.append((target, extended, visited | {target}))
        return None

    def _capture(self, edge, account_currency):
        started = self.monotonic()
        pair = edge["pair"]
        if edge["venue"] == "Deribit":
            body = self.provider.deribit.client.request("public/get_index_price", {"index_name": edge["index_name"]})
            price = _rate(body.get("index_price") if isinstance(body, dict) else None)
            bid = ask = price
            metadata = {"source": "Deribit published currency index", "market": "index",
                        "index_name": edge["index_name"], "synthetic": True, "executable": False,
                        "spread_observed": False, "role": "usd_account_reference" if "USD" in pair.split("/") else "currency_reference"}
        else:
            body = self.provider.binance.quotes([pair])[pair]
            if not isinstance(body, dict) or body.get("symbol") != pair:
                raise DataError("Conversion quote does not match the currency pair")
            bid, ask = _rate(body.get("bid")), _rate(body.get("ask"))
            metadata = {"source": "Binance Spot book ticker", "market": "spot", "instrument": pair.replace("/", ""),
                        "synthetic": False, "executable": True, "spread_observed": True,
                        "provider_observed_ms": body.get("asof_ms"), "role": "currency_market"}
        observed = _stamp(self.provider.now())
        if self.monotonic() - started > 5 or ask < bid:
            raise DataError("Currency market observation is stale or inverted")
        buy, sell = (ask, bid) if edge["direction"] == "direct" else (1. / bid, 1. / ask)
        return {**edge, **metadata, "bid": bid, "ask": ask, "buy_rate": buy, "sell_rate": sell,
                "asof_ms": observed, "expires_ms": observed + CACHE_MS}

    def close(self):
        with self.lock:
            self.cache.clear()
