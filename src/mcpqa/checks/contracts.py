"""Layer 2, part one: does the tool behave like its published contract?

A tool's schema is a promise to every client that reads it. These checks call
the tool with arguments its own schema says are fine, and with arguments its
own schema forbids, and look at which of the two the server actually honours.
"""

from __future__ import annotations

from typing import Any

from ..protocol import INTERNAL_ERROR, INVALID_PARAMS
from ..schema_tools import validate_instance
from ..synth import invalid_candidates, valid_candidates
from .model import Ctx, Outcome, failed, passed, register, skipped


def _safe_tools(ctx: Ctx) -> list[dict[str, Any]]:
    """Tools the kit may call: read-only ones, or everything if allowed to mutate.

    The kit believes `readOnlyHint` here — not because annotations are
    trustworthy (layer 2 exists because they are not) but because pointing a
    conformance tool at a real account should not send anyone's mail. When a
    tool lies about being read-only, A-001 is what notices.
    """
    tools = ctx.tools()
    if ctx.profile.allow_mutations:
        return tools
    return [tool for tool in tools if (tool.get("annotations") or {}).get("readOnlyHint") is True]


def _arguments(ctx: Ctx, tool: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    name = str(tool.get("name"))
    samples = ctx.profile.safe_arguments_for(name)
    if samples:
        return [(arguments, "sample arguments") for arguments in samples]
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        return []
    return [(candidate.arguments, candidate.reason) for candidate in valid_candidates(schema)]


@register(
    id="C-001",
    title="Arguments the schema allows are accepted",
    layer=2,
    severity="error",
)
def valid_arguments_accepted(ctx: Ctx) -> Outcome:
    tools = _safe_tools(ctx)
    if not tools:
        return skipped("no tool is safe to call (none is annotated read-only)")
    problems: list[str] = []
    called = 0
    for tool in tools:
        name = str(tool.get("name"))
        for arguments, reason in _arguments(ctx, tool):
            exchange = ctx.session.call_tool(name, arguments)
            called += 1
            code = exchange.error_code
            if code in (INVALID_PARAMS, INTERNAL_ERROR):
                problems.append(
                    f"{name} rejected {reason} ({sorted(arguments)}) with {code}: "
                    f"{(exchange.error or {}).get('message')!r}"
                )
    if problems:
        return failed("; ".join(problems[:4]), ctx.evidence())
    return passed(f"{called} call(s) across {len(tools)} tool(s) accepted as published")


@register(
    id="C-002",
    title="Arguments the schema forbids are refused, not crashed on",
    layer=2,
    severity="error",
)
def invalid_arguments_refused(ctx: Ctx) -> Outcome:
    tools = _safe_tools(ctx)
    if not tools:
        return skipped("no tool is safe to call (none is annotated read-only)")
    oracle = ctx.profile.oracle
    problems: list[str] = []
    tried = 0
    for tool in tools:
        name = str(tool.get("name"))
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            continue
        for candidate in invalid_candidates(schema):
            before = oracle.snapshot() if oracle is not None else None
            exchange = ctx.session.call_tool(name, candidate.arguments)
            tried += 1
            if exchange.error_code == INTERNAL_ERROR:
                problems.append(f"{name}: {candidate.reason} produced -32603 (internal error)")
                continue
            rejected = exchange.error is not None or exchange.is_tool_error()
            if not rejected:
                problems.append(f"{name}: {candidate.reason} was accepted as a successful call")
            if before is not None:
                after = oracle.snapshot()
                if after != before:
                    problems.append(f"{name}: {candidate.reason} changed state before being rejected")
    if not tried:
        return skipped("no schema produced a violating argument set")
    if problems:
        return failed("; ".join(problems[:4]), ctx.evidence())
    return passed(f"{tried} invalid argument set(s) refused cleanly")


@register(
    id="C-003",
    title="structuredContent matches the published outputSchema",
    layer=2,
    severity="error",
)
def structured_output_matches(ctx: Ctx) -> Outcome:
    tools = [tool for tool in _safe_tools(ctx) if isinstance(tool.get("outputSchema"), dict)]
    if not tools:
        return skipped("no safe tool publishes an outputSchema")
    problems: list[str] = []
    validated = 0
    for tool in tools:
        name = str(tool.get("name"))
        schema = tool["outputSchema"]
        for arguments, reason in _arguments(ctx, tool):
            exchange = ctx.session.call_tool(name, arguments)
            result = exchange.result
            if result is None or result.get("isError"):
                continue
            if "structuredContent" not in result:
                problems.append(f"{name}: declares an outputSchema but returned no structuredContent")
                continue
            errors = validate_instance(schema, result["structuredContent"])
            validated += 1
            if errors:
                problems.append(f"{name} ({reason}): {errors[0]}")
    if problems:
        return failed("; ".join(problems[:4]), ctx.evidence())
    if not validated:
        return skipped("no successful call produced structured content to validate")
    return passed(f"{validated} structured result(s) conform to their schema")


@register(
    id="C-004",
    title="Structured results also come back as text",
    layer=2,
    severity="warning",
)
def structured_results_have_text(ctx: Ctx) -> Outcome:
    tools = [tool for tool in _safe_tools(ctx) if isinstance(tool.get("outputSchema"), dict)]
    if not tools:
        return skipped("no safe tool publishes an outputSchema")
    missing: list[str] = []
    for tool in tools:
        name = str(tool.get("name"))
        arguments = _arguments(ctx, tool)
        if not arguments:
            continue
        result = ctx.session.call_tool(name, arguments[0][0]).result or {}
        if result.get("isError") or "structuredContent" not in result:
            continue
        blocks = result.get("content") or []
        if not any(block.get("type") == "text" and block.get("text") for block in blocks):
            missing.append(name)
    if missing:
        return failed(f"structured content without a text block: {missing}")
    return passed("structured results carry their serialized form too")


@register(
    id="C-005",
    title="Failed calls say something a model can act on",
    layer=2,
    severity="warning",
)
def errors_are_actionable(ctx: Ctx) -> Outcome:
    tools = _safe_tools(ctx)
    if not tools:
        return skipped("no tool is safe to call (none is annotated read-only)")
    empty: list[str] = []
    seen = 0
    for tool in tools:
        name = str(tool.get("name"))
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            continue
        for candidate in invalid_candidates(schema)[:1]:
            result = ctx.session.call_tool(name, candidate.arguments).result
            if result is None or not result.get("isError"):
                continue
            seen += 1
            text = " ".join(
                str(block.get("text", ""))
                for block in result.get("content", [])
                if block.get("type") == "text"
            ).strip()
            if len(text) < 10:
                empty.append(f"{name}: {text!r}")
    if empty:
        return failed(f"error results with no usable message: {empty[:3]}")
    if not seen:
        return skipped("no tool returned an execution error to inspect")
    return passed(f"{seen} error result(s) carry a readable message")
