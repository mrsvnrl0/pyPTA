"""Read-only Deribit market data with optional, memory-only OAuth sessions."""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import uuid

import requests

from .core import DataError


API_URL = "https://www.deribit.com/api/v2/"
REQUEST_TIMEOUT = (4, 8)
AUTH_COOLDOWN = 30.0
PUBLIC_METHODS = {"public/get_instruments", "public/get_book_summary_by_currency", "public/get_index_price",
                  "public/get_tradingview_chart_data", "public/get_time", "public/get_index_price_names",
                  "public/get_index_chart_data", "public/get_volatility_index_data", "public/ticker"}
AUTH_ERROR = "Deribit authentication failed; check the configured production API credentials"
CREDENTIAL_ERROR = "Deribit credentials could not be read; check the connection settings"
CHANGED_ERROR = "Deribit credentials changed; retry the market-data request"


class _ClientError(DataError):
    """Only messages created by this module may reach the dashboard."""


class _AuthError(_ClientError):
    pass


class _TokenError(_AuthError):
    pass


class DeribitClient:
    """Fetch only public market data, attaching a bearer token when configured.

    ``credentials`` returns None for public access or (client_id, client_secret).
    An incomplete or unreadable configuration fails closed. Session factories and
    clocks are injectable for offline tests. Status and invalidation never wait
    for the authentication/network lock or call the credentials provider.
    """

    def __init__(self, credentials=None, session_factory=requests.Session,
                 clock=time.monotonic, wall_clock=time.time):
        self._credentials = credentials or (lambda: None)
        self._session_factory = session_factory
        self._clock = clock
        self._wall_clock = wall_clock
        self._auth_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._session_scope = "session:pypta-" + uuid.uuid4().hex[:12]
        self._generation = 0
        self._fingerprint = object()
        self._token = None
        self._expires = 0.0
        self._retry_at = 0.0
        self._failure = None
        self._status = self._empty_status()

    @staticmethod
    def _empty_status():
        return {"configured": None, "mode": "unknown", "state": "not_connected",
                "error": None, "last_success_ms": None, "last_failure_ms": None,
                "token_expires_ms": None, "retry_at_ms": None}

    def status(self):
        """Return sanitized cached metadata without performing IO."""
        with self._state_lock:
            result = dict(self._status)
            if result["state"] == "connected" and self._clock() >= self._expires:
                result["state"] = "not_connected"
            return result

    def invalidate(self):
        """Forget an old configuration after a settings change, without IO."""
        with self._state_lock:
            self._generation += 1
            self._fingerprint = object()
            self._token = None
            self._expires = self._retry_at = 0.0
            self._failure = None
            self._status = self._empty_status()

    def _failed(self, message, generation, *, cooldown=False):
        with self._state_lock:
            if generation != self._generation:
                return
            self._status.update(state="error", error=message,
                                last_failure_ms=int(self._wall_clock() * 1000))
            if cooldown:
                self._token = None
                self._expires = 0.0
                self._retry_at = self._clock() + AUTH_COOLDOWN
                self._failure = message
                self._status.update(token_expires_ms=None,
                                    retry_at_ms=int((self._wall_clock() + AUTH_COOLDOWN) * 1000))

    @staticmethod
    def _rpc(session, method, params, token=None):
        # No arbitrary URLs or methods, URL credentials, redirects, or response
        # messages may reach callers. requests exceptions can contain secrets.
        if method not in PUBLIC_METHODS | {"public/auth"}:
            raise _ClientError("Unsupported Deribit market-data request")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        try:
            if method == "public/auth":
                response = session.post(API_URL + method,
                                        json={"jsonrpc": "2.0", "id": uuid.uuid4().hex,
                                              "method": method, "params": params},
                                        headers=headers, timeout=REQUEST_TIMEOUT,
                                        allow_redirects=False)
            else:
                response = session.get(API_URL + method, params={key: (str(value).lower() if type(value) is bool else value) for key, value in params.items()}, headers=headers,
                                       timeout=REQUEST_TIMEOUT, allow_redirects=False)
            if response.status_code == 401:
                raise _TokenError(AUTH_ERROR)
            if response.status_code == 403:
                raise _AuthError(AUTH_ERROR)
            if 300 <= response.status_code < 400:
                raise _ClientError("Deribit refused a redirected request")
            if response.status_code != 200:
                raise _ClientError("Deribit market-data service is unavailable")
            body = response.json()
        except _ClientError:
            raise
        except Exception:
            raise _ClientError("Deribit market-data request failed") from None
        if not isinstance(body, dict):
            raise _ClientError("Deribit returned an invalid market-data response")
        error = body.get("error")
        if error:
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            if code == 13009 or message in ("invalid_token", "token_expired", "token_revoked", "unauthorized"):
                raise _TokenError(AUTH_ERROR)
            if code in (10000, 10001, 13004) or message in ("invalid_credentials", "authorization_required", "insufficient_scope"):
                raise _AuthError(AUTH_ERROR)
            raise _ClientError("Deribit rejected the market-data request")
        if "result" not in body:
            raise _ClientError("Deribit returned an invalid market-data response")
        return body["result"]

    def _get_token(self, session, rejected=None):
        # A single auth request serves simultaneous BTC, ETH and SOL refreshes.
        # The status lock is released before credential IO or HTTP requests.
        with self._auth_lock:
            with self._state_lock:
                generation = self._generation
            try:
                credentials = self._credentials()
                if credentials is not None and (
                        not isinstance(credentials, (tuple, list)) or len(credentials) != 2 or
                        any(not isinstance(part, str) or not part.strip() for part in credentials)):
                    raise ValueError()
                fingerprint = (hashlib.sha256(json.dumps(list(credentials)).encode()).digest()
                               if credentials is not None else None)
            except Exception:
                with self._state_lock:
                    if generation == self._generation:
                        self._status.update(configured=True, mode="authenticated")
                self._failed(CREDENTIAL_ERROR, generation, cooldown=True)
                raise _ClientError(CREDENTIAL_ERROR) from None
            with self._state_lock:
                if generation != self._generation:
                    raise _ClientError(CHANGED_ERROR)
                if fingerprint != self._fingerprint:
                    self._generation += 1
                    generation = self._generation
                    self._fingerprint = fingerprint
                    self._token = None
                    self._expires = self._retry_at = 0.0
                    self._failure = None
                    self._status = self._empty_status()
                    self._status.update(configured=credentials is not None,
                                        mode="authenticated" if credentials else "public",
                                        state="not_connected" if credentials else "public")
                if credentials is None:
                    return None, generation
                if self._clock() < self._retry_at:
                    raise _ClientError(self._failure or AUTH_ERROR)
                if rejected == (self._token, generation):
                    self._token = None
                if self._token is not None and self._clock() < self._expires:
                    return self._token, generation
                self._status.update(state="connecting", error=None, token_expires_ms=None,
                                    retry_at_ms=None)
            try:
                result = self._rpc(session, "public/auth", {
                    "grant_type": "client_credentials", "client_id": credentials[0],
                    "client_secret": credentials[1],
                    "scope": self._session_scope + " account:none trade:none wallet:none"})
                token = result.get("access_token") if isinstance(result, dict) else None
                lifetime = result.get("expires_in") if isinstance(result, dict) else None
                if (not isinstance(token, str) or not token or len(token) > 16384 or
                        any(ord(char) < 33 or ord(char) > 126 for char in token) or
                        result.get("token_type", "bearer").lower() != "bearer" or
                        isinstance(lifetime, bool) or not isinstance(lifetime, (int, float)) or
                        not math.isfinite(lifetime) or not 0 < lifetime <= 315360000):
                    raise ValueError()
            except Exception:
                self._failed(AUTH_ERROR, generation, cooldown=True)
                raise _ClientError(AUTH_ERROR) from None
            with self._state_lock:
                if generation != self._generation:
                    raise _ClientError(CHANGED_ERROR)
                self._token = token
                # Refresh before expiry, including short-lived test/key tokens.
                self._expires = self._clock() + lifetime - min(30.0, lifetime * .1)
                self._retry_at = 0.0
                self._failure = None
                self._status.update(state="connecting", error=None, retry_at_ms=None,
                                    token_expires_ms=int((self._wall_clock() + lifetime) * 1000))
                return token, generation

    def request(self, method, params=None):
        """A bounded, read-only RPC using the shared optional authentication."""
        if method not in PUBLIC_METHODS:
            raise _ClientError("Unsupported Deribit market-data request")
        with self._state_lock:
            generation = self._generation
        try:
            with self._session_factory() as session:
                session.trust_env = False
                token, generation = self._get_token(session)
                try:
                    result = self._rpc(session, method, params or {}, token)
                except _TokenError:
                    if token is None:
                        raise
                    token, generation = self._get_token(session, rejected=(token, generation))
                    result = self._rpc(session, method, params or {}, token)
            with self._state_lock:
                if generation != self._generation:
                    raise _ClientError(CHANGED_ERROR)
                self._status.update(state="connected" if token else "public", error=None,
                                    last_success_ms=int(self._wall_clock()*1000))
            return result
        except _AuthError:
            self._failed(AUTH_ERROR, generation, cooldown=True)
            raise _ClientError(AUTH_ERROR) from None
        except _ClientError as exc:
            self._failed(str(exc), generation)
            raise
        except Exception:
            message = "Deribit market-data request failed"
            self._failed(message, generation)
            raise _ClientError(message) from None
