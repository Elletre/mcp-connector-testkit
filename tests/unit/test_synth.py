"""The argument generator keeps its promise: valid means valid, invalid means invalid.

Every candidate is checked against the schema it came from with a real JSON
Schema validator. If the generator ever produced a "valid" set the schema
rejects, the contract checks built on it would blame the server for the
generator's mistake.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from mcpqa.synth import full_instance, invalid_candidates, minimal_instance, valid_candidates

SCHEMAS: dict[str, dict[str, Any]] = {
    "strings with bounds": {
        "type": "object",
        "properties": {
            "short": {"type": "string", "maxLength": 3},
            "long": {"type": "string", "minLength": 20},
        },
        "required": ["short", "long"],
    },
    "numbers with bounds": {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "minimum": 5},
            "strictly_positive": {"type": "integer", "exclusiveMinimum": 0},
            "at_most_zero": {"type": "integer", "maximum": 0},
            "ratio": {"type": "number"},
        },
        "required": ["page", "strictly_positive", "at_most_zero"],
    },
    "enums, consts, defaults, booleans": {
        "type": "object",
        "properties": {
            "mode": {"enum": ["fast", "slow"]},
            "kind": {"const": "mail"},
            "limit": {"type": "integer", "default": 25},
            "flag": {"type": "boolean"},
        },
        "required": ["mode", "kind"],
        "additionalProperties": False,
    },
    "arrays and nested objects": {
        "type": "object",
        "properties": {
            "to": {"type": "array", "items": {"type": "string"}, "minItems": 2},
            "filters": {
                "type": "object",
                "properties": {"label": {"type": "string"}},
                "required": ["label"],
            },
        },
        "required": ["to", "filters"],
    },
    "nullable and composed": {
        "type": "object",
        "properties": {
            "query": {"type": ["string", "null"]},
            "cursor": {"anyOf": [{"type": "null"}, {"type": "string"}]},
            "nothing": {"type": "null"},
        },
        "required": ["query"],
    },
}


@pytest.mark.parametrize("name", SCHEMAS)
def test_valid_candidates_validate(name: str) -> None:
    schema = SCHEMAS[name]
    validator = Draft202012Validator(schema)
    for candidate in valid_candidates(schema):
        errors = [error.message for error in validator.iter_errors(candidate.arguments)]
        assert not errors, f"{name}: {candidate.reason} {candidate.arguments} -> {errors}"


@pytest.mark.parametrize("name", SCHEMAS)
def test_invalid_candidates_do_not(name: str) -> None:
    schema = SCHEMAS[name]
    validator = Draft202012Validator(schema)
    candidates = invalid_candidates(schema)
    assert candidates, f"{name}: no violating argument set was produced"
    for candidate in candidates:
        assert not validator.is_valid(candidate.arguments), f"{name}: {candidate.reason} was accepted"


def test_minimal_and_full_instances_differ_only_in_optional_properties() -> None:
    schema = SCHEMAS["enums, consts, defaults, booleans"]
    assert set(minimal_instance(schema)) == {"mode", "kind"}
    assert set(full_instance(schema)) == {"mode", "kind", "limit", "flag"}
    assert full_instance(schema)["limit"] == 25, "defaults are used where the schema gives one"


def test_a_schema_without_properties_yields_an_empty_call() -> None:
    assert valid_candidates({"type": "object"})[0].arguments == {}
    assert invalid_candidates({"type": "object"}) == []
