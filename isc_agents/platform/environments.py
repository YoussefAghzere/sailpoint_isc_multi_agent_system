import base64
import csv
import io
import json
import re
import uuid

from ..models import Rejected, canonical, digest
from .catalog import Catalog
from .security import tenant_url, reject_inline_secrets


class Environments:
    def __init__(self, db, vault):
        self.db, self.vault = db, vault

    def get(self, environment_id, *, credentials=False):
        row = self.db.one("SELECT * FROM environments WHERE id=?", (environment_id,))
        if not row:
            raise Rejected("Environment not found")
        config = json.loads(row["config"])
        result = {"id": row["id"], **config, "revision": row["revision"], "has_credentials": bool(row["credentials"])}
        if credentials:
            result["credentials"] = self.vault.decrypt(row["credentials"]) if row["credentials"] else {}
        return result

    def list(self):
        return [self.get(row["id"]) for row in self.db.all("SELECT id FROM environments ORDER BY id")]

    def save(self, config, actor, environment_id=None):
        known = {"name", "client", "stage", "backend", "base_url", "client_id", "client_secret",
                 "connector_secrets", "spec_id", "write_enabled", "allowed_write_operations"}
        if set(config) - known:
            raise Rejected("Unknown environment fields")
        if not isinstance(config.get("name"), str) or not 1 <= len(config["name"]) <= 200:
            raise Rejected("Environment name is required and must contain at most 200 characters")
        if not isinstance(config.get("client"), str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", config["client"]):
            raise Rejected("Client identifier must start with a letter and contain only letters, digits, underscores or hyphens (no spaces). Example: magellan_lab")
        for field in ("stage", "backend", "base_url", "client_id", "client_secret", "spec_id"):
            if field in config and (not isinstance(config[field], str) or len(config[field]) > 8192):
                raise Rejected("Environment fields must contain bounded text values")
        if config.get("stage") not in {"sandbox", "production"} or config.get("backend") not in {"simulator", "isc"}:
            raise Rejected("Choose a supported stage and backend")
        if not isinstance(config.get("write_enabled", False), bool):
            raise Rejected("Write permission must be a boolean")
        previous = self.get(environment_id, credentials=True) if environment_id else None
        secrets = dict(previous["credentials"]) if previous else {}
        for key in ("client_id", "client_secret"):
            if config.get(key):
                secrets[key] = config[key]
        if config.get("connector_secrets") is not None:
            if not isinstance(config["connector_secrets"], dict) or any(
                not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", k) or not isinstance(v, str)
                for k, v in config["connector_secrets"].items()):
                raise Rejected("Connector secrets must be a map of named strings")
            secrets["connector_secrets"] = config["connector_secrets"]
        public = {k: v for k, v in config.items() if k not in {"client_id", "client_secret", "connector_secrets"}}
        public.setdefault("write_enabled", False)
        public.setdefault("allowed_write_operations", [])
        if not isinstance(public["allowed_write_operations"], list) or any(not isinstance(x, str) for x in public["allowed_write_operations"]):
            raise Rejected("Allowed write operations must be a list of operation IDs")
        if public["backend"] == "isc":
            public["base_url"] = tenant_url(public.get("base_url", ""))
            if not secrets.get("client_id") or not secrets.get("client_secret"):
                raise Rejected("Client ID and secret are required for a live environment")
            if not public.get("spec_id"):
                raise Rejected("Select an imported API specification")
            catalog = Catalog(self.db)
            spec = catalog.get(public["spec_id"])
            if spec["version"] == "vbeta" or "beta" in spec["version"]:
                raise Rejected("Preview API versions are not enabled")
            for operation_id in public["allowed_write_operations"]:
                operation = catalog.operation(public["spec_id"], operation_id)
                if operation["method"] in {"GET", "DELETE"} or operation["contract"].get("deprecated"):
                    raise Rejected("Allow only nondeprecated mutation operations other than DELETE")
        else:
            public["base_url"] = ""
            secrets = {}
        environment_id = environment_id or uuid.uuid4().hex
        revision = uuid.uuid4().hex
        with self.db.tx():
            self.db.execute("INSERT OR REPLACE INTO environments VALUES(?,?,?,?)",
                            (environment_id, canonical(public), self.vault.encrypt(secrets) if secrets else None, revision))
            self.db.audit(actor, environment_id, "environment.saved", {"revision": revision, "backend": public["backend"]})
        return self.get(environment_id)

    def attach(self, environment_id, name, encoded, actor):
        self.get(environment_id)
        if not re.fullmatch(r"[A-Za-z0-9_. -]{1,120}", name) or not name.lower().endswith(".csv"):
            raise Rejected("Only CSV attachments with plain filenames are supported")
        try:
            content = base64.b64decode(encoded, validate=True)
            content.decode("utf-8-sig")
        except (ValueError, UnicodeDecodeError):
            raise Rejected("Attachment must be a base64-encoded UTF-8 CSV") from None
        if not 1 <= len(content) <= 5_000_000:
            raise Rejected("CSV attachments must be at most 5 MB")
        artifact_id = uuid.uuid4().hex
        with self.db.tx():
            self.db.execute("INSERT INTO attachments VALUES(?,?,?,?,?,?)",
                (artifact_id, environment_id, name, self.vault.encrypt({"base64": encoded}), digest(encoded), len(content)))
            self.db.audit(actor, environment_id, "attachment.saved", {"id": artifact_id, "bytes": len(content)})
        return {"id": artifact_id, "name": name, "bytes": len(content)}

    def file(self, environment_id, artifact_id):
        row = self.db.one("SELECT * FROM attachments WHERE id=? AND environment_id=?", (artifact_id, environment_id))
        if not row:
            raise Rejected("Attachment not found in the selected environment")
        return (row["name"], base64.b64decode(self.vault.decrypt(row["content"])["base64"]), "text/csv")

    def validate_csv(self, environment_id, artifact_id, account_id, required_attributes=()):
        if not isinstance(account_id, str) or not isinstance(required_attributes, (list, tuple)) or len(required_attributes) > 64 or any(not isinstance(k, str) for k in required_attributes):
            raise Rejected("CSV validation needs an account identifier and a list of required columns")
        content = self.file(environment_id, artifact_id)[1].decode("utf-8-sig")
        try:
            reader = csv.DictReader(io.StringIO(content))
            headers = reader.fieldnames or []
            if not account_id or account_id not in headers or len(headers) != len(set(headers)):
                raise Rejected("CSV needs unique headers and the selected account identifier column")
            missing = sorted(set(required_attributes) - set(headers))
            if missing:
                return {"valid": False, "missing_columns": missing}
            seen, duplicate_rows, empty_rows, malformed_rows, required_rows = set(), [], [], [], []
            row_count = 0
            for ordinal, row in enumerate(reader, start=2):
                row_count += 1
                if row_count > 100000:
                    raise Rejected("CSV validation row budget exceeded")
                if None in row or any(v is None for v in row.values()):
                    malformed_rows.append(ordinal)
                identifier = (row.get(account_id) or "").strip()
                if not identifier:
                    empty_rows.append(ordinal)
                elif identifier in seen:
                    duplicate_rows.append(ordinal)
                seen.add(identifier)
                if any(not (row.get(key) or "").strip() for key in required_attributes):
                    required_rows.append(ordinal)
        except csv.Error:
            raise Rejected("Malformed CSV data") from None
        return {"valid": row_count > 0 and not any((duplicate_rows, empty_rows, malformed_rows, required_rows)),
                "rows": row_count, "columns": headers, "duplicate_id_rows": duplicate_rows[:20],
                "empty_id_rows": empty_rows[:20], "malformed_rows": malformed_rows[:20],
                "missing_required_value_rows": required_rows[:20], "pii_values_returned": False}
