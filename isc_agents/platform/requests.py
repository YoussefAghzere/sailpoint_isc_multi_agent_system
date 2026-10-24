"""Conservative intent routing and bounded, model-free identity reads."""
import json
import re
import time
import uuid

from ..models import Rejected


TASKS = {"auto", "identity_lookup", "explanation", "integration"}
LOOKUP = re.compile(
    r"^(?:(?:retrieve|get|show|find|look up|lookup)\s+(?:information|info|details|a summary)\s+(?:about|for|on)\s+(?:the identity(?: of)?\s+)?|"
    r"(?:find|show|lookup|look up|get)\s+(?:the\s+)?(?:identity|person|user)(?:\s+(?:of|for|named))?\s+|who is\s+)(.+?)[?.!]*$", re.I)
UNRELATED = re.compile(r"\b(?:role|mining|access|entitlements?|accounts?|connector|integrate|create|delete|disable|enable|update|change|reset|revoke|provision|and|then)\b", re.I)


def subject(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200 or any(ord(c) < 32 for c in value):
        raise Rejected("Enter a person's name, email, or identity ID (at most 200 characters)")
    return value.strip()


def route(prompt, task="auto", identity_query=""):
    if task not in TASKS:
        raise Rejected("Choose automatic routing, identity lookup, documentation question, or integration")
    if task == "identity_lookup":
        match = LOOKUP.fullmatch(prompt.strip())
        return {"task": task, "subject": subject(identity_query or (match[1] if match else prompt))}
    if task != "auto":
        return {"task": task}
    cleaned = re.sub(r"^please\s+", "", prompt.strip(), flags=re.I)
    match = LOOKUP.fullmatch(cleaned)
    if match:
        candidate = match[1].strip()
        if len(candidate.split()) <= 8 and not UNRELATED.search(candidate) and re.fullmatch(r"[\w\s@.'’+\-]+", candidate):
            return {"task": "identity_lookup", "subject": subject(candidate)}
    if re.match(r"^(?:explain\b|what (?:is|are)\b|how (?:does|do)\b)", prompt.strip(), re.I):
        return {"task": "explanation"}
    return {"task": "integration"}


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


class IdentityLookup:
    def __init__(self, isc):
        self.isc = isc
        self.evidence = []

    def _read(self, operation_id, args):
        op = self.isc.catalog.operation(self.isc.env["spec_id"], operation_id)
        expected = "/identities" if operation_id == "listIdentities" else "/identities/{id}"
        if op["method"] != "GET" or op["path"] != expected:
            raise Rejected("Identity lookup requires the reviewed listIdentities/getIdentity GET contracts")
        response = self.isc.call(operation_id, args)
        self.evidence.append({"operation_id": operation_id, "method": "GET", "path": op["path"],
            "spec_id": self.isc.env["spec_id"], "request_id": response.get("request_id"), "observed_at": time.time()})
        return response["data"]

    @staticmethod
    def summary(identity):
        # v2026 puts name components in attributes and uses emailAddress/managerRef.
        # Normalize a small allowlist across contracts without exposing custom attributes.
        result = {key: identity[key] for key in ("id", "name", "alias", "identityStatus", "cloudStatus",
            "processingState", "correlated", "identityProfile", "lifecycleState", "isManager", "created", "modified", "lastRefresh") if key in identity}
        attrs = identity.get("attributes")
        attrs = attrs if isinstance(attrs, dict) else {}
        for key in ("firstname", "lastname", "cloudStatus"):
            value = identity.get(key) if identity.get(key) is not None else attrs.get(key)
            if value is not None:
                result[key] = value
        email = identity.get("emailAddress") or identity.get("email") or attrs.get("email")
        if email is not None:
            result["email"] = email
        manager = identity.get("managerRef") or identity.get("manager")
        if manager is not None:
            result["manager"] = manager
        return result

    def run(self, query, selected_id=None):
        query = subject(query)
        base = {"kind": "identity_lookup", "query": query, "evidence": self.evidence,
            "scope": "Identity profile only. Accounts, entitlements, roles and role mining were not queried.", "model_calls": 0}
        if selected_id or re.fullmatch(r"[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", query):
            return self._details(base, selected_id or query)
        literal = lambda value: json.dumps(value, ensure_ascii=False)
        if "@" in query:
            filters = "email eq " + literal(query)
        else:
            filters = f"name eq {literal(query)} or alias eq {literal(query)}"
            parts = query.split(maxsplit=1)
            if len(parts) == 2:
                filters += f" or firstname eq {literal(parts[0])} and lastname eq {literal(parts[1])}"
        op = self.isc.catalog.operation(self.isc.env["spec_id"], "listIdentities")
        parameters = {self.isc.catalog.deref(op["document"], p)["name"] for p in op["parameters"]}
        args = {"filters": filters, "limit": 21}
        if "defaultFilter" in parameters:
            args["defaultFilter"] = "NONE"
        matches = self._read("listIdentities", {"query": args})
        if not isinstance(matches, list) or any(not isinstance(m, dict) or not isinstance(m.get("id"), str) for m in matches):
            raise Rejected("Identity search returned an unexpected result structure")
        if not matches:
            return {**base, "status": "not_found", "message": "No exact name, alias, or email match was returned. Try the person's email or identity ID; this does not prove the identity is absent."}
        if len(matches) == 1:
            return self._details(base, matches[0]["id"])
        return {**base, "status": "needs_selection", "candidates": [self.summary(m) for m in matches[:20]],
            "truncated": len(matches) > 20, "message": "Choose the correct identity, or refine the search using an email or identity ID."}

    def _details(self, base, identity_id):
        data = self._read("getIdentity", {"path": {"id": identity_id}})
        if not isinstance(data, dict) or data.get("id") != identity_id:
            raise Rejected("Identity details did not match the selected identity")
        return {**base, "status": "answered", "identity": self.summary(data), "message": "Identity profile retrieved from the selected tenant."}
