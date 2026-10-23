"""Persisted remote workflows with approval, drift checks and ambiguity containment."""
import json
import re
import time
import uuid

import jsonschema

from ..models import Rejected, canonical, digest, check_spec
from .catalog import Catalog
from .db import decoded
from .network import Ambiguous
from .security import reject_inline_secrets, SECRET_KEYS
from .specialists import REVIEW_SCHEMA


WAIT = {"type": "object", "additionalProperties": False,
        "required": ["pointer", "equals", "max_attempts", "interval_seconds"],
        "properties": {"pointer": {"type": "string"}, "equals": {}, "fail_values": {"type": "array"},
                       "max_attempts": {"type": "integer", "minimum": 1, "maximum": 20},
                       "interval_seconds": {"type": "integer", "minimum": 1, "maximum": 60}}}
PROPOSAL = {"type": "object", "additionalProperties": False,
    "required": ["title", "questions", "steps", "checks"],
    "properties": {"title": {"type": "string", "minLength": 1, "maxLength": 200},
        "questions": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 1000}},
        "steps": {"type": "array", "minItems": 1, "maxItems": 20, "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "operation_id", "arguments"],
            "properties": {"id": {"type": "string", "pattern": "^[a-zA-Z][a-zA-Z0-9_]{0,39}$"},
                           "operation_id": {"type": "string"}, "tool_id": {"type": "string"}, "arguments": {"type": "object"}, "wait": WAIT}}},
        "checks": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "object",
            "additionalProperties": False, "required": ["step_id", "pointer", "equals"],
            "properties": {"step_id": {"type": "string"}, "pointer": {"type": "string"}, "equals": {}}}}}}


def pointer(value, path):
    if path == "":
        return value
    if not path.startswith("/"):
        raise Rejected("Checks require JSON Pointer paths, such as /status")
    try:
        for part in path[1:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) else value[key]
        return value
    except (KeyError, TypeError, ValueError, IndexError):
        raise Rejected("Verification pointer was not present in the API result") from None


REFERENCE = re.compile(r"\$\{steps\.([A-Za-z][A-Za-z0-9_]*)(?:\.([^}]+))?\}")


def bind(value, results):
    if isinstance(value, dict):
        return {k: bind(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [bind(v, results) for v in value]
    if isinstance(value, str) and "${steps." in value:
        match = REFERENCE.fullmatch(value)
        if not match or match[1] not in results:
            raise Rejected("Step reference must be a complete value from a completed earlier step")
        data = results[match[1]]
        return pointer(data, "/" + match[2].replace(".", "/")) if match[2] else data
    return value


class Workflows:
    def __init__(self, db, environments, isc_factory):
        self.db, self.environments, self.isc_factory = db, environments, isc_factory
        self.catalog = Catalog(db)

    def get(self, run_id, environment_id):
        row = decoded(self.db.one("SELECT * FROM live_runs WHERE id=? AND environment_id=?",
                                 (run_id, environment_id)), "plan", "approval")
        if not row:
            raise Rejected("Run not found in this environment")
        row["steps"] = [decoded(r, "result") for r in self.db.all("SELECT * FROM live_steps WHERE run_id=? ORDER BY ordinal", (run_id,))]
        return row

    def _save(self, environment_id, actor, plan, status):
        run_id = uuid.uuid4().hex
        with self.db.tx():
            self.db.execute("INSERT INTO live_runs VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, environment_id, actor, status, canonical(plan), digest(plan), None, time.time(), None))
            for ordinal, step in enumerate(plan.get("steps", [])):
                self.db.execute("INSERT INTO live_steps VALUES(?,?,?,?)", (run_id, ordinal, "pending", None))
            self.db.audit(actor, environment_id, "run.planned", {"id": run_id, "hash": digest(plan), "status": status})
        return self.get(run_id, environment_id)

    def questions(self, environment_id, actor, questions, trace=()):
        if not questions or not all(isinstance(q, str) for q in questions):
            raise Rejected("Agent finished without a workflow or useful clarification questions")
        return self._save(environment_id, actor, {"questions": questions[:10], "trace": list(trace), "steps": []}, "needs_input")

    def simulate(self, environment_id, spec, prompt, actor):
        check_spec(spec)
        env = self.environments.get(environment_id)
        self.db.execute("CREATE TABLE IF NOT EXISTS simulated_sources(environment_id TEXT,name TEXT,config TEXT,PRIMARY KEY(environment_id,name))")
        current = self.db.one("SELECT config FROM simulated_sources WHERE environment_id=? AND name=?", (environment_id, spec["source_name"]))
        plan = {"title": prompt[:200], "backend": "simulator", "environment_revision": env["revision"],
                "steps": [{"id": "source", "operation_id": "simulator.upsert_source", "arguments": spec}],
                "expected_hash": digest(json.loads(current["config"]) if current else None),
                "review": {"approved": True, "findings": ["Structural simulation checks only; no live ISC actions"]}}
        return self._save(environment_id, actor, plan, "planned")

    def compile(self, environment_id, proposal, actor, *, model=None, trace=(), evidence=(), request_prompt="Manually authored workflow", model_metadata=None):
        if proposal.get("questions"):
            return self.questions(environment_id, actor, proposal["questions"], trace)
        try:
            jsonschema.validate(proposal, PROPOSAL)
        except jsonschema.ValidationError:
            raise Rejected("Workflow proposal has an invalid structure") from None
        reject_inline_secrets(proposal)
        env = self.environments.get(environment_id)
        if env["backend"] != "isc":
            raise Rejected("Use the simulator's structured requirements workflow for this environment")
        if time.time() - self.catalog.get(env["spec_id"])["fetched"] > 7 * 86400:
            raise Rejected("Pinned API specification is stale; refresh and select a reviewed contract")
        if model and not any(e.get("trust") == "official" and time.time() - e.get("fetched", 0) < 7 * 86400 for e in evidence):
            return self.questions(environment_id, actor, ["Ingest fresh official documentation relevant to this integration before agent planning."], trace)
        steps, seen, static_targets = [], set(), set()
        isc = self.isc_factory(environment_id)
        for step in proposal["steps"]:
            if step["id"] in seen:
                raise Rejected("Duplicate workflow step ID")
            args = step["arguments"]
            for ref in REFERENCE.finditer(canonical(args)):
                if ref[1] not in seen:
                    raise Rejected("References must target an earlier workflow step")
            op = self.catalog.prepare(env["spec_id"], step["operation_id"], args, template=True)
            if op["method"] == "DELETE":
                raise Rejected("Destructive operations are disabled")
            if step.get("wait") and op["method"] != "GET":
                raise Rejected("Only read-only operations may be polled")
            compiled = {**step, "method": op["method"], "api_path": op["path"]}
            if step.get("tool_id"):
                tool = self.db.one("SELECT * FROM tools WHERE id=? AND environment_id=? AND status='active'", (step["tool_id"], environment_id))
                if not tool:
                    raise Rejected("Workflow tool is not active in this environment")
                manifest = json.loads(tool["manifest"])
                if manifest["spec_id"] != env["spec_id"] or manifest["operation_id"] != step["operation_id"]:
                    raise Rejected("Workflow operation does not match the registered tool")
                compiled["tool_hash"] = tool["hash"]
            if op["method"] != "GET":
                if not env["write_enabled"] or step["operation_id"] not in env["allowed_write_operations"]:
                    raise Rejected("Requested mutation exceeds this environment's write permissions")
                # JSON Patch places secrets under 'value'; inspect the target path too.
                for patch in args.get("body", []) if isinstance(args.get("body"), list) else []:
                    if isinstance(patch, dict) and SECRET_KEYS.search(str(patch.get("path", ""))):
                        if not re.fullmatch(r"\$\{secrets\.[A-Za-z0-9_-]+\}", str(patch.get("value", ""))):
                            raise Rejected("Secret patch values must use environment secret references")
                if op["method"] in {"PATCH", "PUT"} and "${steps." not in canonical(args.get("path", {})):
                    target = op["resolved_path"]
                    if target in static_targets:
                        raise Rejected("Combine modifications to the same existing resource into one step")
                    static_targets.add(target)
                    item = op["document"]["paths"][op["path"]]
                    read = item.get("get", {}).get("operationId")
                    if not read:
                        raise Rejected("Existing-resource mutation requires a corresponding GET for drift detection")
                    guard_args = {"path": args.get("path", {})}
                    baseline = isc.call(read, guard_args)
                    compiled["guard"] = {"operation_id": read, "arguments": guard_args, "hash": baseline["state_hash"]}
                if step["operation_id"] == "createSource":
                    name = args.get("body", {}).get("name")
                    if not isinstance(name, str) or any(c in name for c in '\\"\r\n'):
                        raise Rejected("Source creation requires a plain source name for duplicate detection")
                    guard_args = {"query": {"filters": 'name eq "' + name + '"', "limit": 2}}
                    existing = isc.call("listSources", guard_args)
                    if existing["data"]:
                        raise Rejected("A source with this name already exists; design an update instead")
                    compiled["guard"] = {"operation_id": "listSources", "arguments": guard_args, "hash": existing["state_hash"]}
            steps.append(compiled)
            seen.add(step["id"])
        checked = {c["step_id"] for c in proposal["checks"]}
        if not checked <= seen:
            raise Rejected("Verification check refers to an unknown step")
        for ordinal, step in enumerate(steps):
            if step["method"] != "GET" and not any(s["method"] == "GET" and s["id"] in checked for s in steps[ordinal + 1:]):
                raise Rejected("Every mutation requires a subsequent checked read or job-status poll")
        review = {"approved": True, "findings": ["Manually authored workflow passed contract and policy checks; business semantics require operator review"]}
        if model:
            review = model.generate("Independent validator: assess the entire requested integration, the operation contracts, "
                "verification sufficiency, missing prerequisites, client decisions, and rollback limitations. Reject unsafe or "
                "incomplete workflows. Your approval is advisory only.",
                {"request": request_prompt, "proposal": proposal, "environment": env, "evidence": list(evidence)[:6]}, REVIEW_SCHEMA)
        plan = {**proposal, "request": request_prompt, "model": model_metadata, "steps": steps, "backend": "isc", "environment_revision": env["revision"],
                "spec_id": env["spec_id"], "review": review, "trace": list(trace),
                "evidence": list(evidence), "recovery": "Pause ambiguous mutations; reconcile with read-only evidence before continuing. No automatic rollback."}
        return self._save(environment_id, actor, plan, "planned" if review["approved"] else "rejected")

    def approve(self, run_id, environment_id, expected_hash, actor):
        with self.db.tx():
            run = self.get(run_id, environment_id)
            env = self.environments.get(environment_id)
            if run["status"] != "planned" or expected_hash != run["hash"] or digest(run["plan"]) != expected_hash:
                raise Rejected("Only an unchanged, reviewed plan can be approved")
            if env["revision"] != run["plan"]["environment_revision"]:
                raise Rejected("Environment changed; generate a new plan")
            if env["stage"] == "production" and env["backend"] == "isc" and actor == run["creator"]:
                raise Rejected("Production requires a different authenticated reviewer")
            approval = {"actor": actor, "hash": expected_hash, "expires": time.time() + 3600}
            self.db.execute("UPDATE live_runs SET status='approved',approval=? WHERE id=?", (canonical(approval), run_id))
            self.db.audit(actor, environment_id, "run.approved", {"id": run_id, "hash": expected_hash})
        return self.get(run_id, environment_id)

    def _check(self, run, env):
        approval = run["approval"]
        if not approval or approval["hash"] != digest(run["plan"]) or run["hash"] != approval["hash"]:
            raise Rejected("An unchanged approved plan is required")
        if approval["expires"] <= time.time():
            raise Rejected("Approval expired; renew it before continuing")
        if env["revision"] != run["plan"]["environment_revision"]:
            raise Rejected("Environment or credentials changed after planning")
        if env["backend"] == "isc" and time.time() - self.catalog.get(env["spec_id"])["fetched"] > 7 * 86400:
            raise Rejected("Pinned API specification is stale; refresh its evidence before execution")
        for evidence in run["plan"].get("evidence", []):
            if evidence.get("id"):
                current = self.db.one("SELECT hash,fetched FROM documents WHERE id=?", (evidence["id"],))
                if not current or current["hash"] != evidence["hash"] or time.time() - current["fetched"] > 7 * 86400:
                    raise Rejected("Reviewed documentation changed or became stale; generate a new plan")
        for step in run["plan"].get("steps", []):
            if step.get("tool_id"):
                row = self.db.one("SELECT hash,status FROM tools WHERE id=? AND environment_id=?", (step["tool_id"], env["id"]))
                if not row or row["status"] != "active" or row["hash"] != step["tool_hash"]:
                    raise Rejected("A workflow tool was changed or deactivated")

    def tick(self, run_id, environment_id):
        run = self.get(run_id, environment_id)
        env = self.environments.get(environment_id)
        if run["status"] == "completed":
            return {"done": True, "run_id": run_id}
        if run["status"] not in {"approved", "running"}:
            raise Rejected("Run must be approved and free of unresolved failures")
        self._check(run, env)
        if env["backend"] == "simulator":
            return self._simulate_tick(run, env)
        with self.db.tx():
            lock = self.db.one("SELECT * FROM environment_locks WHERE environment_id=?", (environment_id,))
            if lock and lock["run_id"] != run_id:
                raise Rejected("Another workflow holds this environment; reconcile or complete it first")
            self.db.execute("INSERT OR IGNORE INTO environment_locks VALUES(?,?)", (environment_id, run_id))
        results = {}
        for item, step in zip(run["steps"], run["plan"]["steps"]):
            if item["state"] == "done":
                results[step["id"]] = item["result"]["data"]
        pending = next((s for s in run["steps"] if s["state"] != "done"), None)
        if pending is None:
            try:
                for check in run["plan"]["checks"]:
                    if pointer(results[check["step_id"]], check["pointer"]) != check["equals"]:
                        raise Rejected("Business verification check failed; inspect recorded evidence")
            except Rejected as exc:
                self.fail(run_id, environment_id, str(exc))
                raise
            with self.db.tx():
                self.db.execute("UPDATE live_runs SET status='completed' WHERE id=?", (run_id,))
                self.db.execute("DELETE FROM environment_locks WHERE environment_id=? AND run_id=?", (environment_id, run_id))
                self.db.audit("worker", environment_id, "run.verified", {"id": run_id})
            return {"done": True, "run_id": run_id}
        ordinal = pending["ordinal"]
        step = run["plan"]["steps"][ordinal]
        if pending["state"] == "sending":
            if step["method"] != "GET":
                self.fail(run_id, environment_id, "Worker stopped during remote mutation", ambiguous=True)
                raise Ambiguous("Remote mutation outcome is unknown; reconcile without replay")
        if pending["result"] and pending["result"].get("next_at", 0) > time.time():
            return {"done": False, "next_at": pending["result"]["next_at"]}
        isc = self.isc_factory(environment_id)
        try:
            args = bind(step["arguments"], results)
            guard = step.get("guard")
            if guard and isc.call(guard["operation_id"], guard["arguments"])["state_hash"] != guard["hash"]:
                raise Rejected("Tenant state drift detected before mutation; replan")
            with self.db.tx():
                fresh = self.get(run_id, environment_id)
                self._check(fresh, self.environments.get(environment_id))
                state = fresh["steps"][ordinal]["state"]
                if state not in {"pending", "polling", "sending"} or (state == "sending" and step["method"] != "GET"):
                    raise Rejected("Step is already claimed by another worker")
                self.db.execute("UPDATE live_steps SET state='sending' WHERE run_id=? AND ordinal=?", (run_id, ordinal))
                self.db.execute("UPDATE live_runs SET status='running' WHERE id=?", (run_id,))
                self.db.audit("worker", environment_id, "step.started", {"run": run_id, "step": step["id"], "operation": step["operation_id"]})
            result = isc.call(step["operation_id"], args, mutation_authorized=step["method"] != "GET")
            attempts = (pending["result"] or {}).get("attempts", 0) + 1
            state = "done"
            if step.get("wait"):
                wait = step["wait"]
                value = pointer(result["data"], wait["pointer"])
                if value in wait.get("fail_values", []):
                    raise Rejected("Asynchronous ISC task reported a terminal failure")
                if value != wait["equals"]:
                    if attempts >= wait["max_attempts"]:
                        raise Rejected("Asynchronous ISC task exceeded its polling budget")
                    state = "polling"
                    result["next_at"] = time.time() + wait["interval_seconds"]
                result["attempts"] = attempts
            with self.db.tx():
                self.db.execute("UPDATE live_steps SET state=?,result=? WHERE run_id=? AND ordinal=?",
                                (state, canonical(result), run_id, ordinal))
                self.db.audit("worker", environment_id, "step." + state, {"run": run_id, "step": step["id"], "request_id": result.get("request_id")})
            return {"done": False, "next_at": result.get("next_at", time.time())}
        except Ambiguous:
            self.fail(run_id, environment_id, "Remote mutation outcome is ambiguous", ambiguous=True)
            raise
        except Rejected as exc:
            self.fail(run_id, environment_id, str(exc))
            raise

    def fail(self, run_id, environment_id, message, *, ambiguous=False):
        with self.db.tx():
            self.db.execute("UPDATE live_runs SET status=?,error=? WHERE id=? AND environment_id=?",
                            ("needs_reconciliation" if ambiguous else "failed", message[:500], run_id, environment_id))
            # Keep the environment lock after failures: prior steps may have changed it.
            self.db.audit("worker", environment_id, "run.paused", {"run": run_id, "ambiguous": ambiguous, "error": message[:500]})

    def _simulate_tick(self, run, env):
        spec = run["plan"]["steps"][0]["arguments"]
        with self.db.tx():
            current = self.db.one("SELECT config FROM simulated_sources WHERE environment_id=? AND name=?", (env["id"], spec["source_name"]))
            if digest(json.loads(current["config"]) if current else None) != run["plan"]["expected_hash"]:
                raise Rejected("Simulated state drifted; generate a new plan")
            self.db.execute("INSERT OR REPLACE INTO simulated_sources VALUES(?,?,?)", (env["id"], spec["source_name"], canonical(spec)))
            self.db.execute("UPDATE live_steps SET state='done',result=? WHERE run_id=?", (canonical({"data": spec}), run["id"]))
            self.db.execute("UPDATE live_runs SET status='completed' WHERE id=?", (run["id"],))
            self.db.audit("worker", env["id"], "simulation.verified", {"run": run["id"], "hash": digest(spec)})
        return {"done": True, "run_id": run["id"]}

    def reconcile(self, run_id, environment_id, actor, *, operation_id, arguments, expected, decision):
        run = self.get(run_id, environment_id)
        if run["status"] not in {"needs_reconciliation", "failed"}:
            raise Rejected("Only paused workflows can be reconciled")
        result = self.isc_factory(environment_id).call(operation_id, arguments)
        if result["data"] != expected:
            raise Rejected("Reconciliation evidence did not match the expected observed state")
        with self.db.tx():
            if decision == "confirm_applied":
                unknown = next((s for s in run["steps"] if s["state"] == "sending"), None)
                if not unknown or run["status"] != "needs_reconciliation":
                    raise Rejected("There is no ambiguous mutation to confirm")
                self.db.execute("UPDATE live_steps SET state='done',result=? WHERE run_id=? AND ordinal=?",
                                (canonical(result), run_id, unknown["ordinal"]))
                self.db.execute("UPDATE live_runs SET status='planned',approval=NULL,error=NULL WHERE id=?", (run_id,))
            elif decision == "abandon":
                self.db.execute("UPDATE live_runs SET status='abandoned' WHERE id=?", (run_id,))
                self.db.execute("DELETE FROM environment_locks WHERE environment_id=? AND run_id=?", (environment_id, run_id))
            else:
                raise Rejected("Choose confirm_applied or abandon; ambiguous operations cannot be replayed")
            self.db.audit(actor, environment_id, "run.reconciled", {"run": run_id, "decision": decision, "evidence_hash": digest(result["data"])})
        return self.get(run_id, environment_id)
