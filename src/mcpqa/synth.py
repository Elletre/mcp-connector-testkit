"""Arguments generated from a tool's own published schema.

Two questions come out of this, and they are different:

* Does the server accept everything its schema says it accepts? If the schema
  says `label` is optional and the code demands it, every client that trusted
  the schema is now broken.
* Does the server reject what its schema forbids, and reject it *properly* —
  as a rejected call, not as a crash, and without doing the work anyway?

The generator stays deliberately dumb: minimal instances and obvious
violations. It is a contract probe, not a fuzzer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PROBE_STRING = "mcpqa-probe"


@dataclass(frozen=True)
class Candidate:
    """One generated argument set, with the reason it exists."""

    arguments: dict[str, Any]
    reason: str


def _value_for(schema: dict[str, Any]) -> Any:
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]

    declared = schema.get("type")
    if isinstance(declared, list):
        declared = next((t for t in declared if t != "null"), None)

    if declared == "string":
        value = PROBE_STRING
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and minimum > len(value):
            value = value + "x" * (minimum - len(value))
        maximum = schema.get("maxLength")
        if isinstance(maximum, int):
            value = value[:maximum]
        return value
    if declared == "integer":
        for key in ("minimum", "exclusiveMinimum"):
            bound = schema.get(key)
            if isinstance(bound, int | float):
                return int(bound) + (1 if key == "exclusiveMinimum" else 0)
        maximum = schema.get("maximum")
        return min(1, int(maximum)) if isinstance(maximum, int | float) else 1
    if declared == "number":
        return 1.0
    if declared == "boolean":
        return True
    if declared == "array":
        items = schema.get("items")
        if isinstance(items, dict) and schema.get("minItems"):
            return [_value_for(items)] * int(schema["minItems"])
        return [_value_for(items)] if isinstance(items, dict) else []
    if declared == "object":
        return minimal_instance(schema)
    if declared == "null":
        return None

    for keyword in ("anyOf", "oneOf", "allOf"):
        options = schema.get(keyword)
        if isinstance(options, list) and options:
            for option in options:
                if isinstance(option, dict) and option.get("type") != "null":
                    return _value_for(option)
    return PROBE_STRING


def minimal_instance(schema: dict[str, Any]) -> dict[str, Any]:
    """The smallest object the schema accepts: required properties only."""
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    return {name: _value_for(properties[name]) for name in required if isinstance(properties.get(name), dict)}


def full_instance(schema: dict[str, Any]) -> dict[str, Any]:
    """Every declared property filled in, required or not."""
    properties = schema.get("properties") or {}
    return {
        name: _value_for(subschema) for name, subschema in properties.items() if isinstance(subschema, dict)
    }


def valid_candidates(schema: dict[str, Any]) -> list[Candidate]:
    candidates = [Candidate(minimal_instance(schema), "required properties only")]
    full = full_instance(schema)
    if full != candidates[0].arguments:
        candidates.append(Candidate(full, "every declared property"))
    return candidates


def invalid_candidates(schema: dict[str, Any]) -> list[Candidate]:
    """Argument sets the published schema forbids."""
    properties = schema.get("properties") or {}
    required = [name for name in (schema.get("required") or []) if name in properties]
    candidates: list[Candidate] = []

    if required:
        dropped = dict(minimal_instance(schema))
        missing = required[0]
        dropped.pop(missing, None)
        candidates.append(Candidate(dropped, f"required property {missing!r} missing"))

    for name, subschema in properties.items():
        if not isinstance(subschema, dict):
            continue
        declared = subschema.get("type")
        if isinstance(declared, list):
            declared = next((t for t in declared if t != "null"), None)
        if declared in ("integer", "number"):
            wrong = {**minimal_instance(schema), name: "not-a-number"}
            candidates.append(Candidate(wrong, f"{name} is a string where a number is declared"))
            break
        if declared == "string":
            wrong = {**minimal_instance(schema), name: 12345}
            candidates.append(Candidate(wrong, f"{name} is a number where a string is declared"))
            break
        if declared == "array":
            wrong = {**minimal_instance(schema), name: "not-an-array"}
            candidates.append(Candidate(wrong, f"{name} is a string where an array is declared"))
            break

    for name, subschema in properties.items():
        if isinstance(subschema, dict) and subschema.get("enum"):
            wrong = {**minimal_instance(schema), name: "mcpqa-not-in-enum"}
            candidates.append(Candidate(wrong, f"{name} is outside its declared enum"))
            break

    if schema.get("additionalProperties") is False:
        extra = {**minimal_instance(schema), "mcpqa_unexpected": PROBE_STRING}
        candidates.append(Candidate(extra, "an undeclared property, where none are allowed"))

    return candidates
