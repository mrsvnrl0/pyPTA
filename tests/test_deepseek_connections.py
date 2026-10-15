"""DeepSeek profiles use the existing encrypted connection format without exposing keys."""
import base64
import json
from pathlib import Path
import tempfile
import unittest

from adaptive_crypto.ai_prompts import DEFAULT_MARKET_PROMPT
from adaptive_crypto.connection_credentials import ConnectionCredentials, PROVIDERS, environment_ai
from adaptive_crypto.core import DataError


def fake_protector(data, decrypt=False):
    """Reversible fixture encryption; never invokes Windows or live credentials."""
    marker = b"fixture-encrypted:"
    if decrypt:
        if not data.startswith(marker):
            raise ValueError("Not fixture ciphertext")
        return base64.b64decode(data[len(marker):], validate=True)
    return marker + base64.b64encode(data)


class DeepSeekConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.credentials = ConnectionCredentials(self.directory, environment={}, protector=fake_protector)

    def assert_key_absent(self, status, *keys):
        serialized = json.dumps(status)
        self.assertNotIn('"api_key"', serialized)
        for key in keys:
            self.assertNotIn(key, serialized)

    def test_provider_and_environment_default_model(self):
        self.assertEqual(PROVIDERS["deepseek"], "DeepSeek")
        credentials = ConnectionCredentials(self.directory, environment={
            "AI_PROVIDER": " DEEPSEEK ", "DEEPSEEK_API_KEY": " fixture-deepseek-key "
        }, protector=fake_protector)
        config = credentials.read("ai")
        self.assertEqual(config, {"provider": "deepseek", "api_key": "fixture-deepseek-key",
                                  "model": "deepseek-flash", "prompt": DEFAULT_MARKET_PROMPT})
        status = credentials.status("ai")
        self.assertEqual(status["source"], "environment")
        self.assertTrue(status["configured"])
        self.assertEqual(status["profiles"]["deepseek"]["model"], "deepseek-flash")
        self.assert_key_absent(status, "fixture-deepseek-key")
        self.assertFalse(self.credentials.path("ai").exists())

    def test_environment_explicit_model_and_other_profiles(self):
        environment = {"AI_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "fixture-deepseek-key",
                       "DEEPSEEK_MODEL": " deepseek-v4-pro ", "OPENAI_API_KEY": "fixture-openai-key",
                       "OPENAI_MODEL": "gpt-fixture", "GEMINI_API_KEY": "fixture-gemini-key",
                       "GEMINI_MODEL": "gemini-fixture"}
        self.assertEqual(environment_ai(environment)["profiles"]["deepseek"]["model"], "deepseek-v4-pro")
        credentials = ConnectionCredentials(self.directory, environment=environment, protector=fake_protector)
        self.assertEqual(credentials.read("ai")["model"], "deepseek-v4-pro")
        credentials.save_ai("openai", "gpt-updated", "")
        status = credentials.status("ai")
        self.assertEqual(status["profiles"]["deepseek"]["model"], "deepseek-v4-pro")
        self.assertTrue(status["profiles"]["gemini"]["has_key"])
        self.assert_key_absent(status, "fixture-deepseek-key", "fixture-openai-key", "fixture-gemini-key")

    def test_empty_environment_remains_unconfigured(self):
        credentials = ConnectionCredentials(self.directory, environment={"AI_PROVIDER": "deepseek"},
                                            protector=fake_protector)
        self.assertIsNone(credentials.read("ai"))
        status = credentials.status("ai")
        self.assertFalse(status["configured"])
        self.assertIsNone(status["error"])
        self.assertFalse(status["profiles"]["deepseek"]["has_key"])

    def test_encrypted_save_read_and_provider_switch_retains_profiles(self):
        self.credentials.save_ai("openai", "gpt-fixture", "fixture-openai-key", "OpenAI fixture prompt")
        self.credentials.save_ai("gemini", "models/gemini-fixture", "fixture-gemini-key", "Gemini fixture prompt")
        self.credentials.save_ai("deepseek", "deepseek-v4-pro", "fixture-deepseek-key", "DeepSeek fixture prompt")
        encrypted = self.credentials.path("ai").read_bytes()
        for key in (b"fixture-openai-key", b"fixture-gemini-key", b"fixture-deepseek-key"):
            self.assertNotIn(key, encrypted)
        document = json.loads(fake_protector(encrypted, True))
        self.assertEqual(document["version"], 1)
        self.assertEqual(set(document["profiles"]), {"openai", "gemini", "deepseek"})
        self.credentials.save_ai("openai", "gpt-updated", "")
        self.assertEqual(self.credentials.read("ai")["api_key"], "fixture-openai-key")
        self.assertEqual(self.credentials.read("ai")["prompt"], "OpenAI fixture prompt")
        self.credentials.save_ai("deepseek", "deepseek-flash", "")
        self.assertEqual(self.credentials.read("ai"), {
            "provider": "deepseek", "api_key": "fixture-deepseek-key", "model": "deepseek-flash",
            "prompt": "DeepSeek fixture prompt"})
        status = self.credentials.status("ai")
        self.assertEqual(status["source"], "saved")
        self.assertEqual(status["profiles"]["gemini"]["model"], "gemini-fixture")
        self.assertEqual(status["profiles"]["gemini"]["prompt"], "Gemini fixture prompt")
        self.assert_key_absent(status, "fixture-openai-key", "fixture-gemini-key", "fixture-deepseek-key")

    def test_existing_version_one_profiles_are_read_without_reset(self):
        existing_document = {"version": 1, "enabled": True, "provider": "gemini", "profiles": {
            "openai": {"api_key": "fixture-openai-key", "model": "gpt-fixture", "prompt": "Saved prompt"},
            "gemini": {"api_key": "fixture-gemini-key", "model": "gemini-fixture"}}}
        self.credentials.path("ai").write_bytes(fake_protector(json.dumps(existing_document).encode()))
        before = self.credentials.path("ai").read_bytes()
        self.assertEqual(self.credentials.read("ai")["provider"], "gemini")
        status = self.credentials.status("ai")
        self.assertEqual(status["profiles"]["openai"]["prompt"], "Saved prompt")
        self.assertFalse(status["profiles"]["deepseek"]["has_key"])
        self.assertEqual(self.credentials.path("ai").read_bytes(), before)
        self.credentials.save_ai("deepseek", "deepseek-flash", "fixture-deepseek-key")
        self.credentials.save_ai("openai", "gpt-fixture", "")
        self.assertEqual(self.credentials.read("ai")["api_key"], "fixture-openai-key")
        self.assertEqual(self.credentials.read("ai")["prompt"], "Saved prompt")

    def test_disable_and_reenable_retains_deepseek_key_model_and_prompt(self):
        self.credentials.save_ai("deepseek", "deepseek-v4-pro", "fixture-deepseek-key", "Saved DeepSeek prompt")
        self.credentials.disable("ai")
        self.assertIsNone(self.credentials.read("ai"))
        status = self.credentials.status("ai")
        self.assertFalse(status["enabled"])
        self.assertEqual(status["profiles"]["deepseek"]["model"], "deepseek-v4-pro")
        self.assertEqual(status["profiles"]["deepseek"]["prompt"], "Saved DeepSeek prompt")
        self.assert_key_absent(status, "fixture-deepseek-key")
        self.credentials.save_ai("deepseek", "deepseek-v4-pro", "")
        self.assertEqual(self.credentials.read("ai")["api_key"], "fixture-deepseek-key")

    def test_invalid_provider_validation_mentions_deepseek(self):
        with self.assertRaisesRegex(DataError, "OpenAI, Google Gemini or DeepSeek"):
            self.credentials.save_ai("unknown", "fixture-model", "fixture-key")
        credentials = ConnectionCredentials(self.directory, environment={"AI_PROVIDER": "unknown"},
                                            protector=fake_protector)
        with self.assertRaisesRegex(DataError, "OpenAI, Google Gemini or DeepSeek"):
            credentials.read("ai")

    def test_connection_template_exposes_deepseek_and_accurate_help(self):
        template = (Path(__file__).resolve().parents[1] / "adaptive_crypto/templates/connection_settings.html").read_text(encoding="utf-8")
        self.assertIn('<option value="deepseek">DeepSeek</option>', template)
        self.assertIn('href="https://api-docs.deepseek.com/quick_start/pricing"', template)
        self.assertIn("native web search", template)
        self.assertIn("Used for Home market commentary.", template)
        self.assertIn("Home researches news and public posts every 15 minutes", template)
        self.assertIn("deepseek-flash", template)
        self.assertIn("deepseek-v4-pro", template)


if __name__ == "__main__":
    unittest.main()
