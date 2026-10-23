"""Strict contracts shared by agents, policy, and execution."""
import hashlib
import json
import re


class Rejected(ValueError):
    """An input or operation failed a platform invariant."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate(schema, value, path="$"):
    """Validate the deliberately small JSON Schema subset used by this prototype."""
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool}
    if kind and not isinstance(value, types[kind]):
        raise Rejected(f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise Rejected(f"{path}: unsupported value")
    if kind == "object":
        props = schema.get("properties", {})
        missing = set(schema.get("required", [])) - value.keys()
        extra = value.keys() - props.keys()
        if missing or (schema.get("additionalProperties") is False and extra):
            raise Rejected(f"{path}: missing={sorted(missing)}, unknown={sorted(extra)}")
        for key, item in value.items():
            if key in props:
                validate(props[key], item, f"{path}.{key}")
    if kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 1000):
            raise Rejected(f"{path}: invalid item count")
        for index, item in enumerate(value):
            validate(schema["items"], item, f"{path}[{index}]")
    if kind == "string":
        if not value.strip() or len(value) > schema.get("maxLength", 4000):
            raise Rejected(f"{path}: empty or oversized string")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise Rejected(f"{path}: invalid format")
    return value


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


TEXT = {"type": "string", "maxLength": 200}
ATTRIBUTE = {"type": "string", "pattern": r"[A-Za-z][A-Za-z0-9_]{0,63}"}
SCOPE = obj({"client": ATTRIBUTE, "tenant": ATTRIBUTE,
             "environment": {"type": "string", "enum": ["sandbox", "production"]}})
SPEC = obj({
    "source_name": TEXT,
    "connector": {"type": "string", "enum": ["delimited-file"]},
    "authoritative": {"type": "boolean"},
    "account_id": ATTRIBUTE,
    "correlation_attribute": ATTRIBUTE,
    "attributes": {"type": "array", "minItems": 1, "maxItems": 64, "items": ATTRIBUTE},
    "mappings": {"type": "array", "minItems": 1, "maxItems": 64,
                 "items": obj({"source": ATTRIBUTE, "identity": ATTRIBUTE})},
})
REVIEW = obj({"approved": {"type": "boolean"},
              "findings": {"type": "array", "maxItems": 30,
                           "items": {"type": "string", "maxLength": 1000}}})


def check_spec(spec):
    validate(SPEC, spec)
    attrs = spec["attributes"]
    if len(attrs) != len(set(attrs)):
        raise Rejected("Duplicate source attributes")
    if not {spec["account_id"], spec["correlation_attribute"]} <= set(attrs):
        raise Rejected("Account ID and correlation attribute must be in the source schema")
    targets = [m["identity"] for m in spec["mappings"]]
    if len(targets) != len(set(targets)):
        raise Rejected("Multiple mappings to the same identity attribute")
    if any(m["source"] not in attrs for m in spec["mappings"]):
        raise Rejected("Mapping references an unknown source attribute")
    if not any(m["source"] == spec["correlation_attribute"] for m in spec["mappings"]):
        raise Rejected("Correlation attribute must have an identity mapping")
    return spec
