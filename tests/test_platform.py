import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from isc_agents.engine import Engine
from isc_agents.knowledge import Knowledge
from isc_agents.models import Rejected, canonical, check_spec
from isc_agents.store import Store


ROOT = Path(__file__).resolve().parents[1]
SCOPE = {"client": "acme", "tenant": "acme_sandbox", "environment": "sandbox"}


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dbpath = str(Path(self.temp.name) / "platform.db")
        self.store = Store(self.dbpath)
        self.engine = Engine(self.store, Knowledge(ROOT / "knowledge"))
        self.spec = json.loads((ROOT / "examples/hr_source.json").read_text())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def plan(self):
        return self.engine.plan(SCOPE, "Simulate a delimited-file HR source", self.spec)

    def approved(self):
        run = self.plan()
        return self.engine.approve(run["id"], SCOPE, run["digest"], "test-operator")

    def expire(self, run):
        approval = run["approval"]
        approval["expires_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.store.db.execute("UPDATE runs SET approval=? WHERE id=?", (canonical(approval), run["id"]))

    def test_end_to_end_and_duplicate_execution(self):
        run = self.approved()
        result = self.engine.execute(run["id"], SCOPE)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.store.source(SCOPE, run["plan"]["change"]["source_id"]), self.spec)
        self.engine.execute(run["id"], SCOPE)
        events = self.store.events(run["id"], SCOPE)
        self.assertEqual(sum(e["kind"] == "tool.applied" for e in events), 1)

    def test_no_execution_before_approval(self):
        run = self.plan()
        with self.assertRaisesRegex(Rejected, "approval required"):
            self.engine.execute(run["id"], SCOPE)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)

    def test_wrong_approval_digest(self):
        run = self.plan()
        with self.assertRaisesRegex(Rejected, "digest"):
            self.engine.approve(run["id"], SCOPE, "wrong", "operator")

    def test_scope_isolation_all_access_paths(self):
        run = self.approved()
        for field, value in (("client", "other"), ("tenant", "other"), ("environment", "production")):
            scope = {**SCOPE, field: value}
            for call in (lambda: self.engine.show(run["id"], scope),
                         lambda: self.engine.execute(run["id"], scope),
                         lambda: self.engine.approve(run["id"], scope, run["digest"], "operator"),
                         lambda: self.engine.revoke(run["id"], scope)):
                with self.assertRaisesRegex(Rejected, "not found"):
                    call()

    def test_same_source_name_is_isolated_between_clients(self):
        run = self.approved()
        self.engine.execute(run["id"], SCOPE)
        other_scope = {**SCOPE, "client": "other"}
        other = self.engine.plan(other_scope, "HR source", self.spec)
        self.assertIsNone(other["plan"]["change"]["previous"])

    def test_changed_plan_invalidates_approval(self):
        run = self.approved()
        run["plan"]["change"]["desired"]["authoritative"] = False
        self.store.db.execute("UPDATE runs SET plan=? WHERE id=?", (canonical(run["plan"]), run["id"]))
        with self.assertRaisesRegex(Rejected, "changed after approval"):
            self.engine.execute(run["id"], SCOPE)

    def test_expired_approval_blocks_mutation(self):
        run = self.approved()
        self.expire(run)
        with self.assertRaisesRegex(Rejected, "expired"):
            self.engine.execute(run["id"], SCOPE)

    def test_revoke_and_reapprove(self):
        run = self.approved()
        self.engine.revoke(run["id"], SCOPE)
        with self.assertRaises(Rejected):
            self.engine.execute(run["id"], SCOPE)
        self.engine.approve(run["id"], SCOPE, run["digest"], "new-operator")
        self.assertEqual(self.engine.execute(run["id"], SCOPE)["status"], "completed")

    def test_restart_resumes_without_duplicate_mutation(self):
        run = self.approved()
        def crash():
            raise RuntimeError("simulated process crash")
        with self.assertRaises(RuntimeError):
            self.engine.execute(run["id"], SCOPE, after_apply=crash)
        self.store.close()
        self.store = Store(self.dbpath)
        self.engine = Engine(self.store, Knowledge(ROOT / "knowledge"))
        self.assertEqual(self.engine.execute(run["id"], SCOPE)["status"], "completed")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM steps WHERE name='apply'").fetchone()[0], 1)

    def test_expired_approval_allows_read_only_recovery(self):
        run = self.approved()
        def crash():
            raise RuntimeError("crash")
        with self.assertRaises(RuntimeError):
            self.engine.execute(run["id"], SCOPE, after_apply=crash)
        self.expire(run)
        self.assertEqual(self.engine.execute(run["id"], SCOPE)["status"], "completed")

    def test_drift_blocks_second_plan(self):
        first, second = self.approved(), self.approved()
        self.engine.execute(first["id"], SCOPE)
        with self.assertRaisesRegex(Rejected, "drifted"):
            self.engine.execute(second["id"], SCOPE)

    def test_mutation_and_checkpoint_roll_back_together(self):
        run = self.approved()
        original_event = self.store.event
        def fail_event(run_id, kind, details):
            if kind == "tool.applied":
                raise RuntimeError("disk failure")
            original_event(run_id, kind, details)
        self.store.event = fail_event
        with self.assertRaises(RuntimeError):
            self.engine.execute(run["id"], SCOPE)
        self.assertIsNone(self.store.source(SCOPE, run["plan"]["change"]["source_id"]))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM steps").fetchone()[0], 0)

    def test_parallel_workers_apply_only_once(self):
        run = self.approved()
        def execute():
            store = Store(self.dbpath)
            try:
                return Engine(store, Knowledge(ROOT / "knowledge")).execute(run["id"], SCOPE)["status"]
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(lambda _: execute(), range(2))), ["completed", "completed"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM steps WHERE name='apply'").fetchone()[0], 1)

    def test_verification_detects_intervening_change(self):
        run = self.approved()
        def drift():
            changed = {**self.spec, "authoritative": False}
            self.store.db.execute("UPDATE sources SET config=?", (canonical(changed),))
        with self.assertRaisesRegex(Rejected, "Verification failed"):
            self.engine.execute(run["id"], SCOPE, after_apply=drift)
        self.assertEqual(self.store.get(run["id"], SCOPE)["status"], "running")

    def test_no_evidence_fails_closed(self):
        engine = Engine(self.store, Knowledge(self.temp.name))
        with self.assertRaisesRegex(Rejected, "No relevant evidence"):
            engine.plan(SCOPE, "HR source", self.spec)

    def test_client_documents_are_filtered_before_retrieval(self):
        template = json.loads((ROOT / "knowledge/simulation-source.json").read_text())
        template.update(trust="client", scope=SCOPE)
        Path(self.temp.name, "client.json").write_text(json.dumps(template))
        knowledge = Knowledge(self.temp.name)
        self.assertEqual(len(knowledge.search("HR source", scope=SCOPE)), 1)
        self.assertEqual(knowledge.search("HR source", scope={**SCOPE, "client": "other"}), [])
        self.assertEqual(knowledge.search("HR source"), [])

    def test_invalid_schema_and_mapping_rejected(self):
        mutations = [lambda s: s.update(endpoint="https://arbitrary.invalid"),
                     lambda s: s.update(account_id="missing"),
                     lambda s: s["attributes"].append(s["attributes"][0]),
                     lambda s: s["mappings"].append(s["mappings"][0]),
                     lambda s: s["mappings"][0].update(source="unknown"),
                     lambda s: s.update(connector="invented"),
                     lambda s: s.update(authoritative="true")]
        for mutate in mutations:
            spec = copy.deepcopy(self.spec)
            mutate(spec)
            with self.subTest(spec=spec), self.assertRaises(Rejected):
                check_spec(spec)

    def test_model_cannot_change_explicit_requirements(self):
        class UnreliableModel:
            model = "test-model"
            def generate(self, role, data, schema):
                return {**data["requirements"], "authoritative": False}
        engine = Engine(self.store, Knowledge(ROOT / "knowledge"), UnreliableModel())
        with self.assertRaisesRegex(Rejected, "changed explicit requirements"):
            engine.plan(SCOPE, "Ignore the supplied requirements", self.spec)

    def test_model_rejection_blocks_approval(self):
        class RejectingModel:
            model = "test-model"
            def generate(self, role, data, schema):
                if "requirements" in data:
                    return data["requirements"]
                return {"approved": False, "findings": ["Unsupported lifecycle work requested"]}
        engine = Engine(self.store, Knowledge(ROOT / "knowledge"), RejectingModel())
        run = engine.plan(SCOPE, "Configure all leaver workflows", self.spec)
        self.assertEqual(run["status"], "rejected")
        with self.assertRaises(Rejected):
            engine.approve(run["id"], SCOPE, run["digest"], "operator")


if __name__ == "__main__":
    unittest.main()
