"""Independent DVOL pairs, validated against Deribit markets and indices."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import threading
import time

from .core import DataError, normalise_pair, finite
from .storage import atomic_json

DEFAULT_DOCUMENT = {"pairs": [{"symbol": "BTC/USDC", "enabled": True},
                              {"symbol": "ETH/USDC", "enabled": True}]}
SUPPORTED_BASES = frozenset({"BTC", "ETH"})


def validate_document(document):
    """Validate the file shape without borrowing the dashboard asset list."""
    if not isinstance(document, dict) or set(document) != {"pairs"}:
        raise DataError("DVOL settings must contain pairs only.")
    rows = document["pairs"]
    if not isinstance(rows, list) or len(rows) > 40:
        raise DataError("DVOL supports up to 40 pair rows.")
    seen, result = set(), []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"symbol", "enabled"}:
            raise DataError("Each DVOL row needs symbol and enabled.")
        if not isinstance(row["symbol"], str) or type(row["enabled"]) is not bool:
            raise DataError("Choose a DVOL currency pair and Enabled or Disabled.")
        symbol = normalise_pair(row["symbol"])
        parts = symbol.split("/")
        if len(parts) != 2 or parts[0] not in SUPPORTED_BASES or not re.fullmatch(r"[A-Z0-9]{2,30}", parts[1]):
            raise DataError("Deribit publishes DVOL only for BTC and ETH. Choose an available Deribit BTC or ETH pair.")
        if symbol in seen:
            raise DataError("DVOL currency pairs must be distinct, including disabled rows.")
        seen.add(symbol)
        result.append({"symbol": symbol, "enabled": row["enabled"]})
    return {"pairs": result}


class DVOLSettings:
    """Versioned atomic file, independent from main settings and paper state.

    Main settings apply, paper-account purge and account restore leave this
    file intact. The DVOL editor owns its revision and applies saves immediately.
    """

    def __init__(self, path, client, pair_supported, *, monotonic=time.monotonic,
                 wall_clock=None):
        self.path = Path(path)
        self.client = client
        self.pair_supported = pair_supported
        self.monotonic = monotonic
        self.wall_clock = wall_clock or time.time
        self.lock = threading.RLock()
        self._indices = None
        self._indices_at = None

    def read(self):
        with self.lock:
            try:
                raw = self.path.read_bytes()
            except FileNotFoundError:
                raw = b""
            try:
                document = validate_document(json.loads(raw.decode("utf-8-sig"))) if raw else copy.deepcopy(DEFAULT_DOCUMENT)
            except (ValueError, UnicodeError):
                raise DataError("DVOL settings could not be read. Check dvol-settings.json.") from None
            return document, hashlib.sha256(raw).hexdigest()

    def enabled_pairs(self):
        document, revision = self.read()
        return [row["symbol"] for row in document["pairs"] if row["enabled"]], revision

    def index_pairs(self):
        with self.lock:
            now = self.monotonic()
            if self._indices is not None and self._indices_at is not None and 0 <= now - self._indices_at < 60:
                return set(self._indices)
            names = self.client.request("public/get_index_price_names", {})
            if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                raise DataError("Deribit index availability could not be verified.")
            pairs = set()
            for name in names:
                parts = name.upper().split("_")
                if len(parts) == 2 and parts[0] in SUPPORTED_BASES and re.fullmatch(r"[A-Z0-9]{2,30}", parts[1]):
                    pairs.add("/".join(parts))
            if not pairs:
                raise DataError("Deribit has no available BTC or ETH reference indices.")
            self._indices, self._indices_at = pairs, self.monotonic()
            return set(pairs)

    def available_pairs(self):
        return sorted(pair for pair in self.index_pairs() if self.pair_supported(pair))

    def save(self, document, revision):
        validated = validate_document(document)
        # Network lookup occurs before taking the file lock. Concurrent saves
        # remain protected by a fresh revision check immediately before replace.
        indices = self.index_pairs() if validated["pairs"] else set()
        for row in validated["pairs"]:
            symbol = row["symbol"]
            if symbol not in indices or not self.pair_supported(symbol):
                raise DataError(f"{symbol}: select an available Deribit market with a matching reference index.")
        with self.lock:
            _, current_revision = self.read()
            if not isinstance(revision, str) or revision != current_revision:
                raise DataError("DVOL settings changed in another tab or on disk. Reload before saving.")
            atomic_json(self.path, validated)
            _, new_revision = self.read()
        return {"settings": validated, "revision": new_revision,
                "message": "DVOL pairs saved and applied. Dashboard pairs and Parente inputs are unchanged."}

    def quote(self, pair):
        """Fetch the matching Deribit index; never use the dashboard fallback."""
        pair = validate_document({"pairs": [{"symbol": pair, "enabled": True}]})["pairs"][0]["symbol"]
        if pair not in self.index_pairs() or not self.pair_supported(pair):
            raise DataError("This DVOL pair is no longer available on Deribit.")
        started = self.monotonic()
        result = self.client.request("public/get_index_price", {"index_name": pair.lower().replace("/", "_")})
        if not isinstance(result, dict):
            raise DataError("Deribit DVOL reference price is unavailable.")
        price = finite(result.get("index_price"), "DVOL reference price", 1e-15)
        if self.monotonic() - started > 5:
            raise DataError("Deribit DVOL reference price request is stale.")
        return {"bid": price, "ask": price, "last": price,
                "asof_ms": int(self.wall_clock()*1000), "symbol": pair,
                "source": "Deribit composite index", "quote_kind": "index_reference",
                "synthetic": True, "executable": False, "spread_observed": False}
