"""Encrypted, environment-scoped storage for agent-selected answers."""
import time
import uuid

from ..models import Rejected


class Answers:
    """Tenant data stays encrypted; list endpoints expose only nonsensitive metadata."""
    def __init__(self, db, environments):
        self.db, self.environments = db, environments

    def save(self, eid, actor, data, *, expected_revision=None):
        aid = uuid.uuid4().hex
        with self.db.tx():
            env = self.environments.get(eid)
            if expected_revision is not None and env["revision"] != expected_revision:
                raise Rejected("Environment changed while reading; submit a fresh request")
            self.db.execute("INSERT INTO answers VALUES(?,?,?,?,?,?,?)", (aid, eid, actor, data["kind"],
                env["revision"], self.environments.vault.encrypt(data), time.time()))
            self.db.audit(actor, eid, "answer.created", {"id": aid, "kind": data["kind"], "status": data["status"]})
        return {"answer_id": aid, "status": data["status"]}

    def get(self, eid, aid):
        row = self.db.one("SELECT * FROM answers WHERE id=? AND environment_id=?", (aid, eid))
        if not row:
            raise Rejected("Answer not found in this environment")
        return {"id": aid, "created": row["created"], "revision": row["revision"],
                **self.environments.vault.decrypt(row["content"])}
