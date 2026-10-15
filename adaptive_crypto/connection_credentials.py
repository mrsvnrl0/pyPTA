"""Encrypted local AI and Telegram configuration, with environment compatibility."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from .core import DataError
from .deribit_settings import protect_credentials
from .ai_prompts import DEFAULT_MARKET_PROMPT, effective_prompt, validate_prompt

PROVIDERS = {"openai": "OpenAI", "gemini": "Google Gemini", "deepseek": "DeepSeek"}
DEFAULT_MODELS = {"deepseek": "deepseek-flash"}


def secret(value, label, *, required=True):
    if not isinstance(value, str):
        raise DataError(f"Enter {label} as text.")
    value = value.strip()
    if (required and not value) or len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise DataError(f"Enter a valid {label} without spaces.")
    return value


def model_id(value, provider):
    if not isinstance(value, str):
        raise DataError("Enter a model ID.")
    value = value.strip()
    if provider == "gemini":
        value = value.removeprefix("models/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", value):
        raise DataError("Enter a valid text model ID from your provider account.")
    return value


def validate_telegram(token, chat):
    token, chat = secret(token, "Telegram bot token"), secret(chat, "Telegram chat ID")
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?", token):
        raise DataError("Enter a valid Telegram bot token.")
    if not re.fullmatch(r"-?[0-9]+|@[A-Za-z0-9_]{5,}", chat):
        raise DataError("Enter a numeric Telegram chat ID or an @channel username.")
    return token, chat


def environment_ai(environ):
    provider = (environ.get("AI_PROVIDER") or "openai").strip().lower()
    profiles = {}
    for name, prefix in (("openai", "OPENAI"), ("gemini", "GEMINI"), ("deepseek", "DEEPSEEK")):
        key = (environ.get(prefix+"_API_KEY") or "").strip()
        model = (environ.get(prefix+"_MODEL") or "").strip()
        profiles[name] = {"api_key": key, "model": model or (DEFAULT_MODELS.get(name, "") if key else "")}
    return {"enabled": True, "provider": provider, "profiles": profiles}


class ConnectionCredentials:
    def __init__(self, directory, *, environment=None, protector=None):
        self.directory = Path(directory)
        self.environment = os.environ if environment is None else environment
        self.protector = protector or protect_credentials
        self.lock = threading.RLock()

    def path(self, kind):
        if kind not in {"ai", "telegram"}:
            raise DataError("Unknown connection.")
        return self.directory/(kind+"-credentials.dat")

    def _load(self, kind):
        try:
            with self.path(kind).open("rb") as stream:
                encrypted = stream.read(65537)
        except FileNotFoundError:
            if kind == "ai":
                return environment_ai(self.environment), "environment"
            token = (self.environment.get("TELEGRAM_BOT_TOKEN") or "").strip()
            chat = (self.environment.get("TELEGRAM_CHAT_ID") or "").strip()
            source = "environment"
            if not token and not chat:
                from .project_keys import read_project_key
                token = read_project_key(self.directory, "TELEGRAM_BOT_TOKEN.md")
                chat = read_project_key(self.directory, "TELEGRAM_CHAT_ID.md")
                source = "project keys" if token or chat else "environment"
            return {"enabled": True, "bot_token": token, "chat_id": chat}, source
        except OSError:
            raise DataError("Saved connection cannot be read. Check access to the settings folder.") from None
        try:
            if not encrypted or len(encrypted) > 65536:
                raise ValueError()
            doc = json.loads(self.protector(encrypted, True))
            if type(doc.get("version")) is not int or doc["version"] != 1 or type(doc.get("enabled")) is not bool:
                raise ValueError()
            if kind == "ai":
                if set(doc) != {"version", "enabled", "provider", "profiles"} or doc["provider"] not in PROVIDERS:
                    raise ValueError()
                if not isinstance(doc["profiles"], dict) or set(doc["profiles"])-set(PROVIDERS):
                    raise ValueError()
                for provider, profile in doc["profiles"].items():
                    if not isinstance(profile, dict) or not {"api_key", "model"}.issubset(profile) or set(profile)-{"api_key", "model", "prompt"}:
                        raise ValueError()
                    secret(profile["api_key"], "API key")
                    model_id(profile["model"], provider)
                    if "prompt" in profile:
                        validate_prompt(profile["prompt"])
            else:
                if set(doc) != {"version", "enabled", "bot_token", "chat_id"}:
                    raise ValueError()
                if doc["enabled"] or doc["bot_token"] or doc["chat_id"]:
                    validate_telegram(doc["bot_token"], doc["chat_id"])
            doc.pop("version")
            return doc, "saved"
        except Exception:
            raise DataError("Saved connection cannot be unlocked. Re-enter its complete credentials to replace it.") from None

    def read(self, kind):
        with self.lock:
            doc, _ = self._load(kind)
            if not doc["enabled"]:
                return None
            if kind == "ai":
                provider = doc["provider"]
                if provider not in PROVIDERS:
                    raise DataError("Choose OpenAI, Google Gemini or DeepSeek as the AI provider.")
                profile = doc["profiles"].get(provider, {})
                key, model = profile.get("api_key", ""), profile.get("model", "")
                if not key and not model:
                    return None
                return {"provider": provider, "api_key": secret(key, "AI API key"), "model": model_id(model, provider),
                        "prompt": effective_prompt(profile.get("prompt"))}
            token, chat = doc["bot_token"], doc["chat_id"]
            if not token and not chat:
                return None
            token, chat = validate_telegram(token, chat)
            return {"bot_token": token, "chat_id": chat}

    def status(self, kind):
        with self.lock:
            try:
                doc, source = self._load(kind)
                error = None
                try:
                    configured = self.read(kind) is not None
                except DataError as exc:
                    configured, error = False, str(exc)
                result = {"configured": configured, "enabled": doc["enabled"], "source": source, "error": error}
                if kind == "ai":
                    result.update(provider=doc["provider"] if doc["provider"] in PROVIDERS else "openai",
                                  default_prompt=DEFAULT_MARKET_PROMPT,
                                  profiles={p: {"has_key": bool(doc["profiles"].get(p, {}).get("api_key")),
                                                "model": doc["profiles"].get(p, {}).get("model", ""),
                                                "prompt": effective_prompt(doc["profiles"].get(p, {}).get("prompt"))}
                                            for p in PROVIDERS})
                else:
                    result.update(has_token=bool(doc["bot_token"]), has_chat=bool(doc["chat_id"]))
                return result
            except DataError as exc:
                return {"configured": False, "enabled": False, "source": "error", "error": str(exc),
                        **({"default_prompt": DEFAULT_MARKET_PROMPT} if kind == "ai" else {})}

    def _write(self, kind, doc):
        temporary = None
        try:
            plaintext = json.dumps({"version": 1, **doc}, allow_nan=False, ensure_ascii=False).encode("utf-8")
            if len(plaintext) > 65536:
                raise ValueError("Connection configuration is too large")
            encrypted = self.protector(plaintext)
            if not encrypted or len(encrypted) > 65536:
                raise ValueError("Encrypted connection configuration is too large")
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix=".connection-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path(kind))
        except Exception:
            raise DataError("Connection could not be saved securely. Check access to the settings folder and Windows credential protection.") from None
        finally:
            if temporary:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def save_ai(self, provider, model, api_key="", prompt=None):
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise DataError("Choose OpenAI, Google Gemini or DeepSeek as the AI provider.")
        model, api_key = model_id(model, provider), secret(api_key, "AI API key", required=False)
        if prompt is not None:
            prompt = validate_prompt(prompt)
        with self.lock:
            try:
                doc, _ = self._load("ai")
            except DataError:
                if not api_key:
                    raise
                doc = {"profiles": {}}
            profiles = copy.deepcopy(doc["profiles"])
            key = api_key or profiles.get(provider, {}).get("api_key", "")
            key = secret(key, "AI API key")
            # Do not persist incomplete environment profiles for other providers.
            profiles = {p: v for p, v in profiles.items() if p in PROVIDERS and v.get("api_key") and v.get("model")}
            for p, profile in profiles.items():
                profile["model"] = model_id(profile["model"], p)
                profile["api_key"] = secret(profile["api_key"], "AI API key")
            previous_prompt = profiles.get(provider, {}).get("prompt")
            profiles[provider] = {"api_key": key, "model": model}
            chosen_prompt = previous_prompt if prompt is None else prompt
            if chosen_prompt:
                profiles[provider]["prompt"] = validate_prompt(chosen_prompt)
            self._write("ai", {"enabled": True, "provider": provider, "profiles": profiles})

    def save_telegram(self, bot_token="", chat_id=""):
        token, chat = secret(bot_token, "bot token", required=False), secret(chat_id, "chat ID", required=False)
        with self.lock:
            if not token or not chat:
                previous, _ = self._load("telegram")
                token, chat = token or previous["bot_token"], chat or previous["chat_id"]
            token, chat = validate_telegram(token, chat)
            self._write("telegram", {"enabled": True, "bot_token": token, "chat_id": chat})

    def disable(self, kind):
        with self.lock:
            # An explicit off marker also disables environment fallback after restart.
            empty = ({"enabled": False, "provider": "openai", "profiles": {}} if kind == "ai" else
                     {"enabled": False, "bot_token": "", "chat_id": ""})
            try:
                doc, _ = self._load(kind)
                if kind == "ai":
                    if doc["provider"] not in PROVIDERS:
                        doc["provider"] = "openai"
                    profiles = {}
                    for provider, profile in doc["profiles"].items():
                        if profile.get("api_key") and profile.get("model"):
                            profiles[provider] = {"api_key": secret(profile["api_key"], "API key"),
                                                  "model": model_id(profile["model"], provider)}
                            if profile.get("prompt"):
                                profiles[provider]["prompt"] = validate_prompt(profile["prompt"])
                    doc["profiles"] = profiles
                elif doc["bot_token"] or doc["chat_id"]:
                    validate_telegram(doc["bot_token"], doc["chat_id"])
            except DataError:
                doc = empty
            self._write(kind, {**doc, "enabled": False})
