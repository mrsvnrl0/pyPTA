"""Read-only Deribit requests, bounded auth renewal, and sanitized failure paths."""
import unittest
from adaptive_crypto.core import DataError
from adaptive_crypto.deribit import DeribitClient

SECRET = "sentinel-private-key"
TOKEN = "sentinel-access-token"


class Response:
    def __init__(self, body, code=200):
        self.body, self.status_code = body, code
    def json(self):
        return self.body


class Session:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.trust_env = True
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def _send(self, verb, url, **kwargs):
        self.calls.append((verb, url, kwargs))
        response = self.replies.pop(0)
        if isinstance(response, Exception): raise response
        return response
    def get(self, url, **kwargs): return self._send("GET", url, **kwargs)
    def post(self, url, **kwargs): return self._send("POST", url, **kwargs)


def auth(token=TOKEN):
    return Response({"result": {"access_token": token, "expires_in": 3600, "token_type": "bearer"}})


class DeribitClientTests(unittest.TestCase):
    def test_public_request_is_get_without_auth_and_encodes_boolean_api_params(self):
        session = Session([Response({"result": []})])
        client = DeribitClient(session_factory=lambda: session)
        self.assertEqual(client.request("public/get_instruments", {"currency": "USDC", "expired": False}), [])
        verb, url, args = session.calls[0]
        self.assertEqual(verb, "GET")
        self.assertTrue(url.endswith("/public/get_instruments"))
        self.assertEqual(args["params"], {"currency": "USDC", "expired": "false"})
        self.assertNotIn("Authorization", args["headers"])
        self.assertFalse(args["allow_redirects"])
        self.assertFalse(session.trust_env)
        self.assertEqual(client.status()["state"], "public")

    def test_unknown_private_and_auth_methods_never_read_credentials_or_open_session(self):
        def forbidden(): raise AssertionError("External access before whitelist validation")
        client = DeribitClient(credentials=forbidden, session_factory=forbidden)
        for method in ("private/buy", "public/auth", "public/unknown", "https://outside.invalid"):
            with self.subTest(method=method), self.assertRaisesRegex(DataError, "Unsupported"):
                client.request(method)

    def test_authentication_uses_post_no_trade_permissions_and_no_secret_in_status(self):
        session = Session([auth(), Response({"result": {"index_price": 100.}})])
        client = DeribitClient(lambda: ("sentinel-client-id", SECRET), session_factory=lambda: session)
        self.assertEqual(client.request("public/get_index_price", {"index_name": "btc_usdc"}), {"index_price": 100.})
        verb, url, args = session.calls[0]
        self.assertEqual(verb, "POST")
        self.assertNotIn(SECRET, url)
        self.assertEqual(args["json"]["params"]["client_secret"], SECRET)
        for scope in ("account:none", "trade:none", "wallet:none"):
            self.assertIn(scope, args["json"]["params"]["scope"])
        self.assertEqual(session.calls[1][2]["headers"]["Authorization"], "Bearer "+TOKEN)
        status = repr(client.status())
        for secret in (SECRET, TOKEN, "sentinel-client-id"):
            self.assertNotIn(secret, status)
        self.assertEqual(client.status()["state"], "connected")

    def test_expired_token_reauthenticates_once_and_replays_only_public_read(self):
        session = Session([auth(), Response({"error": {"code": 13009, "message": "token_expired"}}),
                           auth("replacement"), Response({"result": 123})])
        client = DeribitClient(lambda: ("id", SECRET), session_factory=lambda: session)
        self.assertEqual(client.request("public/get_time"), 123)
        self.assertEqual([c[0] for c in session.calls], ["POST", "GET", "POST", "GET"])
        self.assertEqual(session.calls[-1][2]["headers"]["Authorization"], "Bearer replacement")

    def test_repeated_expired_tokens_stop_after_one_retry_and_enter_cooldown(self):
        expired = Response({"error": {"code": 13009, "message": TOKEN}})
        session = Session([auth(), expired, auth("second"), expired])
        client = DeribitClient(lambda: ("id", SECRET), session_factory=lambda: session)
        with self.assertRaises(DataError): client.request("public/get_time")
        with self.assertRaises(DataError): client.request("public/get_time")
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(client.status()["state"], "error")
        self.assertNotIn(TOKEN, repr(client.status()))

    def test_upstream_error_exception_redirect_and_invalid_json_cannot_leak_secrets(self):
        replies = [Response({"error": {"code": 999, "message": SECRET}}),
            RuntimeError("request URL contains "+SECRET), Response({"result": SECRET}, 302),
            Response([SECRET]), Response({"other": SECRET}), Response({}, 503)]
        for reply in replies:
            with self.subTest(reply=type(reply).__name__):
                session = Session([reply]); client = DeribitClient(session_factory=lambda: session)
                with self.assertRaises(DataError) as exc: client.request("public/get_time")
                self.assertNotIn(SECRET, str(exc.exception))
                self.assertNotIn(SECRET, repr(client.status()))

    def test_credential_read_failures_are_sanitized_and_status_performs_no_io(self):
        reads = []
        def credentials():
            reads.append(1)
            raise RuntimeError(SECRET)
        session = Session([])
        client = DeribitClient(credentials, session_factory=lambda: session)
        self.assertEqual(client.status()["state"], "not_connected")
        self.assertEqual(reads, [])
        with self.assertRaises(DataError) as exc: client.request("public/get_time")
        self.assertNotIn(SECRET, str(exc.exception))
        self.assertNotIn(SECRET, repr(client.status()))
        self.assertEqual(len(reads), 1)
        self.assertEqual(session.calls, [])
