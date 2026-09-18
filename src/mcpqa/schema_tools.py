"""JSON Schema work, in two directions.

Downwards: validate what the server sent against the specification's own
`schema.json` for the protocol version in play.

Upwards: validate the schemas the *server* publishes. A tool whose `inputSchema`
is not a valid JSON Schema breaks clients before the model ever sees it, and
nothing else in the protocol will tell you.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

SCHEMA_DIR = Path(__file__).parent / "schemas"

RESULT_DEFINITIONS = {
    "initialize": "InitializeResult",
    "server/discover": "DiscoverResult",
    "tools/list": "ListToolsResult",
    "tools/call": "CallToolResult",
    "prompts/list": "ListPromptsResult",
    "resources/list": "ListResourcesResult",
}


@lru_cache(maxsize=8)
def spec_schema(version: str) -> dict[str, Any]:
    path = SCHEMA_DIR / f"{version}.json"
    if not path.exists():
        raise FileNotFoundError(f"no vendored schema for protocol version {version}")
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@lru_cache(maxsize=64)
def _definition_validator(version: str, definition: str) -> Draft202012Validator:
    root = spec_schema(version)
    schema = {"$ref": f"#/$defs/{definition}", "$defs": root["$defs"]}
    return Draft202012Validator(schema)


def available_definitions(version: str) -> set[str]:
    return set(spec_schema(version)["$defs"])


def validate_against_spec(version: str, definition: str, payload: Any) -> list[str]:
    """Errors from checking `payload` against one definition of the spec schema."""
    validator = _definition_validator(version, definition)
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    ]


def validate_result(version: str, method: str, result: Any) -> list[str] | None:
    """Validate a result for `method`, or `None` when the spec has no definition."""
    definition = RESULT_DEFINITIONS.get(method)
    if definition is None or definition not in available_definitions(version):
        return None
    return validate_against_spec(version, definition, result)


def check_is_json_schema(schema: Any) -> list[str]:
    """Is this a usable JSON Schema at all?"""
    if not isinstance(schema, dict):
        return [f"schema must be an object, got {type(schema).__name__}"]
    # The specification lets a schema name its own dialect with `$schema` and
    # defaults to 2020-12 when it does not.
    validator = validator_for(schema, default=Draft202012Validator)
    try:
        validator.check_schema(schema)
    except SchemaError as error:
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        return [f"{location}: {error.message}"]
    return []


def validate_instance(schema: dict[str, Any], instance: Any, *, formats: bool = False) -> list[str]:
    """Validate data against a schema the server published."""
    dialect = validator_for(schema, default=Draft202012Validator)
    validator = dialect(schema, format_checker=FormatChecker() if formats else None)
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    ]
