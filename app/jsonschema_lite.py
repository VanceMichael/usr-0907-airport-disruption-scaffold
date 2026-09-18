"""A minimal JSON Schema (draft 2020-12 subset) validator.

The service validates incoming events against the shipped contract file
(``contracts/disruption-event.schema.json``) instead of a hand-maintained
copy of the rules, so the contract remains the single source of truth.

Only the keywords the contract actually uses are supported.  Anything else
raises :class:`UnsupportedSchemaError` at startup, so the service fails
loudly rather than silently under-validating if the contract grows new
features.
"""
from __future__ import annotations

import re

SUPPORTED_KEYWORDS = frozenset({
    "$schema", "$id", "$defs", "title", "description",
    "type", "required", "properties", "additionalProperties",
    "pattern", "enum", "const", "minimum", "maximum",
    "minLength", "maxLength", "format",
})

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class UnsupportedSchemaError(Exception):
    """Raised when the contract uses keywords this validator does not implement."""


def check_supported(schema: dict, path: str = "$") -> None:
    """Verify every keyword in the schema is implemented (checked at startup)."""
    if not isinstance(schema, dict):
        raise UnsupportedSchemaError(f"{path}: subschema must be a JSON object")
    unknown = sorted(set(schema) - SUPPORTED_KEYWORDS)
    if unknown:
        raise UnsupportedSchemaError(f"{path}: unsupported keyword(s): {', '.join(unknown)}")
    for name, subschema in schema.get("properties", {}).items():
        check_supported(subschema, f"{path}.properties.{name}")


def _matches(pattern: str, value: str) -> bool:
    # JSON Schema patterns are unanchored, but the contract's patterns are
    # ^...$ anchored.  fullmatch() additionally closes the Python "$"
    # trailing-newline loophole, keeping validation strict.
    if pattern.startswith("^") and pattern.endswith("$"):
        return re.fullmatch(pattern, value) is not None
    return re.search(pattern, value) is not None


def validate(instance, schema: dict, format_checkers: dict, path: str = "") -> list:
    """Return a list of ``{"field", "issue"}`` violations (empty when valid)."""
    errors: list = []

    def err(field: str, issue: str) -> None:
        errors.append({"field": field or "(root)", "issue": issue})

    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_TYPE_CHECKS[name](instance) for name in names):
            err(path, "expected type " + "/".join(names))
            return errors  # further keyword checks would be meaningless

    if "enum" in schema and instance not in schema["enum"]:
        err(path, f"must be one of {schema['enum']}")
    if "const" in schema and instance != schema["const"]:
        err(path, f"must equal {schema['const']!r}")

    if isinstance(instance, str):
        if "pattern" in schema and not _matches(schema["pattern"], instance):
            err(path, f"must match pattern {schema['pattern']}")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            err(path, f"must be at least {schema['minLength']} character(s)")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            err(path, f"must be at most {schema['maxLength']} character(s)")
        fmt = schema.get("format")
        if fmt and fmt in format_checkers:
            try:
                format_checkers[fmt](instance)
            except ValueError as exc:
                err(path, str(exc))

    if isinstance(instance, int) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            err(path, f"must be >= {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            err(path, f"must be <= {schema['maximum']}")

    if isinstance(instance, dict):
        for required in schema.get("required", []):
            if required not in instance:
                err(required, "field is required")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in sorted(set(instance) - set(properties)):
                err(key, "additional property is not allowed")
        for key, subschema in properties.items():
            if key in instance:
                errors.extend(validate(instance[key], subschema, format_checkers, key))

    return errors
