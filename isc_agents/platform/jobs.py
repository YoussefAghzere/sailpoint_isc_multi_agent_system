"""Database-backed jobs, leases, recurring ingestion, and one-step workflow dispatch."""
import json
import os
import time
import uuid

from ..models import Rejected, canonical
from .catalog import Catalog
from .db import decoded
from .isc import ISC
from .research import Research
from .model_profiles import ModelProfiles
from .specialists import Orchestrator, PlannerModel, ToolRegistry
from .workflows import Workflows


class Worker:
    def __init__(self, db, environments, http, model=None):
        self.db, self.environments, self.http, self.model = db, environments, http, model
        self.owner = uuid.uuid4().hex
        self.catalog = Catalog(db)
        self.research = Research(db, http, os.environ.get("BRAVE_SEARCH_API_KEY"),
                                 os.environ.get("ISC_RESEARCH_HOSTS", "").split(","))
        self.workflows = Workflows(db, environments, self.isc)

    def isc(self, environment_id):
        return ISC(self.db, self.environments, self.http, environment_id)

    def schedule_due(self):
        with self.db.tx():
            for schedule in self.db.all("SELECT * FROM schedules WHERE next_run<=?", (time.time(),)):
                pending = self.db.one("SELECT id FROM jobs WHERE kind=? AND environment_id IS ? AND state IN ('queued','running')",
                                      (schedule["kind"], schedule["environment_id"]))
                if not pending:
                    self.db.enqueue(schedule["kind"], schedule["environment_id"], json.loads(schedule["payload"]))
                self.db.execute("UPDATE schedules SET next_run=? WHERE id=?", (time.time() + schedule["interval_seconds"], schedule["id"]))

    def run_one(self):
        self.schedule_due()
        with self.db.tx():
            row = self.db.one("SELECT * FROM jobs WHERE (state='queued' AND lease_until<=?) OR "
                              "(state='running' AND lease_until<?) ORDER BY created LIMIT 1", (time.time(), time.time()))
            if not row:
                return False
            self.db.execute("UPDATE jobs SET state='running',owner=?,lease_until=?,updated=? WHERE id=?",
                            (self.owner, time.time() + 300, time.time(), row["id"]))
        row = decoded(row, "payload")
        try:
            result = self.handle(row)
            done = result.pop("_done", True)
            with self.db.tx():
                self.db.execute("UPDATE jobs SET state=?,result=?,lease_until=?,updated=? WHERE id=? AND owner=?",
                    ("completed" if done else "queued", canonical(result), result.get("next_at", 0), time.time(), row["id"], self.owner))
        except Exception as exc:
            message = str(exc)[:500] if isinstance(exc, Rejected) else "Worker error; inspect server diagnostics (no automatic mutation replay)"
            with self.db.tx():
                self.db.execute("UPDATE jobs SET state='failed',error=?,updated=? WHERE id=? AND owner=?", (message, time.time(), row["id"], self.owner))
                self.db.audit("worker", row["environment_id"], "job.failed", {"job": row["id"], "kind": row["kind"], "error": message})
        return True

    def heartbeat(self, job_id, event):
        self.db.execute("UPDATE jobs SET lease_until=?,result=?,updated=? WHERE id=? AND owner=?",
                        (time.time() + 300, canonical({"progress": event}), time.time(), job_id, self.owner))

    def handle(self, job):
        payload = job["payload"]
        environment_id = job["environment_id"]
        kind = job["kind"]
        if kind == "sync_specs":
            result = self.catalog.sync_latest(self.http)
            spec = self.catalog.get(result["id"])
            count = 0
            for path, item in spec["document"]["paths"].items():
                for method, operation in item.items():
                    if method not in {"get", "post", "put", "patch", "delete"} or not operation.get("operationId"):
                        continue
                    url = spec["origin"] + "?sha256=" + spec["hash"] + "#" + operation["operationId"]
                    self.research.ingest(url, f"API {spec['version']} {method.upper()} {path}\n" + operation.get("description", operation.get("summary", "")),
                                         operation.get("summary", operation["operationId"]), "official")
                    count += 1
            return {**result, "indexed_operations": count}
        if kind == "crawl":
            state = self.research.crawl_page(payload)
            self.db.execute("UPDATE jobs SET payload=? WHERE id=? AND owner=?", (canonical(state), job["id"], self.owner))
            return {"_done": state["done"], "pages": len(state["visited"]), "frontier": len(state["queue"]), "next_at": time.time() + 0.2}
        if kind == "discover":
            return self.isc(environment_id).discover()
        if kind == "execute":
            result = self.workflows.tick(payload["run_id"], environment_id)
            return {**result, "_done": result["done"]}
        if kind == "plan":
            env = self.environments.get(environment_id)
            if env["backend"] == "simulator":
                requirements = payload.get("requirements")
                if not requirements:
                    raise Rejected("Offline simulation requires a structured requirements object")
                run = self.workflows.simulate(environment_id, requirements, payload["prompt"], payload["actor"])
            elif payload.get("proposal"):
                run = self.workflows.compile(environment_id, payload["proposal"], payload["actor"], request_prompt=payload["prompt"])
            else:
                profiles = ModelProfiles(self.db, self.environments.vault)
                model_snapshot = payload.get("model_profile") if "model_profile" in payload else profiles.snapshot(payload["actor"])
                model = self.model or profiles.model(self.http, payload["actor"], model_snapshot)
                registry = ToolRegistry(self.db, env)
                agent = Orchestrator(model, self.research, self.catalog, self.isc(environment_id), env, registry,
                                     external=payload.get("external", False), heartbeat=lambda e: self.heartbeat(job["id"], e))
                attachments = self.db.all("SELECT id,name,size FROM attachments WHERE environment_id=?", (environment_id,))
                output = agent.run(payload["prompt"], attachments)
                run = self.workflows.compile(environment_id, output["result"], payload["actor"], model=model,
                    trace=output["trace"], evidence=output["evidence"], request_prompt=payload["prompt"], model_metadata=model_snapshot)
            return {"run_id": run["id"], "status": run["status"]}
        raise Rejected("Unknown job kind")
