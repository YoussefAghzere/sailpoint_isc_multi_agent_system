import json
import re
import time

from ..models import Rejected
from .catalog import Catalog
from .security import redact
from .network import Ambiguous


class ISC:
    """Contract-validated client. This layer never grants write authorization."""
    def __init__(self, db, environments, http, environment_id):
        self.db, self.environments, self.http = db, environments, http
        self.env = environments.get(environment_id, credentials=True)
        self.environment_id = environment_id
        self.catalog = Catalog(db)
        self.token, self.expires = None, 0

    def _budget(self):
        at = time.time()
        with self.db.tx():
            previous = self.db.one("SELECT * FROM budgets WHERE environment_id=?", (self.environment_id,))
            if previous and at - previous["window"] < 60:
                if previous["requests"] >= 60:
                    raise Rejected("Environment request budget exhausted; retry in the next minute")
                self.db.execute("UPDATE budgets SET requests=requests+1 WHERE environment_id=?", (self.environment_id,))
            else:
                self.db.execute("INSERT OR REPLACE INTO budgets VALUES(?,?,?)", (self.environment_id, at, 1))

    def _token(self):
        if self.token and time.time() < self.expires:
            return self.token
        credentials = self.env["credentials"]
        try:
            response = self.http.require_ok(self.http.request("POST", self.env["base_url"] + "/oauth/token",
                before_attempt=self._budget,
                data={"grant_type": "client_credentials", "client_id": credentials["client_id"],
                      "client_secret": credentials["client_secret"]}))
        except Ambiguous:
            raise Rejected("OAuth token request failed before the ISC operation was sent") from None
        payload = response.json()
        if not isinstance(payload.get("access_token"), str):
            raise Rejected("OAuth response did not contain an access token")
        self.token = payload["access_token"]
        self.expires = time.time() + min(max(int(payload.get("expires_in", 300)) - 30, 0), 3600)
        return self.token

    def resolve_secrets(self, value):
        if isinstance(value, dict):
            return {k: self.resolve_secrets(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve_secrets(v) for v in value]
        if isinstance(value, str) and value.startswith("${secrets."):
            match = re.fullmatch(r"\$\{secrets\.([A-Za-z0-9_-]+)\}", value)
            name = match[1] if match else ""
            if name not in self.env["credentials"].get("connector_secrets", {}):
                raise Rejected("A required connector secret is not configured in this environment")
            return self.env["credentials"]["connector_secrets"][name]
        return value

    def call(self, operation_id, arguments, *, mutation_authorized=False):
        if self.env["backend"] != "isc":
            raise Rejected("Live API tools require an ISC environment")
        args = self.resolve_secrets(arguments)
        op = self.catalog.prepare(self.env["spec_id"], operation_id, args)
        if op["method"] != "GET" and not mutation_authorized:
            raise Rejected("Mutation requires an approved workflow execution")
        if op["method"] == "DELETE":
            raise Rejected("Destructive operations are disabled")
        if op["method"] != "GET" and (not self.env["write_enabled"] or operation_id not in self.env["allowed_write_operations"]):
            raise Rejected("Operation is outside this environment's write permissions")
        headers = {"Authorization": "Bearer " + self._token(), "Accept": "application/json"}
        query = {k: str(v).lower() if isinstance(v, bool) else v for k, v in args.get("query", {}).items()}
        request = {"headers": headers, "params": query}
        if op["media"] == "multipart/form-data":
            parts = [(key, (None, str(value))) for key, value in args.get("form", {}).items()]
            parts.extend((key, self.environments.file(self.environment_id, file_id)) for key, file_id in args.get("files", {}).items())
            if parts:
                request["files"] = parts
        elif "body" in args:
            headers["Content-Type"] = op["media"]
            request["json"] = args["body"]
        response = self.http.require_ok(self.http.request(op["method"], self.env["base_url"] + op["resolved_path"], before_attempt=self._budget, **request))
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            error = Rejected if op["method"] == "GET" else Ambiguous
            raise error("ISC returned a non-JSON response; inspect the remote outcome") from None
        try:
            response_contract = op["contract"].get("responses", {}).get(str(response.status_code))
            if response_contract:
                response_contract = self.catalog.deref(op["document"], response_contract)
                schema = response_contract.get("content", {}).get("application/json", {}).get("schema")
                if schema:
                    self.catalog.validate_value(op["document"], schema, payload)
            elif op["contract"].get("responses") and "default" not in op["contract"]["responses"]:
                raise Rejected("Undocumented API response status")
        except Rejected:
            error = Rejected if op["method"] == "GET" else Ambiguous
            raise error("ISC response did not match the pinned contract; inspect the remote outcome") from None
        secret_values = [self.token, *[v for k, v in self.env["credentials"].items() if isinstance(v, str)],
                         *self.env["credentials"].get("connector_secrets", {}).values()]
        return {"status_code": response.status_code, "data": redact(payload, secret_values),
                "state_hash": self.environments.vault.fingerprint(payload),
                "request_id": response.headers.get("X-Request-ID"), "operation_id": operation_id}

    def discover(self):
        operations = {o["operation_id"] for o in self.catalog.operations(self.env["spec_id"], "sources", limit=200)}
        if "listSources" not in operations:
            raise Rejected("Selected specification does not expose listSources")
        result = self.call("listSources", {"query": {"limit": 100}})
        return {"sources": result["data"], "truncated_at": 100,
                "note": "Discovery is a bounded first page, not a complete tenant inventory"}
