"""Synthetic tenant contracts and HTTP responses: no live credentials or writes."""
import base64
import copy
import json
import gzip
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from isc_agents.models import Rejected, canonical, digest
from isc_agents.platform.catalog import Catalog
from isc_agents.platform.db import DB
from isc_agents.platform.environments import Environments
from isc_agents.platform.isc import ISC
from isc_agents.platform.jobs import Worker
from isc_agents.platform.network import HTTP, Ambiguous
from isc_agents.platform.research import Research
from isc_agents.platform.security import Vault, public_url, tenant_url
from isc_agents.platform.server import Settings, create_app
from isc_agents.platform.specialists import Orchestrator, ToolRegistry
from isc_agents.platform.workflows import Workflows, bind


def operation(oid, *, parameters=(), body=None, response=None, status="200"):
    result = {"operationId": oid, "summary": oid, "parameters": list(parameters),
              "responses": {status: {"description": "synthetic response", "content": {"application/json": {"schema": response or {}}}}}}
    if body is not None:
        result["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    return result


ID = {"in": "path", "name": "id", "required": True, "schema": {"type": "string"}}
SOURCE = {"type": "object", "required": ["name", "connector", "owner"], "properties": {
    "name": {"type": "string"}, "connector": {"type": "string"}, "owner": {"type": "object"},
    "id": {"type": "string"}, "description": {"type": "string"}, "connectorAttributes": {"type": "object"}}}
SPEC = {"openapi": "3.0.1", "info": {"version": "v2026", "title": "Synthetic contract"},
    "paths": {"/sources": {
        "get": operation("listSources", parameters=[{"in": "query", "name": "limit", "schema": {"type": "integer"}},
            {"in": "query", "name": "filters", "schema": {"type": "string"}}], response={"type": "array", "items": {"$ref": "#/components/schemas/Source"}}),
        "post": operation("createSource", body={"$ref": "#/components/schemas/Source"}, status="201")},
        "/sources/{id}": {"get": operation("getSource", parameters=[ID]),
            "patch": operation("updateSource", parameters=[ID], body={"type": "array", "items": {"type": "object"}}),
            "delete": operation("deleteSource", parameters=[ID])},
        "/task-status/{id}": {"get": operation("getTaskStatus", parameters=[ID])},
        "/sources/{id}/load-accounts": {"post": {**operation("importAccounts", parameters=[ID], status="202"),
            "requestBody": {"content": {"multipart/form-data": {"schema": {"type": "object", "properties": {"file": {"type": "string"}}}}}}}}},
    "components": {"schemas": {"Source": SOURCE}}}


def proposal():
    return {"title": "Create and verify a source", "questions": [], "steps": [
        {"id": "create", "operation_id": "createSource", "arguments": {"body": {"name": "HR", "connector": "test", "owner": {"id": "owner-id"}}}},
        {"id": "verify", "operation_id": "getSource", "arguments": {"path": {"id": "${steps.create.id}"}}}],
        "checks": [{"step_id": "verify", "pointer": "/name", "equals": "HR"}]}


class Tenant:
    def __init__(self):
        self.sources, self.requests, self.mutations = {}, [], 0
        self.timeout_after_write = False
        self.bad_response = False
        self.poll_count = 0

    def __call__(self, request):
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "secret-access-token", "expires_in": 300})
        if request.headers.get("authorization") != "Bearer secret-access-token":
            return httpx.Response(401)
        if path == "/v2026/sources":
            if method == "GET":
                return httpx.Response(200, json=list(self.sources.values()))
            if method == "POST":
                self.mutations += 1
                body = json.loads(request.content)
                self.sources["source1"] = {**body, "id": "source1"}
                if self.timeout_after_write:
                    raise httpx.ReadTimeout("remote outcome unknown", request=request)
                return httpx.Response(201, json="not-an-object" if self.bad_response else self.sources["source1"])
        if path.startswith("/v2026/sources/"):
            source_id = path.rsplit("/", 1)[-1]
            if source_id not in self.sources:
                return httpx.Response(404)
            if method == "PATCH":
                self.mutations += 1
                for edit in json.loads(request.content):
                    self.sources[source_id][edit["path"].lstrip("/")] = edit["value"]
            return httpx.Response(200, json=self.sources[source_id])
        if path.startswith("/v2026/task-status/"):
            self.poll_count += 1
            return httpx.Response(200, json={"status": "SUCCESS" if self.poll_count > 1 else "RUNNING"})
        return httpx.Response(404)


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "platform.db")
        self.vault = Vault(self.tmp.name)
        self.envs = Environments(self.db, self.vault)
        self.catalog = Catalog(self.db)
        self.spec = self.catalog.install(SPEC, "v2026", "https://example.test/synthetic-spec.json")
        self.tenant = Tenant()
        self.http = HTTP(httpx.Client(transport=httpx.MockTransport(self.tenant)))
        self.env = self.live_env()
        self.workflows = Workflows(self.db, self.envs, lambda eid: ISC(self.db, self.envs, self.http, eid))

    def live_env(self, **overrides):
        return self.envs.save({"name": "Acme sandbox", "client": "acme", "stage": "sandbox", "backend": "isc",
            "base_url": "https://acme.api.identitynow.com", "client_id": "synthetic-id", "client_secret": "synthetic-client-secret",
            "spec_id": self.spec["id"], "write_enabled": True,
            "allowed_write_operations": ["createSource", "updateSource", "importAccounts"], **overrides}, "admin")

    def tearDown(self):
        self.http.client.close()
        self.db.close()
        self.tmp.cleanup()

    def approved(self, value=None):
        run = self.workflows.compile(self.env["id"], value or proposal(), "operator")
        return self.workflows.approve(run["id"], self.env["id"], run["hash"], "reviewer")

    def finish(self, run):
        for _ in range(10):
            result = self.workflows.tick(run["id"], self.env["id"])
            if result["done"]:
                return self.workflows.get(run["id"], self.env["id"])
        self.fail("Workflow did not finish within its step budget")

    def test_live_contract_driven_workflow_and_no_duplicate_write(self):
        run = self.approved()
        self.assertEqual(self.finish(run)["status"], "completed")
        self.workflows.tick(run["id"], self.env["id"])
        self.assertEqual(self.tenant.mutations, 1)
        post = next(r for r in self.tenant.requests if r.url.path == "/v2026/sources" and r.method == "POST")
        self.assertEqual(post.url.host, "acme.api.identitynow.com")

    def test_unknown_mutation_is_never_replayed(self):
        run = self.approved()
        self.tenant.timeout_after_write = True
        with self.assertRaises(Ambiguous):
            self.workflows.tick(run["id"], self.env["id"])
        self.assertEqual(self.workflows.get(run["id"], self.env["id"])["status"], "needs_reconciliation")
        with self.assertRaises(Rejected):
            self.workflows.tick(run["id"], self.env["id"])
        self.assertEqual(self.tenant.mutations, 1)

    def test_crashed_sending_checkpoint_requires_reconciliation(self):
        run = self.approved()
        self.db.execute("UPDATE live_steps SET state='sending' WHERE run_id=? AND ordinal=0", (run["id"],))
        with self.assertRaises(Ambiguous):
            self.workflows.tick(run["id"], self.env["id"])
        self.assertEqual(self.tenant.mutations, 0)

    def test_reconciliation_confirms_read_only_evidence(self):
        run = self.approved()
        self.tenant.timeout_after_write = True
        with self.assertRaises(Ambiguous):
            self.workflows.tick(run["id"], self.env["id"])
        run = self.workflows.reconcile(run["id"], self.env["id"], "admin", operation_id="getSource",
            arguments={"path": {"id": "source1"}}, expected=self.tenant.sources["source1"], decision="confirm_applied")
        self.workflows.approve(run["id"], self.env["id"], run["hash"], "reviewer")
        self.assertEqual(self.finish(run)["status"], "completed")
        self.assertEqual(self.tenant.mutations, 1)

    def test_environment_rotation_invalidates_approval(self):
        run = self.approved()
        config = {k: v for k, v in self.env.items() if k not in {"id", "revision", "has_credentials"}}
        self.envs.save(config, "admin", self.env["id"])
        with self.assertRaisesRegex(Rejected, "changed"):
            self.workflows.tick(run["id"], self.env["id"])

    def test_production_requires_second_person(self):
        env = self.live_env(stage="production")
        run = self.workflows.compile(env["id"], proposal(), "admin")
        with self.assertRaisesRegex(Rejected, "different"):
            self.workflows.approve(run["id"], env["id"], run["hash"], "admin")

    def test_write_permissions_and_no_delete(self):
        env = self.live_env(write_enabled=False)
        with self.assertRaisesRegex(Rejected, "permissions"):
            self.workflows.compile(env["id"], proposal(), "admin")
        with self.assertRaisesRegex(Rejected, "nondeprecated"):
            self.live_env(allowed_write_operations=["deleteSource"])

    def test_agents_cannot_mutate_using_read_tools(self):
        isc = ISC(self.db, self.envs, self.http, self.env["id"])
        with self.assertRaisesRegex(Rejected, "approved workflow"):
            isc.call("createSource", proposal()["steps"][0]["arguments"])
        self.assertEqual(self.tenant.mutations, 0)

    def test_credentials_are_encrypted_write_only_and_redacted(self):
        self.assertNotIn("synthetic-client-secret", canonical(self.env))
        stored = self.db.one("SELECT credentials FROM environments WHERE id=?", (self.env["id"],))["credentials"]
        self.assertNotIn("synthetic-client-secret", stored)
        self.tenant.sources["source1"] = {"id": "source1", "name": "HR", "connector": "test", "owner": {},
            "connectorAttributes": {"password": "synthetic-client-secret"}, "note": "secret-access-token"}
        result = ISC(self.db, self.envs, self.http, self.env["id"]).call("getSource", {"path": {"id": "source1"}})
        self.assertNotIn("synthetic-client-secret", canonical(result))
        self.assertNotIn("secret-access-token", canonical(result))

    def test_secret_reference_resolves_only_inside_gateway(self):
        env = self.live_env(connector_secrets={"hr_password": "a-secret-password"})
        value = proposal()
        value["steps"][0]["arguments"]["body"]["connectorAttributes"] = {"password": "${secrets.hr_password}"}
        run = self.workflows.compile(env["id"], value, "operator")
        run = self.workflows.approve(run["id"], env["id"], run["hash"], "reviewer")
        self.workflows.tick(run["id"], env["id"])
        self.assertEqual(self.tenant.sources["source1"]["connectorAttributes"]["password"], "a-secret-password")
        self.assertNotIn("a-secret-password", canonical(self.workflows.get(run["id"], env["id"])))

    def test_inline_secret_and_forward_reference_rejected(self):
        value = proposal()
        value["steps"][0]["arguments"]["body"]["connectorAttributes"] = {"password": "inline-secret"}
        with self.assertRaisesRegex(Rejected, "inline"):
            self.workflows.compile(self.env["id"], value, "operator")
        value = proposal()
        value["steps"][0]["arguments"]["body"]["name"] = "${steps.verify.name}"
        with self.assertRaisesRegex(Rejected, "earlier"):
            self.workflows.compile(self.env["id"], value, "operator")

    def test_api_schema_parameters_and_traversal(self):
        for args in ({"query": {"limit": "wrong"}}, {"query": {"invented": True}}, {"headers": {"Host": "evil"}}):
            with self.subTest(args=args), self.assertRaises(Rejected):
                self.catalog.prepare(self.spec["id"], "listSources", args)
        with self.assertRaises(Rejected):
            self.catalog.prepare(self.spec["id"], "getSource", {"path": {"id": "../oauth"}})

    def test_existing_resource_drift_rejected_before_write(self):
        self.tenant.sources["source1"] = {"id": "source1", "name": "HR", "connector": "test", "owner": {}}
        value = proposal()
        value["steps"][0] = {"id": "update", "operation_id": "updateSource", "arguments": {"path": {"id": "source1"},
            "body": [{"op": "replace", "path": "/description", "value": "Updated"}]}}
        value["steps"][1]["arguments"]["path"]["id"] = "source1"
        run = self.approved(value)
        self.tenant.sources["source1"]["name"] = "Changed externally"
        with self.assertRaisesRegex(Rejected, "drift"):
            self.workflows.tick(run["id"], self.env["id"])
        self.assertEqual(self.tenant.mutations, 0)

    def test_redacted_secret_changes_still_trigger_drift_detection(self):
        self.tenant.sources["source1"] = {"id": "source1", "name": "HR", "connector": "test", "owner": {},
                                           "connectorAttributes": {"password": "original-secret"}}
        value = proposal()
        value["steps"][0] = {"id": "update", "operation_id": "updateSource", "arguments": {"path": {"id": "source1"},
            "body": [{"op": "replace", "path": "/description", "value": "Updated"}]}}
        value["steps"][1]["arguments"]["path"]["id"] = "source1"
        run = self.approved(value)
        self.tenant.sources["source1"]["connectorAttributes"]["password"] = "changed-secret"
        with self.assertRaisesRegex(Rejected, "drift"):
            self.workflows.tick(run["id"], self.env["id"])
        self.assertNotIn("original-secret", canonical(run))

    def test_meaningful_checked_read_required_after_mutation(self):
        value = proposal(); value["steps"] = value["steps"][:1]
        value["checks"] = [{"step_id": "create", "pointer": "/name", "equals": "HR"}]
        with self.assertRaisesRegex(Rejected, "subsequent"):
            self.workflows.compile(self.env["id"], value, "operator")

    def test_async_polling_has_persisted_attempt_budget(self):
        value = {"title": "Poll existing task", "questions": [], "steps": [{"id": "task", "operation_id": "getTaskStatus",
            "arguments": {"path": {"id": "task1"}}, "wait": {"pointer": "/status", "equals": "SUCCESS", "max_attempts": 3, "interval_seconds": 1}}],
            "checks": [{"step_id": "task", "pointer": "/status", "equals": "SUCCESS"}]}
        run = self.approved(value)
        result = self.workflows.tick(run["id"], self.env["id"])
        self.assertFalse(result["done"])
        self.assertEqual(self.workflows.get(run["id"], self.env["id"])["steps"][0]["state"], "polling")
        self.workflows.tick(run["id"], self.env["id"])
        self.assertEqual(self.tenant.poll_count, 1)
        item = self.workflows.get(run["id"], self.env["id"])["steps"][0]
        item["result"]["next_at"] = 0
        self.db.execute("UPDATE live_steps SET result=? WHERE run_id=?", (canonical(item["result"]), run["id"]))
        self.assertEqual(self.finish(run)["status"], "completed")

    def test_spec_import_rejects_remote_refs(self):
        spec = copy.deepcopy(SPEC)
        spec["components"]["schemas"]["Source"]["$ref"] = "http://169.254.169.254/metadata"
        with self.assertRaisesRegex(Rejected, "External"):
            self.catalog.install(spec, "v2026", "local://test")

    def test_tool_activation_is_scoped_and_cannot_escalate(self):
        registry = ToolRegistry(self.db, self.env)
        tool = registry.propose({"name": "Read a source", "description": "Validated wrapper", "operation_id": "getSource"}, "agent")
        self.assertEqual(tool["status"], "proposed")
        with self.assertRaises(Rejected):
            registry.activate(tool["id"], "wrong", "admin")
        self.assertEqual(registry.activate(tool["id"], tool["hash"], "admin")["status"], "active")
        forbidden = registry.propose({"name": "Delete source", "description": "Not allowed", "operation_id": "deleteSource"}, "agent")
        with self.assertRaises(Rejected):
            registry.activate(forbidden["id"], forbidden["hash"], "admin")

    def test_code_drafts_are_not_executed(self):
        registry = ToolRegistry(self.db, self.env)
        draft = registry.draft("connector.py", "raise RuntimeError('must not run')", "agent")
        self.assertFalse(draft["review"]["executed"])
        with self.assertRaises(Rejected):
            registry.draft("bad.py", "this is not valid python!", "agent")

    def test_attachments_encrypted_and_environment_scoped(self):
        text = base64.b64encode(b"id,email\n1,example@example.test\n").decode()
        attachment = self.envs.attach(self.env["id"], "accounts.csv", text, "operator")
        another = self.live_env(name="Other environment")
        with self.assertRaises(Rejected):
            self.envs.file(another["id"], attachment["id"])
        self.assertEqual(self.envs.file(self.env["id"], attachment["id"])[1], base64.b64decode(text))

    def test_csv_quality_checks_report_rows_without_employee_values(self):
        raw = b"id,email\n1,one@example.test\n1,two@example.test\n,three@example.test\n"
        attachment = self.envs.attach(self.env["id"], "accounts.csv", base64.b64encode(raw).decode(), "operator")
        report = self.envs.validate_csv(self.env["id"], attachment["id"], "id", ["email"])
        self.assertFalse(report["valid"])
        self.assertEqual(report["duplicate_id_rows"], [3])
        self.assertEqual(report["empty_id_rows"], [4])
        self.assertNotIn("example.test", canonical(report))

    def test_changed_documentation_invalidates_approved_plan(self):
        research = Research(self.db, self.http)
        research.ingest("client://guide", "HR integration guidance", "HR", "client", scope=self.env["id"])
        evidence = research.search("HR", self.env["id"])
        run = self.workflows.compile(self.env["id"], proposal(), "operator", evidence=evidence)
        self.workflows.approve(run["id"], self.env["id"], run["hash"], "reviewer")
        research.ingest("client://guide", "Changed HR integration guidance", "HR", "client", scope=self.env["id"])
        with self.assertRaisesRegex(Rejected, "documentation changed"):
            self.workflows.tick(run["id"], self.env["id"])
        self.assertEqual(self.tenant.mutations, 0)

    def test_stale_specification_blocks_execution(self):
        run = self.approved()
        self.db.execute("UPDATE specs SET fetched=?", (time.time() - 8 * 86400,))
        with self.assertRaisesRegex(Rejected, "stale"):
            self.workflows.tick(run["id"], self.env["id"])

    def test_deactivated_tool_invalidates_approved_workflow(self):
        registry = ToolRegistry(self.db, self.env)
        tool = registry.propose({"name": "Read source", "description": "GET wrapper", "operation_id": "getSource"}, "agent")
        registry.activate(tool["id"], tool["hash"], "admin")
        value = proposal()
        value["steps"][1]["tool_id"] = tool["id"]
        run = self.approved(value)
        self.db.execute("UPDATE tools SET status='inactive' WHERE id=?", (tool["id"],))
        with self.assertRaisesRegex(Rejected, "deactivated"):
            self.workflows.tick(run["id"], self.env["id"])

    def test_multipart_forms_keep_the_documented_content_type(self):
        requests = []
        def transport(request):
            requests.append(request)
            if request.url.path == "/oauth/token":
                return httpx.Response(200, json={"access_token": "test-token"})
            return httpx.Response(202, json={"task": {"id": "task1"}})
        spec = copy.deepcopy(SPEC)
        spec["paths"]["/sources/{id}/load-accounts"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]["properties"]["disableOptimization"] = {"type": "string"}
        imported = self.catalog.install(spec, "v2026", "local://synthetic-multipart")
        env = self.live_env(spec_id=imported["id"])
        http = HTTP(httpx.Client(transport=httpx.MockTransport(transport)))
        ISC(self.db, self.envs, http, env["id"]).call("importAccounts", {"path": {"id": "source1"}, "form": {"disableOptimization": "true"}}, mutation_authorized=True)
        self.assertIn("multipart/form-data", requests[-1].headers["content-type"])
        self.assertIn(b"disableOptimization", requests[-1].content)
        http.client.close()

    def test_mismatched_success_response_needs_reconciliation(self):
        spec = copy.deepcopy(SPEC)
        spec["paths"]["/sources"]["post"]["responses"]["201"]["content"]["application/json"]["schema"] = {"type": "object"}
        imported = self.catalog.install(spec, "v2026", "local://synthetic-response")
        env = self.live_env(spec_id=imported["id"])
        run = self.workflows.compile(env["id"], proposal(), "operator")
        self.workflows.approve(run["id"], env["id"], run["hash"], "reviewer")
        self.tenant.bad_response = True
        with self.assertRaises(Ambiguous):
            self.workflows.tick(run["id"], env["id"])
        self.assertEqual(self.workflows.get(run["id"], env["id"])["status"], "needs_reconciliation")

    def test_research_keeps_client_guidance_isolated(self):
        research = Research(self.db, self.http)
        research.ingest("client://acme", "HR correlation confidential guidance", "HR rules", "client", scope=self.env["id"])
        self.assertEqual(len(research.search("correlation", self.env["id"])), 1)
        self.assertEqual(research.search("correlation", "another-environment"), [])

    def test_fetch_and_tenant_origins_reject_ssrf_and_credential_urls(self):
        for url in ("http://localhost", "https://127.0.0.1", "https://user:password@documentation.sailpoint.com/a", "https://evil.example"):
            with self.subTest(url=url), self.assertRaises(Rejected):
                public_url(url, {"documentation.sailpoint.com"})
        for url in ("https://evilidentitynow.com", "https://acme.api.identitynow.com/oauth", "http://acme.api.identitynow.com"):
            with self.subTest(url=url), self.assertRaises(Rejected):
                tenant_url(url)

    def test_standard_and_demo_api_origins_are_supported(self):
        for origin in ("https://example.api.identitynow.com", "https://example.api.identitynow-demo.com"):
            with self.subTest(origin=origin):
                self.assertEqual(tenant_url(origin + "/"), origin)
        for host in ("example.identitynow.com", "example.identitynow-demo.com"):
            with self.subTest(host=host), self.assertRaisesRegex(Rejected, "SailPoint web URL.*https://example.api"):
                tenant_url("https://" + host)
        for host in ("example.api.identitynow-demo.com.evil.example", "example.api.identitynow-demo.com@evil.example"):
            with self.subTest(host=host), self.assertRaises(Rejected):
                tenant_url("https://" + host)

    def test_configured_tenant_policy_can_restrict_demo_origins(self):
        with patch.dict("os.environ", {"ISC_TENANT_SUFFIXES": "api.identitynow.com"}):
            with self.assertRaises(Rejected):
                tenant_url("https://example.api.identitynow-demo.com")

    def test_get_retries_but_post_does_not(self):
        count = []
        def transport(request):
            count.append(request.method)
            return httpx.Response(503)
        client = HTTP(httpx.Client(transport=httpx.MockTransport(transport)))
        with patch("time.sleep"):
            client.request("GET", "https://example.test")
            with self.assertRaises(Ambiguous):
                client.request("POST", "https://example.test")
        self.assertEqual(count, ["GET", "GET", "GET", "POST"])
        client.client.close()

    def test_compressed_responses_are_not_decoded_twice(self):
        def transport(request):
            return httpx.Response(200, content=gzip.compress(b'{"ok":true}'), headers={"Content-Encoding": "gzip"})
        client = HTTP(httpx.Client(transport=httpx.MockTransport(transport)))
        self.assertEqual(client.request("GET", "https://example.test").json(), {"ok": True})
        client.client.close()

    def test_bounded_specialist_delegation_is_real_and_shared(self):
        calls = []
        class Model:
            def generate(self, role, context, schema=None):
                calls.append(role)
                if role == "orchestrator" and len(calls) == 1:
                    return {"action": "delegate", "summary": "Review connector evidence", "arguments": json.dumps({"template": "researcher", "task": "Find prerequisites"})}
                if role == "researcher":
                    return {"action": "finish", "summary": "Research complete", "arguments": json.dumps({"answer": "Need owner identity", "citations": []})}
                return {"action": "ask", "summary": "Missing owner", "arguments": json.dumps({"questions": ["Who owns this source?"]})}
        agent = Orchestrator(Model(), Research(self.db, self.http), self.catalog,
            ISC(self.db, self.envs, self.http, self.env["id"]), self.env, ToolRegistry(self.db, self.env))
        output = agent.run("Integrate HR")
        self.assertEqual(calls, ["orchestrator", "researcher", "orchestrator"])
        self.assertEqual(output["budget"]["agents"], 2)
        self.assertTrue(output["result"]["questions"])

    def test_model_planning_job_builds_a_reviewed_plan_without_writes(self):
        Research(self.db, self.http).ingest("https://developer.sailpoint.com/synthetic-test-evidence", "Create source HR prerequisites", "Synthetic official-labelled test document", "official")
        requests = []
        class Model:
            def generate(self, role, context, schema=None):
                requests.append((role, context))
                if role.startswith("Independent"):
                    return {"approved": True, "findings": ["Synthetic review"]}
                return {"action": "finish", "summary": "Compile validated steps", "arguments": json.dumps(proposal())}
        job = self.db.enqueue("plan", self.env["id"], {"prompt": "Create HR source", "actor": "operator"})
        worker = Worker(self.db, self.envs, self.http, model=Model())
        worker.run_one()
        row = self.db.one("SELECT * FROM jobs WHERE id=?", (job,))
        self.assertEqual(row["state"], "completed", row["error"])
        run = self.workflows.get(json.loads(row["result"])["run_id"], self.env["id"])
        self.assertEqual(run["status"], "planned")
        self.assertEqual(requests[-1][1]["request"], "Create HR source")
        self.assertEqual(self.tenant.mutations, 0)

    def test_specialist_cannot_register_tools_or_expand_permissions(self):
        agent = Orchestrator(None, Research(self.db, self.http), self.catalog,
            ISC(self.db, self.envs, self.http, self.env["id"]), self.env, ToolRegistry(self.db, self.env))
        with self.assertRaisesRegex(Rejected, "read-only"):
            agent._tool("propose_tool", {"operation_id": "deleteSource"}, "researcher", 1)
        with self.assertRaisesRegex(Rejected, "template"):
            agent._tool("delegate", {"template": "super_admin", "task": "Grant permissions"}, "orchestrator", 0)
        with self.assertRaisesRegex(Rejected, "disabled"):
            agent._tool("research", {"kind": "web", "query": "private client context"}, "researcher", 1)

    def test_crawler_follows_only_official_section_and_checkpoints(self):
        def transport(request):
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /\n")
            if request.url.path.endswith("index.html"):
                return httpx.Response(200, headers={"content-type": "text/html", "etag": "one"}, text='<title>Sources</title><main>HR integration<a href="next.html">Next</a><a href="https://evil.example/">bad</a></main>')
            return httpx.Response(200, headers={"content-type": "text/html"}, text='<title>Correlation</title><main>Account correlation guidance</main>')
        http = HTTP(httpx.Client(transport=httpx.MockTransport(transport)))
        research = Research(self.db, http)
        with patch("time.sleep"):
            state = research.crawl_page({"seeds": ["https://documentation.sailpoint.com/saas/help/index.html"], "max_pages": 2})
            self.assertEqual(len(state["visited"]), 1)
            self.assertEqual(state["queue"], ["https://documentation.sailpoint.com/saas/help/next.html"])
            state = research.crawl_page(state)
        self.assertTrue(state["done"])
        self.assertTrue(research.search("correlation", self.env["id"]))
        http.client.close()


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(directory=self.tmp.name, worker=False, auto_ingestion=False,
                                 bootstrap_password="synthetic-admin-password", allowed_hosts=("testserver",))
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        login = self.client.post("/api/login", json={"username": "admin", "password": "synthetic-admin-password"})
        self.headers = {"X-CSRF-Token": login.json()["csrf"]}

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_authentication_csrf_and_security_headers(self):
        self.assertEqual(TestClient(self.app).get("/api/environments").status_code, 401)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(self.client.post("/api/environments", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/environments", headers={**self.headers, "Origin": "https://evil.example"}, json={}).status_code, 400)

    def test_web_environment_plan_approve_execute(self):
        e = self.client.post("/api/environments", headers=self.headers, json={"name": "Demo", "client": "demo", "stage": "sandbox", "backend": "simulator"}).json()
        spec = json.loads((Path(__file__).resolve().parents[1] / "examples/hr_source.json").read_text())
        p = self.client.post(f"/api/environments/{e['id']}/plan", headers=self.headers, json={"prompt": "Simulate HR", "requirements": spec})
        self.assertEqual(p.status_code, 200)
        from isc_agents.platform.server import Context
        ctx = Context(self.settings)
        worker = Worker(ctx.db, ctx.envs, ctx.http)
        self.assertTrue(worker.run_one())
        runs = self.client.get(f"/api/environments/{e['id']}/runs").json()
        run = runs[0]
        a = self.client.post(f"/api/environments/{e['id']}/runs/{run['id']}/approve", headers=self.headers, json={"hash": run["hash"]})
        self.assertEqual(a.status_code, 200, a.text)
        x = self.client.post(f"/api/environments/{e['id']}/runs/{run['id']}/execute", headers=self.headers, json={})
        self.assertEqual(x.status_code, 200, x.text)
        worker.run_one()
        self.assertEqual(self.client.get(f"/api/environments/{e['id']}/runs/{run['id']}").json()["status"], "completed")
        ctx.close()

    def test_demo_environment_save_errors_are_actionable_and_corrected_values_persist(self):
        from isc_agents.platform.server import Context
        ctx = Context(self.settings)
        try:
            imported = ctx.catalog.install(SPEC, "v2026", "local://synthetic-demo-contract")
        finally:
            ctx.close()
        payload = {"name": "Magellan Lab", "client": "Magellan Lab", "stage": "sandbox", "backend": "isc",
                   "base_url": "https://example.identitynow-demo.com", "client_id": "synthetic-client-id",
                   "client_secret": "synthetic-client-secret", "spec_id": imported["id"]}
        invalid = self.client.post("/api/environments", headers=self.headers, json=payload)
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("no spaces", invalid.json()["error"])
        self.assertEqual(self.client.get("/api/environments").json(), [])
        payload["client"] = "magellan_lab"
        invalid = self.client.post("/api/environments", headers=self.headers, json=payload)
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("https://example.api.identitynow-demo.com", invalid.json()["error"])
        payload["base_url"] = "https://example.api.identitynow-demo.com"
        saved = self.client.post("/api/environments", headers=self.headers, json=payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        rows = self.client.get("/api/environments").json()
        self.assertEqual(rows[0]["id"], saved.json()["id"])
        self.assertEqual(rows[0]["base_url"], payload["base_url"])
        self.assertNotIn("synthetic-client-secret", canonical(rows))

    def test_user_cannot_access_ungranted_environment(self):
        e = self.client.post("/api/environments", headers=self.headers, json={"name": "Private", "client": "private", "stage": "sandbox", "backend": "simulator"}).json()
        r = self.client.post("/api/users", headers=self.headers, json={"username": "operator", "password": "synthetic-operator-password", "role": "operator", "environments": []})
        self.assertEqual(r.status_code, 200)
        other = TestClient(self.app)
        login = other.post("/api/login", json={"username": "operator", "password": "synthetic-operator-password"})
        self.assertEqual(other.get("/api/environments").json(), [])
        self.assertEqual(other.get(f"/api/environments/{e['id']}/runs").status_code, 400)
        csrf = {"X-CSRF-Token": login.json()["csrf"]}
        self.assertEqual(other.post("/api/specs/sync", headers=csrf, json={}).status_code, 400)
        other.close()


if __name__ == "__main__":
    unittest.main()
