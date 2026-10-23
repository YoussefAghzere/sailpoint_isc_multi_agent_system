"""Per-user encrypted provider profiles. Jobs pin the chosen profile revision."""
import json
import os
import time
import uuid

from ..models import Rejected, canonical, digest
from .llm import PROVIDERS, ProviderModel, validate_config


class ModelProfiles:
    def __init__(self, db, vault):
        self.db, self.vault = db, vault

    def get(self, profile_id, owner, *, secret=False):
        row = self.db.one("SELECT * FROM model_profiles WHERE id=? AND owner=?", (profile_id, owner))
        if not row:
            raise Rejected("Model profile not found for this user")
        config = json.loads(row["config"])
        result = {"id": row["id"], "name": row["name"], **config, "revision": row["revision"],
                  "has_api_key": bool(row["secret"]), "configured": True}
        if secret:
            result["api_key"] = self.vault.decrypt(row["secret"])["api_key"] if row["secret"] else ""
        return result

    def list(self, owner):
        return [self.get(r["id"], owner) for r in self.db.all("SELECT id FROM model_profiles WHERE owner=? ORDER BY created", (owner,))]

    def save(self, owner, payload, profile_id=None):
        if not isinstance(payload, dict) or set(payload) - {"name", "provider", "model", "base_url", "api_key", "clear_api_key", "max_output_tokens", "max_input_chars", "disable_thinking", "max_planning_turns"}:
            raise Rejected("Unknown model profile fields")
        name = payload.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise Rejected("Give this model profile a name")
        config = validate_config({k: v for k, v in payload.items() if k not in {"name", "api_key", "clear_api_key"}})
        key = payload.get("api_key", "")
        if not isinstance(key, str) or len(key) > 8192 or type(payload.get("clear_api_key", False)) is not bool:
            raise Rejected("API key must be text and clear-key must be a boolean")
        previous = self.get(profile_id, owner, secret=True) if profile_id else None
        # Never forward retained keys to another provider or local server.
        same_endpoint = previous and (config["provider"] != "local" or previous["base_url"] == config["base_url"])
        if not key and not payload.get("clear_api_key") and previous and previous["provider"] == config["provider"] and same_endpoint:
            key = previous["api_key"]
        if payload.get("clear_api_key"):
            key = ""
        if config["provider"] != "local" and not key:
            raise Rejected("Enter this provider's API key, or choose local llama.cpp without a cloud account")
        profile_id, revision, at = profile_id or uuid.uuid4().hex, uuid.uuid4().hex, time.time()
        with self.db.tx():
            existing = self.db.one("SELECT created FROM model_profiles WHERE id=? AND owner=?", (profile_id, owner))
            self.db.execute("INSERT OR REPLACE INTO model_profiles VALUES(?,?,?,?,?,?,?,?)",
                (profile_id, owner, name.strip(), canonical(config), self.vault.encrypt({"api_key": key}) if key else None, revision, existing["created"] if existing else at, at))
            self.db.audit(owner, None, "model_profile.saved", {"id": profile_id, "provider": config["provider"], "model": config["model"], "revision": revision})
        return self.get(profile_id, owner)

    def select(self, owner, profile_id):
        if profile_id:
            self.get(profile_id, owner)
        with self.db.tx():
            self.db.execute("INSERT OR REPLACE INTO model_preferences VALUES(?,?)", (owner, profile_id or ""))
            self.db.audit(owner, None, "model_profile.selected", {"id": profile_id or None})

    def selected_id(self, owner):
        row = self.db.one("SELECT profile_id FROM model_preferences WHERE user_id=?", (owner,))
        return row["profile_id"] if row else ""

    def server_config(self):
        provider, model = os.environ.get("ISC_LLM_PROVIDER", "openai"), os.environ.get("ISC_MODEL")
        if provider not in PROVIDERS or not model:
            return None
        config = {"provider": provider, "model": model}
        if provider == "local":
            config["base_url"] = os.environ.get("ISC_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
            key = os.environ.get("ISC_LLM_API_KEY", "")
        else:
            key = os.environ.get(PROVIDERS[provider]["key_env"], "")
            if not key:
                return None
        return validate_config(config), key

    def snapshot(self, owner, profile_id=None):
        profile_id = self.selected_id(owner) if profile_id is None else profile_id
        if profile_id:
            row = self.get(profile_id, owner)
            return {k: row[k] for k in ("id", "name", "provider", "model", "revision")}
        server = self.server_config()
        if server:
            config, key = server
            return {"id": "_server", "name": "Server default", "provider": config["provider"], "model": config["model"],
                    "revision": digest([config, self.vault.fingerprint(key)])}
        return None

    def model(self, http, owner, snapshot):
        if not snapshot:
            raise Rejected("Choose a model in Models & appearance. Local Qwen or Llama through llama.cpp needs no OpenAI account")
        if snapshot["id"] == "_server":
            current = self.snapshot(owner, "")
            if current != snapshot:
                raise Rejected("Server model configuration changed after this job was queued; create a new plan")
            config, key = self.server_config()
        else:
            row = self.get(snapshot["id"], owner, secret=True)
            if row["revision"] != snapshot["revision"]:
                raise Rejected("Model profile changed after this job was queued; create a new plan")
            config = {k: row[k] for k in ("provider", "model", "base_url", "max_output_tokens", "max_input_chars", "disable_thinking")}
            key = row["api_key"]
        return ProviderModel(http, config, key)

    def delete(self, owner, profile_id):
        self.get(profile_id, owner)
        with self.db.tx():
            self.db.execute("DELETE FROM model_profiles WHERE id=? AND owner=?", (profile_id, owner))
            self.db.execute("DELETE FROM model_preferences WHERE user_id=? AND profile_id=?", (owner, profile_id))
            self.db.audit(owner, None, "model_profile.deleted", {"id": profile_id})
