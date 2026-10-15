"""Deribit inverse and linear option chains in the selected native price units."""
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from .core import DataError, finite, normalise_pair
from .gex import MAX_AGE_MS, build_profile
from .gex_currency import convert_usd_profile
from .currency_conversion import validate_fx


class DeribitOptions:
    def __init__(self, client, wall_clock=time.time, *, currency_provider=None):
        self.client, self.wall_clock = client, wall_clock
        self.currency_provider = currency_provider
        self.lock = threading.RLock()
        self.catalog = None
        self.chains = {}

    def _catalog(self):
        """Discover bases and settlements from the exchange, never a fixed asset list."""
        with self.lock:
            now = int(self.wall_clock()*1000)
            if self.catalog and 0 <= now-self.catalog[0] < 60000:
                return copy.deepcopy(self.catalog[1])
            rows = self.client.request("public/get_instruments", {
                "currency": "any", "kind": "option", "expired": False})
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise DataError("Invalid Deribit options instrument catalog")
            self.catalog = (int(self.wall_clock()*1000), copy.deepcopy(rows))
            return copy.deepcopy(rows)

    def _chain(self, settlement):
        # One settlement snapshot serves every configured base using that market.
        with self.lock:
            now = int(self.wall_clock()*1000)
            cached = self.chains.get(settlement)
            if cached and 0 <= now-cached[0] < 30000:
                return copy.deepcopy(cached[1])
            summaries = self.client.request("public/get_book_summary_by_currency", {
                "currency": settlement, "kind": "option"})
            if not isinstance(summaries, list):
                raise DataError("Invalid Deribit options chain")
            self.chains[settlement] = (int(self.wall_clock()*1000), copy.deepcopy(summaries))
            return copy.deepcopy(summaries)

    @staticmethod
    def _native_quote(row, base):
        # quote_currency describes option PREMIUMS for inverse contracts. Their
        # strikes and forwards instead use the price_index coordinate (e.g. USD).
        index = row.get("price_index")
        if not isinstance(index, str) or index.lower() != index:
            raise DataError("Deribit option price index is missing or invalid")
        parts = index.upper().split("_")
        if len(parts) != 2 or parts[0] != base:
            raise DataError("Deribit option price index does not match its base asset")
        return normalise_pair("/".join(parts)).split("/")[1]

    def _conversion(self, native, quote):
        if native == quote:
            return 1., None
        now = int(self.wall_clock()*1000)
        if {native, quote} <= {"USD", "USDC"}:
            result = self.client.request("public/get_index_price", {
                "index_name": native.lower()+"_"+quote.lower()})
            rate = finite(result.get("index_price"), "Deribit quote conversion", 1e-15)
            return rate, int(self.wall_clock()*1000)+MAX_AGE_MS
        if self.currency_provider is None:
            raise DataError("A captured currency reference is required for this option price index")
        fx = self.currency_provider.quote(native, quote, now, refresh=True)
        fx = validate_fx(fx, native, int(self.wall_clock()*1000), quote)
        return (fx["buy_rate"]+fx["sell_rate"])/2, fx["expires_ms"]

    @staticmethod
    def _unavailable(base, quote, sources, error):
        return {"status": "unavailable", "stale": False, "source": "Deribit options",
                "base": base, "symbol": base+"/"+quote, "price_currency": quote,
                "exposure_currency": quote, "quote_currency": quote, "asof_ms": None,
                "sources": sources, "coverage_complete": False, "option_count": 0,
                "error": error}

    def fetch(self, base, quote):
        base, quote = normalise_pair(base+"/"+quote).split("/")
        if quote not in {"USD", "USDC"}:
            canonical = self.fetch(base, "USD")
            if canonical.get("status") == "unavailable":
                result = copy.deepcopy(canonical)
                result.update(symbol=base+"/"+quote, price_currency=quote, exposure_currency=quote, quote_currency=quote)
                return result
            if self.currency_provider is None:
                raise DataError("A captured currency reference is required for this options quote")
            fx = self.currency_provider.quote(quote, "USD", int(self.wall_clock()*1000), refresh=True)
            return convert_usd_profile(canonical, quote, fx, int(self.wall_clock()*1000))
        started = int(self.wall_clock()*1000)
        selected = [row for row in self._catalog() if row.get("base_currency") == base
                    and row.get("kind") == "option" and row.get("is_active")
                    and finite(row.get("expiration_timestamp"), "option expiry", 0) > started]
        if not selected:
            return self._unavailable(base, quote, [],
                                     "Deribit has no currently listed options for "+base)
        groups = {}
        for row in selected:
            settlement = row.get("settlement_currency")
            if (not isinstance(settlement, str) or not 1 <= len(settlement) <= 30
                    or not settlement.isalnum() or settlement != settlement.upper()):
                raise DataError("Deribit option settlement currency is missing or invalid")
            native = self._native_quote(row, base)
            groups.setdefault((settlement, native), []).append(row)
        settlements = sorted({settlement for settlement, _ in groups}, key=lambda value: (value != base, value))
        reference = self.client.request("public/get_index_price", {"index_name": base.lower()+"_"+quote.lower()})
        spot = finite(reference.get("index_price"), "Deribit index", 1e-15)
        # Convert actual strike/forward coordinates, never the premium currency.
        rates = {native: self._conversion(native, quote) for native in sorted({native for _, native in groups})}
        deadlines = [deadline for _, deadline in rates.values() if deadline is not None]
        sources, instruments, summaries = [], [], []
        seen = set()
        with ThreadPoolExecutor(max_workers=len(settlements), thread_name_prefix="deribit-options") as pool:
            jobs = {settlement: pool.submit(self._chain, settlement) for settlement in settlements}
            ordered = sorted(groups, key=lambda key: (settlements.index(key[0]), key[1]))
            for settlement, native in ordered:
                future = jobs[settlement]
                source = {"key": "Deribit:"+base+":"+settlement, "venue": "Deribit",
                          "settlement": settlement, "quote_currency": quote,
                          "native_quote": native, "price_index": base.lower()+"_"+native.lower(),
                          "status": "unavailable", "option_count": 0, "error": None}
                sources.append(source)
                try:
                    raw_summaries = future.result()
                    rate = rates[native][0]
                    selected_group = groups[(settlement, native)]
                    names = {row["instrument_name"] for row in selected_group}
                    if len(names) != len(selected_group) or seen.intersection(names):
                        raise DataError("Duplicate Deribit options contract")
                    books = [row for row in raw_summaries if row.get("instrument_name") in names]
                    local_inst, local_books = [], []
                    for row in selected_group:
                        local_inst.append({**row, "strike": finite(row["strike"], "strike", 1e-15)*rate})
                    for row in books:
                        normalized = dict(row)
                        if finite(row.get("open_interest"), "option open interest", 0) > 0:
                            normalized["underlying_price"] = finite(row.get("underlying_price"), "option forward", 1e-15)*rate
                        # API open_interest is already BASE COINS, including linear options.
                        # Multiplying SOL's contract_size=10 here would overstate exposure 10x.
                        local_books.append(normalized)
                    now = int(self.wall_clock()*1000)
                    try:
                        check = build_profile(local_inst, local_books, base, spot, now, quote_currency=quote)
                    except DataError as exc:
                        if str(exc) != "No active options with positive open interest for this asset":
                            raise
                        source.update(status="no_open_interest", eligible_count=len(local_inst))
                        continue
                    source.update(status="ready", option_count=check["option_count"],
                                  eligible_count=check["eligible_count"], asof_ms=check["asof_ms"],
                                  native_quote=native, conversion_factor=rate, net_gex=check["net_gex"])
                    seen.update(names)
                    instruments.extend(local_inst)
                    summaries.extend(local_books)
                except Exception as exc:
                    source["error"] = str(exc)[:200] if isinstance(exc, DataError) else "Deribit options chain unavailable"
        now = int(self.wall_clock()*1000)
        if not instruments:
            return self._unavailable(base, quote, sources,
                                     "No complete current Deribit option chain is available for this asset")
        if now-started > 30000:
            raise DataError("Deribit options snapshot took too long; waiting for synchronized inputs")
        result = build_profile(instruments, summaries, base, spot, now, quote_currency=quote)
        complete = all(row["status"] in {"ready", "not_listed", "no_open_interest"} for row in sources)
        result.update(status="ready" if complete else "partial", coverage_complete=complete,
                      sources=sources, source="Deribit options", symbol=base+"/"+quote,
                      market=base+" Deribit options across listed settlement markets",
                      reference_index="Deribit "+base+"/"+quote+" composite index",
                      **({"fx_valid_until_ms": min(deadlines)} if deadlines else {}),
                      error=None if complete else "Partial Deribit coverage; excluded from GEX target selection")
        return result
