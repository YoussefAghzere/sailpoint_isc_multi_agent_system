"""Plan compiler, external policy gate, and resumable local workflow."""
import json
import uuid
from datetime import datetime, timedelta, timezone

from .agents import Architect, Reviewer
from .models import Rejected, SCOPE, canonical, digest, validate, check_spec
from .store import now


def source_id(spec):
    return "src_" + digest(spec["source_name"].strip().casefold())[:24]


class ToolGateway:
    """Only a simulator is registered. Agents cannot register tools or get secrets."""
    def __init__(self, store):
        self.store = store

    def apply(self, scope, change):
        check_spec(change["desired"])
        current = self.store.source(scope, change["source_id"])
        if digest(current) != change["expected_state_hash"]:
            raise Rejected("Tenant state drifted; create and approve a new plan")
        self.store.db.execute(
            "INSERT INTO sources(scope,id,config) VALUES(?,?,?) "
            "ON CONFLICT(scope,id) DO UPDATE SET config=excluded.config",
            (canonical(scope), change["source_id"], canonical(change["desired"])))
        return {"source_id": change["source_id"], "state_hash": digest(change["desired"]),
                "backend": "simulator"}


class Engine:
    def __init__(self, store, knowledge, model=None):
        self.store = store
        self.knowledge = knowledge
        self.architect = Architect(model)
        self.reviewer = Reviewer(model)
        self.gateway = ToolGateway(store)
        self.provider = "openai" if model else "offline"
        self.model_name = model.model if model else None

    def plan(self, scope, prompt, requirements):
        validate(SCOPE, scope)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
            raise Rejected("Prompt must contain 1–12000 characters")
        check_spec(requirements)
        evidence = self.knowledge.search(prompt + " " + canonical(requirements), scope=scope)
        spec = self.architect.run(prompt, requirements, evidence)
        sid = source_id(spec)
        previous = self.store.source(scope, sid)
        change = {"tool": "simulator.upsert_source", "source_id": sid,
                  "expected_state_hash": digest(previous), "previous": previous, "desired": spec}
        review = self.reviewer.run(prompt, spec, evidence, change)
        plan = {
            "version": 1, "scope": scope, "backend": "simulator", "prompt": prompt,
            "provider": self.provider, "model": self.model_name,
            "evidence": evidence, "specification": spec, "change": change, "review": review,
            "acceptance": ["Exact desired simulated configuration exists",
                           "No changes outside the specified client/tenant/environment"],
            "limitations": ["No live ISC API requests", "No aggregation or identity correlation executed",
                            "No provisioning or lifecycle workflows executed",
                            "No authentication boundary: trusted local operator only"],
            "recovery": {"strategy": "Reconcile checkpointed state; do not blindly retry",
                         "previous_configuration": previous,
                         "automatic_rollback": False},
        }
        run_id = uuid.uuid4().hex
        with self.store.transaction():
            self.store.db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)",
                                  (run_id, canonical(scope), "planned" if review["approved"] else "rejected",
                                   canonical(plan), None, now()))
            for kind, details in [
                ("research.completed", {"documents": len(evidence)}),
                ("architect.completed", {"provider": self.provider, "model": self.model_name}),
                ("implementation.compiled", {"tool": change["tool"]}),
                ("validation.completed", review),
                ("plan.created", {"digest": digest(plan)})]:
                self.store.event(run_id, kind, details)
        return self.show(run_id, scope)

    def show(self, run_id, scope):
        run = self.store.get(run_id, scope)
        return {**run, "digest": digest(run["plan"]), "events": self.store.events(run_id, scope)}

    def approve(self, run_id, scope, expected_digest, actor):
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
            raise Rejected("An operator name is required")
        with self.store.transaction():
            run = self.store.get(run_id, scope)
            if run["status"] != "planned" or not run["plan"]["review"]["approved"]:
                raise Rejected("Only a reviewed, planned run can be approved")
            if digest(run["plan"]) != expected_digest:
                raise Rejected("Approval digest does not match the reviewed plan")
            approval = {"digest": expected_digest, "actor": actor,
                        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
            self.store.db.execute("UPDATE runs SET status='approved',approval=? WHERE id=?",
                                  (canonical(approval), run_id))
            self.store.event(run_id, "approval.granted", approval)
        return self.show(run_id, scope)

    def revoke(self, run_id, scope):
        with self.store.transaction():
            run = self.store.get(run_id, scope)
            if run["status"] != "approved":
                raise Rejected("Can revoke only before execution starts")
            self.store.db.execute("UPDATE runs SET status='planned',approval=NULL WHERE id=?", (run_id,))
            self.store.event(run_id, "approval.revoked", {})
        return self.show(run_id, scope)

    def _authorize(self, run, *, already_applied=False):
        plan, approval = run["plan"], run["approval"]
        if plan["backend"] != "simulator" or plan["change"]["tool"] != "simulator.upsert_source":
            raise Rejected("Unregistered backend or tool")
        if plan["scope"] != run["scope"] or not plan["review"]["approved"]:
            raise Rejected("Plan scope or review invalid")
        if run["status"] not in {"approved", "running"} or not approval:
            raise Rejected("Explicit operator approval required")
        if approval["digest"] != digest(plan):
            raise Rejected("Plan changed after approval")
        if not already_applied and datetime.fromisoformat(approval["expires_at"]) <= datetime.now(timezone.utc):
            raise Rejected("Approval expired; revoke and approve again before execution")
        check_spec(plan["change"]["desired"])
        if plan["change"]["desired"] != plan["specification"]:
            raise Rejected("Compiled change does not match specification")
        if plan["change"]["source_id"] != source_id(plan["specification"]):
            raise Rejected("Unexpected target source")

    def execute(self, run_id, scope, *, after_apply=None):
        """Two durable checkpoints. after_apply is a test-only crash injection hook.

        Simulator mutations and their checkpoints commit atomically. Live execution
        would additionally need remote operation IDs, reconciliation and leases.
        """
        with self.store.transaction():
            run = self.store.get(run_id, scope)
            if run["status"] == "completed":
                return self.show(run_id, scope)
            step = self.store.db.execute("SELECT result FROM steps WHERE run_id=? AND name='apply'",
                                         (run_id,)).fetchone()
            self._authorize(run, already_applied=step is not None)
            if not step:
                result = self.gateway.apply(scope, run["plan"]["change"])
                self.store.db.execute("INSERT INTO steps VALUES(?,?,?)", (run_id, "apply", canonical(result)))
                self.store.db.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
                self.store.event(run_id, "tool.applied", result)
        if after_apply:
            after_apply()
        with self.store.transaction():
            run = self.store.get(run_id, scope)
            if run["status"] == "completed":
                return self.show(run_id, scope)
            # Authorization is checked at each step. Expired approval may resume only
            # read-only verification of an already atomically checkpointed mutation.
            change = run["plan"]["change"]
            if not run["approval"] or run["approval"]["digest"] != digest(run["plan"]):
                raise Rejected("Plan integrity check failed during verification")
            observed = self.store.source(scope, change["source_id"])
            if observed != change["desired"]:
                raise Rejected("Verification failed; state changed after apply; manual reconciliation needed")
            result = {"passed": True, "state_hash": digest(observed), "backend": "simulator"}
            self.store.db.execute("INSERT OR IGNORE INTO steps VALUES(?,?,?)",
                                  (run_id, "verify", canonical(result)))
            self.store.db.execute("UPDATE runs SET status='completed' WHERE id=?", (run_id,))
            self.store.event(run_id, "verification.completed", result)
        return self.show(run_id, scope)
