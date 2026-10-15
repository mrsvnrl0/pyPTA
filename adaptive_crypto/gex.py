"""Signed open-interest gamma proxy with an independently refreshed cache."""
from __future__ import annotations

import copy
import math
import threading
import time
from .gex_currency import profile_for_market, require_market_currency
from .core import DataError, finite, normalise_pair, safe_error

YEAR_MS = 365 * 86400000
PRICING_MODEL = "forward-basis-spot-delta-v2"
MAX_AGE_MS = 300000
SMC_MAX_AGE_MS = 45000


def exposure(option, spot):
    """Conventional delta change per 1% index move, in quote currency.

    Reprice each expiry at its observed forward/index ratio (fixed carry).
    d N(d1)/d spot = phi(d1)/(spot * IV * sqrt(T)); using a forward gamma
    without the forward/spot sensitivity would mix the two price coordinates.
    This is not the gamma of the coin-denominated option premium.
    Deribit option OI is already in base coins: do NOT multiply contract_size.
    Calls positive / puts negative is an assumption, not dealer inventory.
    """
    width = option["iv"] * math.sqrt(option["years"])
    forward = spot * option.get("forward_ratio", 1.)
    d1 = (math.log(forward / option["strike"]) + width * width / 2) / width
    gamma = math.exp(-d1 * d1 / 2) / (math.sqrt(2 * math.pi) * spot * width)
    return gamma * option["oi"] * spot * spot * .01 * option["sign"]


def build_profile(instruments, summaries, base, spot, now, *, quote_currency="USD"):
    if quote_currency not in {"USD", "USDC"}:
        raise DataError("Unsupported options quote currency")
    spot = finite(spot, "options index", 1e-15)
    books = {}
    for book in summaries:
        name = book["instrument_name"]
        if name in books:
            raise DataError("Ambiguous options snapshot: duplicate instrument summary")
        books[name] = book
    options, stamps, expiries = [], [], []
    seen = set()
    eligible = 0
    forward_priced = 0
    for inst in instruments:
        if inst.get("base_currency") != base or inst.get("kind") != "option" or not inst.get("is_active"):
            continue
        expiry = finite(inst["expiration_timestamp"], "expiry", 0)
        if expiry <= now:
            continue
        name = inst["instrument_name"]
        if name in seen:
            raise DataError("Ambiguous options snapshot: duplicate active instrument")
        seen.add(name)
        eligible += 1
        book = books.get(inst["instrument_name"])
        if book is None:
            raise DataError("Incomplete options snapshot: missing instrument summary")
        stamp = finite(book["creation_timestamp"], "options timestamp", 0)
        if not -2000 <= now - stamp <= MAX_AGE_MS:
            raise DataError("Stale options summary")
        stamps.append(stamp)
        oi = finite(book["open_interest"], "option open interest", 0)
        if oi == 0:
            continue
        kind = inst.get("option_type")
        if kind not in {"call", "put"}:
            raise DataError("Unknown option type")
        # Deribit mark IV uses the expiry forward, not the current index.
        forward = finite(book.get("underlying_price"), "option forward", 1e-15)
        forward_priced += 1
        options.append({"strike": finite(inst["strike"], "strike", 1e-15),
                        "forward_ratio": forward / spot,
                        "iv": finite(book["mark_iv"], "mark IV", 1e-15) / 100,
                        "oi": oi, "years": (expiry - now) / YEAR_MS,
                        "sign": 1 if kind == "call" else -1})
        expiries.append(expiry)
    if not options:
        raise DataError("No active options with positive open interest for this asset")
    strikes = {}
    for option in options:
        k = option["strike"]
        row = strikes.setdefault(k, {"strike": k, "call_gex": 0., "put_gex": 0., "net_gex": 0.})
        value = finite(exposure(option, spot), "gamma exposure")
        row["call_gex" if option["sign"] > 0 else "put_gex"] += value
        row["net_gex"] += value
    rows = sorted(strikes.values(), key=lambda r: r["strike"])
    calls = [r for r in rows if r["call_gex"] > 0]
    puts = [r for r in rows if r["put_gex"] < 0]
    call = max(calls, key=lambda r: r["call_gex"], default=None)
    put = min(puts, key=lambda r: r["put_gex"], default=None)
    def total(price):
        return finite(math.fsum(exposure(o, price) for o in options), "net gamma")
    # Reprice the entire chain with fixed IV/OI. A strike cumulative sum is NOT a flip.
    curve = [{"price": spot * (.7 + i * .005)} for i in range(121)]
    for point in curve:
        point["gex"] = total(point["price"])
    roots = []
    nonzero = [p for p in curve if p["gex"] != 0]
    for left, right in zip(nonzero, nonzero[1:]):
        if (left["gex"] > 0) == (right["gex"] > 0):
            continue
        low, high, sign = left["price"], right["price"], left["gex"] > 0
        for _ in range(32):
            mid = (low + high) / 2
            if (total(mid) > 0) == sign:
                low = mid
            else:
                high = mid
        roots.append((low + high) / 2)
    net = total(spot)
    gross = math.fsum(abs(exposure(o, spot)) for o in options)
    return {"status": "ready", "stale": False, "error": None, "source": "Deribit",
            "base": base, "spot": spot, "asof_ms": int(min(stamps)), "calculated_ms": now,
            "expires_ms": int(min(expiries)), "option_count": len(options), "eligible_count": eligible,
            "strikes": rows, "net_gex": net, "gross_gex": gross,
            "net_to_gross": abs(net)/gross if gross else None,
            "pricing_model": PRICING_MODEL, "day_count": "ACT/365",
            "forward_priced_count": forward_priced,
            "regime": "positive" if net > 0 else "negative" if net < 0 else "neutral",
            "call_wall": copy.deepcopy(call), "put_wall": copy.deepcopy(put),
            "gamma_flip": min(roots, key=lambda p: abs(p-spot)) if roots else None,
            "flip_candidates": roots, "flip_range": [curve[0]["price"], curve[-1]["price"]],
            "curve": curve, "price_currency":quote_currency, "quote_currency":quote_currency, "exposure_currency":quote_currency,
            "units": quote_currency+" delta per 1% index move",
            "sign_assumption": "Calls positive, puts negative; dealer inventory is not observed",
            "wall_method": "Largest call gamma and largest absolute put gamma separately"}


class DeribitGEX:
    """Cache Deribit chains independently of NN inference and stop monitoring."""
    def __init__(self, assets, provider=None, ttl=120):
        self.assets = {name: normalise_pair(cfg["symbol"]) for name, cfg in assets.items()}
        self.provider, self.ttl = provider, ttl
        self.cache, self.published, self.feed_history = {}, {}, {}
        self.locks = {name: threading.Lock() for name in assets}
        self.snapshot_lock = threading.Lock()
        self.refreshing, self.next_refresh = set(), {}

    @staticmethod
    def _current_copy(profile, now):
        result = copy.deepcopy(profile)
        if result and result.get("status") in {"ready", "partial"}:
            stamp, expiry = result.get("asof_ms"), result.get("expires_ms")
            deadline = result.get("fx_valid_until_ms", expiry)
            if (type(stamp) is not int or type(expiry) is not int or type(deadline) is not int
                    or not 0 <= now-stamp < MAX_AGE_MS or now >= min(expiry, deadline)):
                result.update(status="stale", stale=True, error="Deribit options snapshot or quote conversion expired")
        return result

    def diagnostics(self, now_ms=None):
        now = int(time.time()*1000) if now_ms is None else now_ms
        mono = time.monotonic()
        with self.snapshot_lock:
            published, history = copy.deepcopy(self.published), copy.deepcopy(self.feed_history)
            refreshing, scheduled = set(self.refreshing), dict(self.next_refresh)
        rows = {}
        for name, symbol in self.assets.items():
            profile = self._current_copy(published.get(name), now) or {}
            detail = history.get(name, {})
            stamp, expiry = profile.get("asof_ms"), profile.get("expires_ms")
            valid = min(stamp+MAX_AGE_MS, expiry, profile.get("fx_valid_until_ms",expiry)) if stamp and expiry else None
            busy = name in refreshing or detail.get("in_progress",False)
            retry = detail.get("retry_at",0)
            rows[name] = {"status":profile.get("status","unknown"),"error":profile.get("error"),
                "asof_ms":stamp,"valid_until_ms":valid,"refreshing":busy,
                "last_attempt_ms":detail.get("last_attempt_ms"),"last_success_ms":detail.get("last_success_ms"),
                "last_failure_ms":detail.get("last_failure_ms"),"last_failure":detail.get("last_failure"),
                "sources":profile.get("sources",[]),"coverage_complete":profile.get("coverage_complete"),
                "price_currency":symbol.split("/")[1],"exposure_currency":symbol.split("/")[1],
                "next_eligible_ms":None if busy else now+max(0,int((retry-mono)*1000)),
                "next_scan_eligible_ms":None if busy else now+max(0,int((max(retry,scheduled.get(name,0))-mono)*1000)),
                "next_check":"Refresh in progress" if busy else "Next scheduled Deribit options snapshot"}
        return rows

    def snapshot(self, name):
        if name not in self.assets:
            raise KeyError(name)
        with self.snapshot_lock:
            result = copy.deepcopy(self.published.get(name))
            if name not in self.refreshing and time.monotonic() >= self.next_refresh.get(name,0):
                self.refreshing.add(name)
                def refresh():
                    try:
                        self.get(name)
                    finally:
                        with self.snapshot_lock:
                            self.refreshing.discard(name)
                            self.next_refresh[name] = self.cache.get(name, {}).get("retry_at", time.monotonic()+self.ttl)
                threading.Thread(target=refresh,daemon=True,name="gex-"+name).start()
        return self._current_copy(result,int(time.time()*1000))

    def get(self,name):
        if name not in self.assets:
            raise KeyError(name)
        base,quote = self.assets[name].split("/")
        with self.locks[name]:
            now=int(time.time()*1000)
            cached=self.cache.get(name)
            if cached and time.monotonic()<cached["retry_at"]:
                result=self._current_copy(cached["result"],now)
            else:
                with self.snapshot_lock:
                    detail=self.feed_history.setdefault(name,{})
                    detail.update(last_attempt_ms=now,in_progress=True)
                try:
                    if self.provider is None:
                        raise DataError("Deribit options provider is required")
                    result=self.provider(base,quote)
                    if not isinstance(result,dict):
                        raise DataError("Invalid Deribit GEX response")
                    require_market_currency(result,self.assets[name],int(time.time()*1000))
                    if result.get("status")=="ready" and result.get("coverage_complete") is not True:
                        raise DataError("Incomplete Deribit options coverage")
                    if result.get("status")=="unavailable" and cached and cached["result"].get("strikes"):
                        failure=result
                        result=copy.deepcopy(cached["result"])
                        result.update(status="stale",stale=True,coverage_complete=False,
                                      sources=failure.get("sources",[]),error=failure.get("error"))
                    with self.snapshot_lock:
                        if result.get("status")=="ready":
                            self.feed_history[name]["last_success_ms"]=int(time.time()*1000)
                        else:
                            self.feed_history[name].update(last_failure_ms=int(time.time()*1000),last_failure=result.get("error"))
                except Exception as exc:
                    result=copy.deepcopy(cached["result"]) if cached else {"asof_ms":None,"base":base,"symbol":self.assets[name],"price_currency":quote,"exposure_currency":quote}
                    result.update(status="stale" if result.get("asof_ms") else "unavailable",stale=True,error=safe_error(exc))
                    with self.snapshot_lock:
                        self.feed_history[name].update(last_failure_ms=int(time.time()*1000),last_failure=result["error"])
                delay = self.ttl
                deadline = result.get("fx_valid_until_ms")
                if result.get("status") in {"ready", "partial"} and type(deadline) is int:
                    delay = min(delay, max(1., (deadline-int(time.time()*1000))/1000-5.))
                self.cache[name]={"result":copy.deepcopy(result),"retry_at":time.monotonic()+delay}
                with self.snapshot_lock:
                    self.feed_history[name].update(in_progress=False,retry_at=self.cache[name]["retry_at"])
                result=self._current_copy(result,int(time.time()*1000))
            with self.snapshot_lock:
                self.published[name]=copy.deepcopy(result)
            return result


def map_smc(profile, row, now):
    """Exact wall/block overlap with current, healthy SMC evidence only."""
    try:
        symbol = row.get("symbol") or profile.get("symbol")
        result = profile_for_market(profile, symbol, now)
        currency_deadline = require_market_currency(result, symbol, now)
    except (DataError, TypeError, ValueError, KeyError) as exc:
        result = copy.deepcopy(profile)
        result.update(status="unavailable", stale=True, error="GEX/SMC price coordinate unavailable: " + safe_error(exc))
        currency_deadline = None
    quote = row.get("quote") or {}
    price, quote_until, scan_until = None, None, None
    try:
        bid = finite(quote.get("bid"), "Spot bid", 1e-15)
        ask = finite(quote.get("ask"), "Spot ask", 1e-15)
        last = finite(quote.get("last"), "Spot last", 1e-15)
        stamp = finite(quote.get("asof_ms"), "Spot quote time", 0)
        if bid <= ask and -2000 <= now-stamp <= SMC_MAX_AGE_MS and not row.get("errors", {}).get("clock"):
            price, quote_until = last, int(stamp+SMC_MAX_AGE_MS)
        scan_stamp = finite(row.get("asof_ms"), "SMC scan time", 0)
        if -2000 <= now-scan_stamp <= SMC_MAX_AGE_MS:
            scan_until = int(scan_stamp+SMC_MAX_AGE_MS)
    except (DataError, TypeError, AttributeError):
        pass
    fresh = (price is not None and scan_until is not None and
             not row.get("errors") and not row.get("scan_error"))
    result["market_source_label"] = row.get("market_source_label", "Deribit composite index")
    result["price"] = price
    result["price_valid_until_ms"] = quote_until
    result["smc_valid_until_ms"] = min(quote_until, scan_until) if fresh else None
    result["options_valid_until_ms"] = (min(result["asof_ms"]+MAX_AGE_MS, result["expires_ms"])
                                         if result.get("asof_ms") is not None and result.get("expires_ms") else None)
    if currency_deadline is not None and result["options_valid_until_ms"] is not None:
        result["options_valid_until_ms"] = min(result["options_valid_until_ms"], currency_deadline)
    if result.get("status") in {"ready", "partial"} and (result.get("stale") or
            result["options_valid_until_ms"] is None or now >= result["options_valid_until_ms"]
            or now < result["asof_ms"]-2000):
        result.update(status="stale", stale=True, error="Options snapshot aged or an included expiry settled")
    ready = result.get("status") == "ready" and result.get("coverage_complete") is True
    result["smc_fresh"] = bool(fresh)
    result["basis_percent"] = (100*(result["spot"]/price-1)
                               if ready and price is not None and result.get("spot") else None)
    result["levels"] = []
    for key, label, value in (("call", "Call wall", (result.get("call_wall") or {}).get("strike")),
                              ("flip", "Gamma flip", result.get("gamma_flip")),
                              ("put", "Put wall", (result.get("put_wall") or {}).get("strike"))):
        distance = 100*(value/price-1) if ready and value is not None and price is not None else None
        result["levels"].append({"key": key, "label": label, "price": value, "distance_percent": distance,
                                 "relation": None if distance is None else
                                 "above" if distance > 0 else "below" if distance < 0 else "at"})
    zones = []
    result["zone_errors"] = []
    pine = row.get("pine_structure") or (row.get("gex_context") or {}).get("pine_structure")
    if isinstance(pine, dict) and pine.get("source") == "pine_smc":
        # Pine volume-pivot blocks are authoritative for the dashboard; never
        # reconstruct a different SMC block in the options view.
        zones = []
        if fresh and pine.get("status") == "ready":
            for zone in pine.get("zones", []):
                if zone.get("status") not in {"active", "tested", "mitigated"}:
                    continue
                try:
                    low = finite(zone["low"], "Pine block low", 1e-15)
                    high = finite(zone["high"], "Pine block high", low)
                    known = int(finite(zone["known_ms"], "Pine block confirmation", 0))
                    if known > now or zone.get("side") not in {"long", "short"}:
                        continue
                    wall = result.get("put_wall" if zone["side"] == "long" else "call_wall")
                    reference = zone["status"] == "mitigated"
                    nearest = low if price < low else high if price > high else price
                    zones.append({"label": "Perky SMC " + zone["side"] + " order block",
                        "low": low, "high": high, "side": zone["side"], "confirmed_ms": known,
                        "wall_price": wall["strike"] if wall else None,
                        "distance_percent": 100*(nearest/price-1),
                        "relation": "above" if price < low else "below" if price > high else "inside",
                        "reference_only": reference,
                        "confluence": bool(ready and wall and low <= wall["strike"] <= high and not reference)})
                except (DataError, KeyError, TypeError):
                    result["zone_errors"].append("Invalid Perky SMC order block")
    result["zones"] = zones
    confluence = copy.deepcopy(row.get("gex_context") or {})
    selection_fresh = (fresh and ready and confluence.get("ready") and
                       confluence.get("price_currency", "USD") == result.get("price_currency", "USD") and
                       confluence.get("gex_asof_ms") == result.get("asof_ms") and
                       now < confluence.get("valid_until_ms", 0))
    result["pois"] = (confluence.get("pois", []) if fresh and
                      confluence.get("price_currency", "USD") == result.get("price_currency", "USD") else [])
    result["target_confluence"] = {**confluence, "ready": bool(selection_fresh),
        "matches": confluence.get("matches", []) if selection_fresh else [],
        "reason": confluence.get("reason", "Waiting for SMC target analysis") if selection_fresh or not confluence.get("ready")
                  else "Waiting for target analysis with current GEX and SMC data"}
    return result
