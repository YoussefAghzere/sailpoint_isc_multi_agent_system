"""Official OpenAPI discovery, contract pinning, and strict API argument validation."""
import copy
import json
import math
import re
import time
from urllib.parse import quote

import jsonschema
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT4

from ..models import Rejected, canonical, digest


OFFICIAL_ROOT = "https://raw.githubusercontent.com/sailpoint-oss/api-specs/main/"
METHODS = {"get", "post", "put", "patch", "delete"}


def convert_schema(value):
    """Convert OpenAPI 3.0 nullable semantics to Draft 4 for local validation."""
    if isinstance(value, list):
        return [convert_schema(v) for v in value]
    if not isinstance(value, dict):
        return value
    result = {k: convert_schema(v) for k, v in value.items() if k != "nullable"}
    if value.get("nullable") and isinstance(result.get("type"), str):
        result["type"] = [result["type"], "null"]
    return result


class Catalog:
    def __init__(self, db):
        self.db = db
        self._documents = {}
        self._operation_index = {}

    def sync_latest(self, http):
        workflow = http.require_ok(http.request("GET", OFFICIAL_ROOT + ".github/workflows/build-dereferenced-spec.yml"))
        versions = sorted(set(re.findall(r"sailpoint-api\.(v20\d{2})\.yaml", workflow.text)))
        if not versions:
            raise Rejected("Official release discovery returned no stable versioned specifications")
        version = versions[-1]
        url = OFFICIAL_ROOT + f"dereferenced/deref-sailpoint-api.{version}.json"
        response = http.require_ok(http.request("GET", url, max_bytes=16_000_000))
        return self.install(response.json(), version, url)

    def install(self, spec, version, origin):
        if not re.fullmatch(r"v(?:3|20\d{2}|beta)", version):
            raise Rejected("Unrecognized API version")
        if not isinstance(spec, dict) or not str(spec.get("openapi", "")).startswith("3.") or not spec.get("paths"):
            raise Rejected("A complete OpenAPI 3 specification is required")
        def check_refs(value):
            if isinstance(value, dict):
                if "$ref" in value and not value["$ref"].startswith("#/"):
                    raise Rejected("External schema references are forbidden; import a bundled specification")
                for child in value.values():
                    check_refs(child)
            elif isinstance(value, list):
                for child in value:
                    check_refs(child)
        check_refs(spec)
        seen = set()
        for path, item in spec["paths"].items():
            if not path.startswith("/") or ".." in path or "?" in path or "#" in path:
                raise Rejected("Invalid operation path")
            for method, op in item.items():
                if method in METHODS and op.get("operationId"):
                    if op["operationId"] in seen:
                        raise Rejected("Duplicate API operation ID")
                    seen.add(op["operationId"])
        sha = digest(spec)
        spec_id = version + ":" + sha
        self.db.execute("INSERT OR REPLACE INTO specs VALUES(?,?,?,?,?,?)",
                        (spec_id, version, canonical(spec), sha, origin, time.time()))
        return {"id": spec_id, "version": version, "hash": sha, "operations": len(seen), "origin": origin}

    def list(self):
        return self.db.all("SELECT id,version,hash,origin,fetched FROM specs ORDER BY fetched DESC")

    def get(self, spec_id):
        row = self.db.one("SELECT id,version,hash,origin,fetched FROM specs WHERE id=?", (spec_id,))
        if not row:
            raise Rejected("Import and select an API specification first")
        if spec_id not in self._documents:
            raw = self.db.one("SELECT document FROM specs WHERE id=?", (spec_id,))
            self._documents[spec_id] = json.loads(raw["document"])
            # Bound memory in long-lived ingestion workers.
            if len(self._documents) > 3:
                oldest = next(iter(self._documents))
                self._documents.pop(oldest)
                self._operation_index.pop(oldest, None)
        row["document"] = self._documents[spec_id]
        return row

    @staticmethod
    def deref(spec, value):
        visited = set()
        while isinstance(value, dict) and "$ref" in value:
            ref = value["$ref"]
            if ref in visited or not ref.startswith("#/"):
                raise Rejected("Unresolvable schema reference")
            visited.add(ref)
            value = spec
            for part in ref[2:].split("/"):
                value = value[part.replace("~1", "/").replace("~0", "~")]
        return value

    def operations(self, spec_id, query="", limit=30):
        row = self.get(spec_id)
        def tokens(text):
            separated = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
            words = re.findall(r"[a-z0-9]+", separated.lower())
            return {w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us")) else w
                    for w in words if w not in {"a", "an", "the", "on", "of", "to", "and", "all", "our", "for", "please"}}
        words = tokens(query)
        records = []
        for path, item in row["document"]["paths"].items():
            for method, op in item.items():
                if method not in METHODS or not op.get("operationId"):
                    continue
                fields = [tokens(op.get("summary", "")), tokens(op["operationId"]), tokens(path), tokens(" ".join(op.get("tags", [])))]
                records.append(({"operation_id": op["operationId"], "method": method.upper(), "path": path,
                                "summary": op.get("summary", ""), "deprecated": op.get("deprecated", False),
                                "score": 0}, fields))
        frequencies = {word: sum(any(word in field for field in fields) for _, fields in records) for word in words}
        results = []
        for result, fields in records:
            score = 0
            for word in words:
                weight = math.log(1 + len(records) / (1 + frequencies[word]))
                score += sum(multiplier * weight / (1 + .15 * len(field)) for multiplier, field in zip((3, 3, 1, .3), fields) if word in field)
            if words and not score:
                continue
            if re.sub(r"\W", "", query).lower() == result["operation_id"].lower():
                score += 10
            result["score"] = round(score, 4)
            results.append(result)
        return sorted(results, key=lambda r: (-r["score"], r["operation_id"]))[:limit]

    def operation(self, spec_id, operation_id):
        row = self.get(spec_id)
        spec = row["document"]
        if spec_id not in self._operation_index:
            self._operation_index[spec_id] = {op["operationId"]: (path, method, op, item.get("parameters", []))
                for path, item in spec["paths"].items() for method, op in item.items()
                if method in METHODS and op.get("operationId")}
        found = self._operation_index[spec_id].get(operation_id)
        if not found:
            raise Rejected("Operation is absent from the pinned specification")
        path, method, op, parameters = found
        return {"path": path, "method": method.upper(), "contract": op,
                "parameters": parameters + op.get("parameters", []),
                "version": row["version"], "document": spec}

    def describe(self, spec_id, operation_id):
        op = self.operation(spec_id, operation_id)
        spec = op.pop("document")
        # Include only recursively referenced definitions, bounded to keep model context manageable.
        definitions, pending = {}, [op]
        while pending and len(definitions) < 40:
            value = pending.pop()
            if isinstance(value, dict):
                ref = value.get("$ref")
                if ref and ref not in definitions:
                    definition = self.deref(spec, {"$ref": ref})
                    definitions[ref] = definition
                    pending.append(definition)
                pending.extend(v for k, v in value.items() if k != "example" and k != "examples")
            elif isinstance(value, list):
                pending.extend(value)
        return {**op, "definitions": definitions}

    def validate_value(self, spec, schema, value):
        converted = convert_schema(spec)
        registry = Registry().with_resource("urn:isc:spec", Resource.from_contents(converted, default_specification=DRAFT4))
        def absolute(value):
            if isinstance(value, dict):
                return {k: "urn:isc:spec" + v if k == "$ref" and v.startswith("#") else absolute(v)
                        for k, v in value.items()}
            if isinstance(value, list):
                return [absolute(v) for v in value]
            return value
        selected = absolute(convert_schema(schema))
        try:
            jsonschema.Draft4Validator(selected, registry=registry).validate(value)
        except (jsonschema.ValidationError, jsonschema.SchemaError):
            raise Rejected("Arguments do not satisfy the pinned OpenAPI schema") from None

    def prepare(self, spec_id, operation_id, arguments, *, template=False):
        op = self.operation(spec_id, operation_id)
        if op["contract"].get("deprecated"):
            raise Rejected("Deprecated operations are disabled")
        if not isinstance(arguments, dict) or set(arguments) - {"path", "query", "body", "form", "files"}:
            raise Rejected("Unknown API argument fields")
        path_values, query_values = arguments.get("path", {}), arguments.get("query", {})
        if not isinstance(path_values, dict) or not isinstance(query_values, dict):
            raise Rejected("Path and query arguments must be objects")
        known = {"path": set(), "query": set()}
        path = op["path"]
        def check(schema, value):
            if not template or "${" not in canonical(value):
                self.validate_value(op["document"], schema, value)
        for parameter in op["parameters"]:
            p = self.deref(op["document"], parameter)
            location = p.get("in")
            if location not in known:
                if p.get("required"):
                    raise Rejected("Required custom header/cookie parameters are not supported")
                continue
            key = p["name"]
            known[location].add(key)
            values = path_values if location == "path" else query_values
            if p.get("required") and key not in values:
                raise Rejected(f"Missing required {location} parameter: {key}")
            if key in values:
                check(p.get("schema", {}), values[key])
                if location == "path":
                    if isinstance(values[key], (dict, list)):
                        raise Rejected("Complex path parameters are unsupported")
                    encoded = quote(str(values[key]), safe="")
                    if str(values[key]) in {".", ".."} or any(c in str(values[key]) for c in "/\\%?#\r\n"):
                        raise Rejected("Path traversal is forbidden")
                    path = path.replace("{" + key + "}", encoded)
        if set(path_values) - known["path"] or set(query_values) - known["query"] or "{" in path:
            raise Rejected("Unknown or unresolved path/query parameters")
        request = self.deref(op["document"], op["contract"].get("requestBody", {}))
        content = request.get("content", {})
        media = next((x for x in content if x in {"application/json", "application/json-patch+json"}), None)
        if media:
            if "body" in arguments:
                check(content[media].get("schema", {}), arguments["body"])
            elif request.get("required"):
                raise Rejected("This operation requires a request body")
            if arguments.get("form") or arguments.get("files"):
                raise Rejected("Operation expects JSON rather than multipart data")
        elif "multipart/form-data" in content:
            media = "multipart/form-data"
            if "body" in arguments:
                raise Rejected("Multipart operation cannot receive a JSON body")
            form = {**arguments.get("form", {}), **{k: "binary-file" for k in arguments.get("files", {})}}
            check(content[media].get("schema", {}), form)
        elif "body" in arguments or arguments.get("form") or arguments.get("files"):
            raise Rejected("Unsupported request content type or unexpected request body")
        return {**op, "resolved_path": "/" + op["version"] + path, "media": media}
