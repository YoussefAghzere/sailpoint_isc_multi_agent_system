"""Provider protocols and per-user model settings. No paid API or model download."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from isc_agents.models import Rejected, canonical
from isc_agents.platform.db import DB
from isc_agents.platform.llm import ProviderModel, local_base_url
from isc_agents.platform.model_profiles import ModelProfiles
from isc_agents.platform.network import HTTP
from isc_agents.platform.security import Vault
from isc_agents.platform.server import Context, Settings, create_app
from isc_agents.platform.jobs import Worker
from test_studio import SPEC


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


def reply(provider, value, *, incomplete=False):
    if provider == "openai":
        return {"status": "incomplete" if incomplete else "completed", "output": [{"content": [{"type": "output_text", "text": json.dumps(value)}]}]}
    if provider == "anthropic":
        return {"stop_reason": "max_tokens" if incomplete else "tool_use", "content": [{"type": "tool_use", "name": "structured_result", "input": value}]}
    if provider == "gemini":
        return {"candidates": [{"finishReason": "MAX_TOKENS" if incomplete else "STOP", "content": {"parts": [{"text": json.dumps(value)}]}}]}
    return {"choices": [{"finish_reason": "length" if incomplete else "stop", "message": {"content": json.dumps(value)}}]}


class AdapterTests(unittest.TestCase):
    def call(self, provider, value=None, *, incomplete=False, key="synthetic-model-key"):
        requests = []
        def transport(request):
            requests.append(request)
            return httpx.Response(200, json=reply(provider, {"ok": True} if value is None else value, incomplete=incomplete))
        client = httpx.Client(transport=httpx.MockTransport(transport))
        self.addCleanup(client.close)
        model = ProviderModel(HTTP(client), {"provider": provider, "model": "test-model"}, key)
        result = model.generate("Connection test", {"task": "Return ok=true"}, SCHEMA)
        return result, requests[0]

    def test_all_provider_protocols_return_the_same_validated_contract(self):
        for provider in ("openai", "anthropic", "gemini", "local"):
            with self.subTest(provider=provider):
                result, request = self.call(provider)
                self.assertEqual(result, {"ok": True})
                body = json.loads(request.content)
                if provider == "openai":
                    self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
                    self.assertFalse(body["store"])
                    self.assertTrue(body["text"]["format"]["strict"])
                    self.assertEqual(request.headers["authorization"], "Bearer synthetic-model-key")
                elif provider == "anthropic":
                    self.assertEqual(str(request.url), "https://api.anthropic.com/v1/messages")
                    self.assertEqual(request.headers["x-api-key"], "synthetic-model-key")
                    self.assertEqual(body["tool_choice"]["name"], "structured_result")
                    self.assertEqual(body["tools"][0]["input_schema"], SCHEMA)
                elif provider == "gemini":
                    self.assertEqual(request.url.host, "generativelanguage.googleapis.com")
                    self.assertEqual(request.headers["x-goog-api-key"], "synthetic-model-key")
                    self.assertNotIn("synthetic-model-key", str(request.url))
                    self.assertEqual(body["generationConfig"]["responseJsonSchema"], SCHEMA)
                else:
                    self.assertEqual(str(request.url), "http://127.0.0.1:8080/v1/chat/completions")
                    self.assertEqual(body["response_format"]["schema"], SCHEMA)
                    self.assertFalse(body["stream"])

    @patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-cloud-key-never-send-local"})
    def test_keyless_local_inference_does_not_read_or_forward_openai_credentials(self):
        result, request = self.call("local", key="")
        self.assertEqual(result, {"ok": True})
        self.assertNotIn("authorization", request.headers)
        self.assertNotIn("synthetic-cloud-key-never-send-local", str(request.headers))
        self.assertFalse(json.loads(request.content)["chat_template_kwargs"]["enable_thinking"])

    def test_incomplete_and_schema_invalid_outputs_fail_for_every_provider(self):
        for provider in ("openai", "anthropic", "gemini", "local"):
            with self.subTest(provider=provider), self.assertRaises(Rejected):
                self.call(provider, incomplete=True)
            with self.subTest(provider=provider), self.assertRaises(Rejected):
                self.call(provider, {"invented": "not the schema"})

    def test_missing_cloud_keys_do_not_prevent_local_profiles(self):
        for provider in ("openai", "anthropic", "gemini"):
            with self.subTest(provider=provider), self.assertRaises(Rejected):
                ProviderModel(None, {"provider": provider, "model": "test"})
        self.assertEqual(ProviderModel(None, {"provider": "local", "model": "qwen"}).model, "qwen")

    def test_context_limit_prevents_network_calls(self):
        model = ProviderModel(None, {"provider": "local", "model": "qwen", "max_input_chars": 16000})
        with self.assertRaisesRegex(Rejected, "context budget"):
            model.generate("test", {"text": "x" * 20000}, SCHEMA)

    def test_local_server_errors_are_actionable_without_cloud_fallback(self):
        requests = []
        def transport(request):
            requests.append(request)
            raise httpx.ConnectError("not running", request=request)
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            model = ProviderModel(HTTP(client), {"provider": "local", "model": "qwen"})
            with self.assertRaisesRegex(Rejected, "Launch llama-server"):
                model.generate("test", {}, SCHEMA)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "127.0.0.1")

    def test_provider_error_does_not_echo_secret_response_bodies(self):
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="synthetic-sensitive-provider-key"))) as client:
            model = ProviderModel(HTTP(client), {"provider": "gemini", "model": "test"}, "synthetic-sensitive-provider-key")
            with self.assertRaises(Rejected) as caught:
                model.generate("test", {}, SCHEMA)
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertNotIn("synthetic-sensitive-provider-key", str(caught.exception))

    def test_local_url_policy_prevents_arbitrary_internal_fetches(self):
        for url in ("http://127.0.0.1:8080", "http://localhost:8080/v1/", "http://[::1]:8080/v1"):
            self.assertTrue(local_base_url(url).endswith("/v1"))
        for url in ("file:///etc/passwd", "http://169.254.169.254", "http://example.test:8080/v1", "http://localhost:8080/admin", "http://user:pass@localhost:8080", "http://localhost:8080/v1?key=secret"):
            with self.subTest(url=url), self.assertRaises(Rejected):
                local_base_url(url)
        with patch.dict(os.environ, {"ISC_LLM_LOCAL_ORIGINS": "https://llm.example.test"}):
            self.assertEqual(local_base_url("https://llm.example.test/v1"), "https://llm.example.test/v1")


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "platform.db")
        self.profiles = ModelProfiles(self.db, Vault(self.tmp.name))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def local(self, owner="alice"):
        return self.profiles.save(owner, {"name": "My Qwen", "provider": "local", "model": "qwen", "base_url": "http://127.0.0.1:8080/v1"})

    @patch.dict(os.environ, {}, clear=True)
    def test_local_model_selection_works_without_any_cloud_keys(self):
        profile = self.local()
        self.profiles.select("alice", profile["id"])
        snapshot = self.profiles.snapshot("alice")
        self.assertEqual(snapshot["provider"], "local")
        self.assertFalse(profile["has_api_key"])
        self.assertEqual(self.profiles.model(None, "alice", snapshot).provider, "local")

    def test_profiles_and_selections_are_private_to_the_owner(self):
        profile = self.local()
        self.assertEqual(self.profiles.list("bob"), [])
        for call in (lambda: self.profiles.get(profile["id"], "bob"), lambda: self.profiles.select("bob", profile["id"]), lambda: self.profiles.delete("bob", profile["id"])):
            with self.assertRaises(Rejected):
                call()

    def test_provider_keys_are_encrypted_write_only_and_retained_on_edit(self):
        profile = self.profiles.save("alice", {"name": "Claude", "provider": "anthropic", "model": "test", "api_key": "synthetic-claude-secret"})
        self.assertNotIn("synthetic-claude-secret", canonical(profile))
        self.assertNotIn("synthetic-claude-secret", self.db.one("SELECT secret FROM model_profiles")["secret"])
        self.profiles.save("alice", {"name": "Renamed", "provider": "anthropic", "model": "test", "api_key": ""}, profile["id"])
        self.assertEqual(self.profiles.get(profile["id"], "alice", secret=True)["api_key"], "synthetic-claude-secret")

    def test_switching_from_cloud_to_local_does_not_retain_a_cloud_key(self):
        profile = self.profiles.save("alice", {"name": "Cloud", "provider": "openai", "model": "test", "api_key": "synthetic-private-cloud-key"})
        local = self.profiles.save("alice", {"name": "Local", "provider": "local", "model": "qwen"}, profile["id"])
        self.assertFalse(local["has_api_key"])
        self.assertEqual(self.profiles.get(local["id"], "alice", secret=True)["api_key"], "")

    def test_changing_local_server_does_not_forward_its_old_authentication_key(self):
        profile = self.profiles.save("alice", {"name": "Local secured", "provider": "local", "model": "qwen", "api_key": "synthetic-local-key"})
        edited = self.profiles.save("alice", {"name": "Other server", "provider": "local", "model": "qwen", "base_url": "http://127.0.0.1:9090/v1"}, profile["id"])
        self.assertFalse(edited["has_api_key"])

    def test_editing_a_queued_profile_invalidates_its_snapshot(self):
        profile = self.local()
        before = self.profiles.snapshot("alice", profile["id"])
        self.profiles.save("alice", {"name": "Other local", "provider": "local", "model": "llama"}, profile["id"])
        with self.assertRaisesRegex(Rejected, "changed after"):
            self.profiles.model(None, "alice", before)

    def test_selecting_another_profile_does_not_retarget_queued_jobs(self):
        first, second = self.local(), self.profiles.save("alice", {"name": "Llama", "provider": "local", "model": "llama"})
        self.profiles.select("alice", first["id"])
        snapshot = self.profiles.snapshot("alice")
        self.profiles.select("alice", second["id"])
        self.assertEqual(self.profiles.model(None, "alice", snapshot).model, "qwen")

    @patch.dict(os.environ, {"ISC_LLM_PROVIDER": "local", "ISC_MODEL": "qwen"}, clear=True)
    def test_environment_defaults_support_keyless_local_models(self):
        snapshot = self.profiles.snapshot("alice")
        self.assertEqual(snapshot["provider"], "local")
        self.assertEqual(self.profiles.model(None, "alice", snapshot).model, "qwen")


class ModelAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(directory=self.tmp.name, worker=False, auto_ingestion=False,
            bootstrap_password="synthetic-admin-password", allowed_hosts=("testserver",))
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        response = self.client.post("/api/login", json={"username": "admin", "password": "synthetic-admin-password"})
        self.headers = {"X-CSRF-Token": response.json()["csrf"]}

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_save_select_and_read_local_profile_from_ui_api(self):
        saved = self.client.post("/api/model-profiles", headers=self.headers, json={"name": "Local Qwen", "provider": "local", "model": "qwen"})
        self.assertEqual(saved.status_code, 200, saved.text)
        profile = saved.json()
        result = self.client.post("/api/model-selection", headers=self.headers, json={"profile_id": profile["id"]})
        self.assertEqual(result.json()["selected_model"]["provider"], "local")
        self.assertTrue(self.client.get("/api/me").json()["model_configured"])
        self.assertEqual(self.client.get("/api/model-profiles").json()["selected_id"], profile["id"])
        self.assertEqual(self.client.post("/api/model-profiles", json={}).status_code, 400)

    def test_ui_connection_test_uses_only_the_selected_local_server(self):
        saved = self.client.post("/api/model-profiles", headers=self.headers, json={"name": "Local Qwen", "provider": "local", "model": "qwen"}).json()
        requests = []
        def transport(request):
            requests.append(request)
            return httpx.Response(200, json=reply("local", {"ok": True}))
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            with patch("isc_agents.platform.server.HTTP", return_value=HTTP(client)):
                result = self.client.post(f"/api/model-profiles/{saved['id']}/test", headers=self.headers, json={})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["ok"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "127.0.0.1")

    def test_planning_job_can_use_a_real_local_adapter_without_openai(self):
        saved = self.client.post("/api/model-profiles", headers=self.headers, json={"name": "Local Qwen", "provider": "local", "model": "qwen"}).json()
        self.client.post("/api/model-selection", headers=self.headers, json={"profile_id": saved["id"]})
        ctx = Context(self.settings)
        try:
            spec = ctx.catalog.install(SPEC, "v2026", "local://synthetic-spec")
        finally:
            ctx.close()
        environment = self.client.post("/api/environments", headers=self.headers, json={"name": "Sandbox", "client": "acme", "stage": "sandbox", "backend": "isc", "base_url": "https://acme.api.identitynow.com", "client_id": "synthetic-client", "client_secret": "synthetic-tenant-secret", "spec_id": spec["id"]}).json()
        response = self.client.post(f"/api/environments/{environment['id']}/plan", headers=self.headers, json={"prompt": "Integrate HR; ask for missing ownership"})
        self.assertEqual(response.status_code, 200, response.text)
        requests = []
        action = {"action": "ask", "summary": "Ownership is missing", "arguments": json.dumps({"questions": ["Who owns the source?"]})}
        def transport(request):
            requests.append(request)
            return httpx.Response(200, json=reply("local", action))
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            ctx = Context(self.settings, HTTP(client))
            try:
                with patch.dict(os.environ, {}, clear=True):
                    Worker(ctx.db, ctx.envs, ctx.http).run_one()
                job = ctx.db.one("SELECT * FROM jobs WHERE id=?", (response.json()["job_id"],))
                self.assertEqual(job["state"], "completed", job["error"])
                self.assertEqual(json.loads(job["result"])["status"], "needs_input")
                self.assertNotIn("synthetic-tenant-secret", requests[0].content.decode())
                self.assertEqual(requests[0].url.host, "127.0.0.1")
                self.assertEqual(len(requests), 1)
            finally:
                ctx.close()

    def test_settings_profiles_are_not_accessible_to_other_users(self):
        profile = self.client.post("/api/model-profiles", headers=self.headers, json={"name": "Private", "provider": "local", "model": "qwen"}).json()
        self.client.post("/api/users", headers=self.headers, json={"username": "other", "password": "synthetic-other-password", "role": "operator", "environments": []})
        with TestClient(self.app) as other:
            login = other.post("/api/login", json={"username": "other", "password": "synthetic-other-password"}).json()
            headers = {"X-CSRF-Token": login["csrf"]}
            self.assertEqual(other.get("/api/model-profiles").json()["profiles"], [])
            self.assertEqual(other.post("/api/model-selection", headers=headers, json={"profile_id": profile["id"]}).status_code, 400)
            self.assertEqual(other.post(f"/api/model-profiles/{profile['id']}/test", headers=headers, json={}).status_code, 400)

    def test_database_upgrade_retains_existing_environments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "platform.db"
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE environments(id TEXT PRIMARY KEY,config TEXT,credentials TEXT,revision TEXT)")
            old.execute("INSERT INTO environments VALUES(?,?,?,?)", ("existing-env", canonical({"name": "Existing tenant", "client": "acme", "backend": "simulator", "stage": "sandbox"}), None, "existing-revision"))
            old.commit(); old.close()
            app = create_app(Settings(directory=directory,worker=False,auto_ingestion=False,bootstrap_password="synthetic-admin-password",allowed_hosts=("testserver",)))
            with TestClient(app) as client:
                client.post("/api/login",json={"username":"admin","password":"synthetic-admin-password"})
                row = client.get("/api/environments").json()[0]
                self.assertEqual(row["id"], "existing-env")
                self.assertEqual(row["revision"], "existing-revision")


if __name__ == "__main__":
    unittest.main()
