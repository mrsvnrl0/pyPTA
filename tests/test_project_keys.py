"""Named project key fallback, saved overrides and explicit disable precedence."""
import json
from pathlib import Path
import tempfile
import unittest
from adaptive_crypto.core import DataError
from adaptive_crypto.project_keys import read_project_key
from adaptive_crypto.deribit_settings import DeribitCredentials
from adaptive_crypto.connection_credentials import ConnectionCredentials


def identity(data, decrypt=False): return data


class ProjectKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/"keys").mkdir()
    def key(self, filename, value):
        (self.root/"keys"/filename).write_text(value, encoding="utf-8")
    def deribit_files(self):
        self.key("CLIENT_ID_DERIBIT.md", "file-client-id")
        self.key("CLIENT_SECRET_DERIBIT.md", "file-client-secret")
    def telegram_files(self):
        self.key("TELEGRAM_BOT_TOKEN.md", "123:file-token")
        self.key("TELEGRAM_CHAT_ID.md", "-123456")

    def test_named_key_only_optional_missing_and_utf8_bom(self):
        self.key("unrelated.md", "do-not-load")
        self.assertEqual(read_project_key(self.root, "CLIENT_ID_DERIBIT.md"), "")
        self.key("CLIENT_ID_DERIBIT.md", "\ufeffactual-key\n")
        self.assertEqual(read_project_key(self.root, "CLIENT_ID_DERIBIT.md"), "actual-key")

    def test_invalid_key_content_fails_without_echoing_plaintext(self):
        for value in ("private secret", "secret\nsecond", "x"*4097, "secret\x00value", "nonascii-\u00e9", ""):
            with self.subTest(value_length=len(value)):
                self.key("CLIENT_ID_DERIBIT.md", value)
                with self.assertRaises(DataError) as exc:
                    read_project_key(self.root, "CLIENT_ID_DERIBIT.md")
                self.assertNotIn("private", str(exc.exception))
                self.assertNotIn("nonascii", str(exc.exception))

    def test_deribit_project_files_and_secret_free_status(self):
        self.deribit_files()
        creds = DeribitCredentials(self.root, environment={}, protector=identity)
        self.assertEqual(creds.read(), ("file-client-id", "file-client-secret"))
        self.assertEqual(creds.status()["source"], "project keys")
        self.assertNotIn("file-client", json.dumps(creds.status()))

    def test_deribit_environment_and_saved_override_files_then_disable_survives_restart(self):
        self.deribit_files()
        env = {"DERIBIT_CLIENT_ID": "environment-id", "DERIBIT_CLIENT_SECRET": "environment-secret"}
        creds = DeribitCredentials(self.root, environment=env, protector=identity)
        self.assertEqual(creds.read(), ("environment-id", "environment-secret"))
        creds.save("saved-id", "saved-secret")
        self.assertEqual(creds.read(), ("saved-id", "saved-secret"))
        creds.disable()
        restarted = DeribitCredentials(self.root, environment=env, protector=identity)
        self.assertIsNone(restarted.read())
        self.assertEqual(restarted.status()["source"], "public")

    def test_deribit_partial_environment_never_mixes_in_project_secret(self):
        self.deribit_files()
        creds = DeribitCredentials(self.root, environment={"DERIBIT_CLIENT_ID": "environment-id"}, protector=identity)
        with self.assertRaises(DataError): creds.read()
        self.assertFalse(creds.status()["configured"])
        self.assertNotIn("file-client-secret", json.dumps(creds.status()))

    def test_telegram_project_files_saved_override_and_disable(self):
        self.telegram_files()
        creds = ConnectionCredentials(self.root, environment={}, protector=identity)
        self.assertEqual(creds.read("telegram"), {"bot_token": "123:file-token", "chat_id": "-123456"})
        self.assertEqual(creds.status("telegram")["source"], "project keys")
        self.assertNotIn("file-token", json.dumps(creds.status("telegram")))
        creds.save_telegram("456:saved-token", "987654")
        self.assertEqual(creds.read("telegram")["bot_token"], "456:saved-token")
        creds.disable("telegram")
        restarted = ConnectionCredentials(self.root, environment={}, protector=identity)
        self.assertIsNone(restarted.read("telegram"))
        self.assertFalse(restarted.status("telegram")["enabled"])

    def test_telegram_partial_environment_does_not_mix_other_account_file_fields(self):
        self.telegram_files()
        creds = ConnectionCredentials(self.root, environment={"TELEGRAM_BOT_TOKEN": "999:env-token"}, protector=identity)
        with self.assertRaises(DataError): creds.read("telegram")
        self.assertFalse(creds.status("telegram")["configured"])

    def test_corrupt_saved_credentials_never_fall_back_to_files(self):
        self.deribit_files(); self.telegram_files()
        (self.root/"deribit-credentials.dat").write_text("corrupt file-client-secret")
        (self.root/"telegram-credentials.dat").write_text("corrupt 123:file-token")
        first = DeribitCredentials(self.root, environment={}, protector=identity)
        second = ConnectionCredentials(self.root, environment={}, protector=identity)
        with self.assertRaises(DataError): first.read()
        with self.assertRaises(DataError): second.read("telegram")
        self.assertNotIn("file-client-secret", json.dumps(first.status()))
        self.assertNotIn("file-token", json.dumps(second.status("telegram")))
