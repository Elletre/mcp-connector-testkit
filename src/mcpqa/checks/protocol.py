"""Layer 1: does this thing speak the protocol?

Nothing here knows or cares what the server is for. These are the checks that
apply to a weather server and a mailbox connector alike, and they are the ones
most likely to be the only tests a connector has.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    META_CLIENT_CAPABILITIES,
    META_PROTOCOL_VERSION,
    META_SERVER_INFO,
    METHOD_NOT_FOUND,
    UNSUPPORTED_PROTOCOL_VERSION,
)
from ..schema_tools import check_is_json_schema, validate_result
from ..session import Session
from ..wire.stdio import StdioWire, WireTimeout
from .model import Ctx, Outcome, failed, passed, register, skipped

TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


@register(
    id="P-001",
    title="Responses echo the request id",
    layer=1,
    severity="error",
)
def responses_echo_ids(ctx: Ctx) -> Outcome:
    problems: list[str] = []
    for request_id in (ctx.session.next_id(), "mcpqa-string-id"):
        exchange = ctx.session.call("tools/list", request_id=request_id)
        if exchange.response is None:
            problems.append(f"id {request_id!r}: no JSON-RPC response at all")
            continue
        if exchange.response.get("jsonrpc") != "2.0":
            problems.append(f"id {request_id!r}: jsonrpc field is {exchange.response.get('jsonrpc')!r}")
        if exchange.response.get("id") != request_id:
            problems.append(f"sent id {request_id!r}, got {exchange.response.get('id')!r}")
    if problems:
        return failed("; ".join(problems), ctx.evidence())
    return passed("integer and string ids both echoed correctly")


@register(
    id="P-002",
    title="Results match the specification's schema",
    layer=1,
    severity="error",
)
def results_match_schema(ctx: Ctx) -> Outcome:
    version = ctx.session.protocol_version
    problems: list[str] = []
    checked: list[str] = []

    exchanges = [("tools/list", ctx.session.call("tools/list"))]
    tools = ctx.tools()
    if tools:
        name = tools[0]["name"]
        arguments = (ctx.profile.safe_arguments_for(name) or [{}])[0]
        exchanges.append(("tools/call", ctx.session.call_tool(name, arguments)))

    for method, exchange in exchanges:
        result = exchange.result
        if result is None:
            problems.append(f"{method}: no result ({exchange.raw_response[:200]})")
            continue
        errors = validate_result(version, method, result)
        if errors is None:
            continue
        checked.append(method)
        problems.extend(f"{method}: {error}" for error in errors[:4])

    if problems:
        return failed("; ".join(problems), ctx.evidence())
    return passed(f"validated against {version} schema.json: {', '.join(checked) or 'nothing'}")


@register(
    id="P-003",
    title="An unknown method is answered with -32601",
    layer=1,
    severity="error",
)
def unknown_method(ctx: Ctx) -> Outcome:
    exchange = ctx.session.call("mcpqa/does-not-exist")
    if exchange.error_code == METHOD_NOT_FOUND:
        return passed("answered -32601")
    if exchange.error is not None:
        return failed(
            f"expected -32601, got {exchange.error_code} ({exchange.error.get('message')!r})",
            ctx.evidence(),
        )
    return failed("an unknown method produced a result instead of an error", ctx.evidence())


@register(
    id="P-004",
    title="Tool names follow the naming rules",
    layer=1,
    severity="warning",
)
def tool_names(ctx: Ctx) -> Outcome:
    names = [str(tool.get("name", "")) for tool in ctx.tools()]
    if not names:
        return skipped("the server published no tools")
    bad = [name for name in names if not TOOL_NAME_PATTERN.match(name)]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    problems = []
    if bad:
        problems.append(f"names outside the allowed character set or length: {bad}")
    if duplicates:
        problems.append(f"duplicate names: {duplicates}")
    if problems:
        return failed("; ".join(problems))
    return passed(f"{len(names)} tool names are well formed and unique")


@register(
    id="P-005",
    title="Every inputSchema is a valid JSON Schema object",
    layer=1,
    severity="error",
)
def input_schemas_valid(ctx: Ctx) -> Outcome:
    tools = ctx.tools()
    if not tools:
        return skipped("the server published no tools")
    problems: list[str] = []
    for tool in tools:
        name = tool.get("name")
        schema = tool.get("inputSchema")
        errors = check_is_json_schema(schema)
        if errors:
            problems.append(f"{name}: {errors[0]}")
            continue
        assert isinstance(schema, dict)
        if schema.get("type") != "object":
            problems.append(f"{name}: root type is {schema.get('type')!r}, expected 'object'")
    if problems:
        return failed("; ".join(problems[:5]))
    return passed(f"{len(tools)} input schemas are valid JSON Schema")


@register(
    id="P-006",
    title="Every outputSchema is a valid JSON Schema",
    layer=1,
    severity="error",
)
def output_schemas_valid(ctx: Ctx) -> Outcome:
    declared = [tool for tool in ctx.tools() if tool.get("outputSchema") is not None]
    if not declared:
        return skipped("no tool publishes an outputSchema")
    problems = []
    for tool in declared:
        errors = check_is_json_schema(tool["outputSchema"])
        if errors:
            problems.append(f"{tool.get('name')}: {errors[0]}")
    if problems:
        return failed("; ".join(problems[:5]))
    return passed(f"{len(declared)} output schemas are valid JSON Schema")


@register(
    id="P-007",
    title="tools/list is returned in a stable order",
    layer=1,
    severity="warning",
)
def stable_tool_order(ctx: Ctx) -> Outcome:
    """Same order twice on this connection, and the same order on a new one.

    The second half matters more than it looks: a server that registers its
    tools from a set, or from plugin discovery, is stable within a process and
    reshuffled by every restart — which is when clients rebuild their prompt
    caches.
    """
    first = [str(tool.get("name")) for tool in ctx.tools(refresh=True)]
    second = [str(tool.get("name")) for tool in ctx.tools(refresh=True)]
    if not first:
        return skipped("the server published no tools")
    if first != second:
        return failed(f"order changed between two calls: {first} then {second}", ctx.evidence())
    other = ctx.fresh_session()
    try:
        third = [str(tool.get("name")) for tool in other.list_tools()]
    finally:
        other.close()
    if sorted(third) == sorted(first) and third != first:
        return failed(f"a second connection listed the same tools in another order: {first} then {third}")
    return passed("the same order on repeat calls and on a second connection")


@register(
    id="P-008",
    title="The tool set does not vary per connection",
    layer=1,
    severity="error",
)
def tools_stable_across_connections(ctx: Ctx) -> Outcome:
    here = sorted(str(tool.get("name")) for tool in ctx.tools())
    other = ctx.fresh_session()
    try:
        there = sorted(str(tool.get("name")) for tool in other.list_tools())
    finally:
        other.close()
    if here != there:
        return failed(f"a second connection with the same credentials saw {there}, this one saw {here}")
    if not here:
        return skipped("the server published no tools")
    order_here = [str(tool.get("name")) for tool in ctx.tools()]
    return passed(f"both connections list the same {len(order_here)} tools")


@register(
    id="P-009",
    title="Results identify the server in _meta",
    layer=1,
    severity="warning",
    eras=("stateless",),
)
def server_info_in_results(ctx: Ctx) -> Outcome:
    result = ctx.session.call("tools/list").result or {}
    meta = result.get("_meta") or {}
    if META_SERVER_INFO in meta:
        return passed(f"serverInfo present: {meta[META_SERVER_INFO]}")
    return failed("tools/list result carries no io.modelcontextprotocol/serverInfo", ctx.evidence())


@register(
    id="P-010",
    title="server/discover is implemented",
    layer=1,
    severity="error",
    eras=("stateless",),
)
def discover_implemented(ctx: Ctx) -> Outcome:
    exchange = ctx.session.discover()
    result = exchange.result
    if result is None:
        return failed(
            f"server/discover returned an error: {exchange.error}",
            exchange.describe(),
        )
    errors = validate_result(ctx.session.protocol_version, "server/discover", result)
    if errors:
        return failed("; ".join(errors[:4]), exchange.describe())
    return passed(f"supportedVersions={result.get('supportedVersions')}")


@register(
    id="P-011",
    title="An unsupported protocol version is refused with -32022",
    layer=1,
    severity="error",
    eras=("stateless",),
)
def unsupported_version_refused(ctx: Ctx) -> Outcome:
    bogus = "1999-01-01"
    params = {
        "_meta": {
            META_PROTOCOL_VERSION: bogus,
            META_CLIENT_CAPABILITIES: {},
        }
    }
    headers = {"MCP-Protocol-Version": bogus, "Mcp-Method": "tools/list"}
    exchange = ctx.session.call(
        "tools/list", params, envelope=False, headers=headers if ctx.transport == "http" else None
    )
    if exchange.error_code != UNSUPPORTED_PROTOCOL_VERSION:
        return failed(
            f"expected -32022, got {exchange.error_code or 'a result'}",
            exchange.describe(),
        )
    data = (exchange.error or {}).get("data") or {}
    missing = [key for key in ("supported", "requested") if key not in data]
    if missing:
        return failed(f"-32022 error data is missing {missing}", exchange.describe())
    if exchange.status is not None and exchange.status != 400:
        return failed(f"HTTP status was {exchange.status}, expected 400", exchange.describe())
    return passed(f"refused with supported={data.get('supported')}")


@register(
    id="P-012",
    title="A request without the per-request envelope is refused with -32602",
    layer=1,
    severity="error",
    eras=("stateless",),
)
def envelope_required(ctx: Ctx) -> Outcome:
    exchange = ctx.session.call("tools/list", {}, envelope=False)
    if exchange.error_code != INVALID_PARAMS:
        return failed(
            f"expected -32602 for a request with no _meta envelope, got {exchange.error_code or 'a result'}",
            exchange.describe(),
        )
    if exchange.status is not None and exchange.status != 400:
        return failed(f"HTTP status was {exchange.status}, expected 400", exchange.describe())
    return passed("refused with -32602")


@register(
    id="P-013",
    title="initialize negotiates a version the server supports",
    layer=1,
    severity="error",
    eras=("handshake",),
    isolation="fresh",
)
def initialize_negotiates(ctx: Ctx) -> Outcome:
    session = Session(target=ctx.target, protocol_version=ctx.session.protocol_version)
    session.wire.start()
    try:
        exchange = session.initialize(version="1999-01-01")
        result = exchange.result
        if result is None:
            error = exchange.error or {}
            if error.get("code") == UNSUPPORTED_PROTOCOL_VERSION:
                return passed("answered -32022 with its supported versions")
            return failed(f"initialize with an unknown version returned {error}", exchange.describe())
        answered = result.get("protocolVersion")
        if answered == "1999-01-01":
            return failed("the server echoed a version it cannot support", exchange.describe())
        if not isinstance(answered, str) or not answered:
            return failed(f"no usable protocolVersion in the result: {answered!r}", exchange.describe())
        return passed(f"countered with {answered}")
    finally:
        session.close()


@register(
    id="P-014",
    title="stdout carries nothing but MCP messages",
    layer=1,
    severity="error",
    transports=("stdio",),
)
def stdout_is_clean(ctx: Ctx) -> Outcome:
    ctx.session.call("tools/list")
    tools = ctx.tools()
    if tools:
        name = tools[0]["name"]
        ctx.session.call_tool(name, (ctx.profile.safe_arguments_for(name) or [{}])[0])
    violations = ctx.session.transcript.stdout_violations
    if violations:
        return failed(
            f"{len(violations)} non-JSON line(s) on stdout, first: {violations[0][:160]!r}",
            "\n".join(violations[:3]),
        )
    return passed("no stray output on the message stream")


@register(
    id="P-015",
    title="The server exits when stdin is closed",
    layer=1,
    severity="warning",
    transports=("stdio",),
    isolation="fresh",
)
def exits_on_stdin_close(ctx: Ctx) -> Outcome:
    session = Session(target=ctx.target, protocol_version=ctx.session.protocol_version)
    session.open()
    session.call("tools/list")
    started = time.monotonic()
    wire = session.wire
    assert isinstance(wire, StdioWire)
    code = wire.close(timeout=5.0)
    elapsed = time.monotonic() - started
    if code is None:
        return failed("the process did not exit after stdin was closed")
    if elapsed > 4.5:
        return failed(f"the process needed {elapsed:.1f}s to exit after stdin closed")
    return passed(f"exited with code {code} after {elapsed:.2f}s")


@register(
    id="P-016",
    title="A malformed message does not take the server down",
    layer=1,
    severity="warning",
    transports=("stdio",),
    isolation="fresh",
)
def survives_malformed_input(ctx: Ctx) -> Outcome:
    session = Session(target=ctx.target, protocol_version=ctx.session.protocol_version)
    session.open()
    wire = session.wire
    assert isinstance(wire, StdioWire)
    try:
        wire.send_raw("this is not json\n")
        time.sleep(0.2)
        try:
            exchange = session.call("tools/list", timeout=8.0)
        except WireTimeout:
            return failed("the server stopped answering after one malformed line")
        if exchange.result is None:
            return failed(
                f"the next request after malformed input failed: {exchange.error}",
                exchange.describe(),
            )
        return passed("kept serving after a malformed line")
    finally:
        session.close()


@register(
    id="P-017",
    title="Requests in flight together are answered correctly",
    layer=1,
    severity="error",
    transports=("stdio",),
)
def concurrent_requests(ctx: Ctx) -> Outcome:
    session = ctx.session
    wire = session.wire
    assert isinstance(wire, StdioWire)
    ids = [session.next_id() for _ in range(5)]
    for request_id in ids:
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}
        params = session.params(None)
        if params is not None:
            body["params"] = params
        wire.send(body)
    for request_id in ids:
        try:
            response = wire.await_response(request_id)
        except WireTimeout as error:
            return failed(str(error))
        if response.get("id") != request_id:
            return failed(f"response carried id {response.get('id')!r}, expected {request_id!r}")
        if "result" not in response:
            return failed(f"id {request_id!r} came back as an error: {response.get('error')}")
    return passed(f"{len(ids)} concurrent requests answered, ids intact")


@register(
    id="P-018",
    title="A slow call does not block the connection",
    layer=1,
    severity="warning",
    transports=("stdio",),
)
def slow_call_does_not_block(ctx: Ctx) -> Outcome:
    """Send a slow tool call, then a cheap request, and time the cheap one.

    `tools/list` does no upstream work, so on a server that handles requests
    concurrently it comes back in milliseconds even while a tool call is still
    running. If it has to wait for the tool call to finish, something is holding
    the event loop — nearly always a synchronous call inside an async handler.

    With fault control the tool call is made slow on purpose; without it, the
    check uses whichever sample call is naturally slow enough to tell, and
    skips if none is.
    """
    published = {str(tool.get("name")) for tool in ctx.tools()}
    candidates = [
        (name, arguments)
        for name in ctx.profile.samples
        if name in published
        for arguments in ctx.profile.safe_arguments_for(name)
    ]
    if not candidates:
        return skipped("needs a tool call with sample arguments")

    session = ctx.session
    wire = session.wire
    assert isinstance(wire, StdioWire)
    faults = ctx.profile.faults
    if faults is not None:
        faults.clear()
        faults.delay(path=".*", ms=1200)
    session.call("tools/list")  # warm the connection before timing anything

    try:
        for name, arguments in candidates[:4]:
            slow_id = session.next_id()
            wire.send(
                {
                    "jsonrpc": "2.0",
                    "id": slow_id,
                    "method": "tools/call",
                    "params": session.params({"name": name, "arguments": arguments}),
                }
            )
            slow_sent = time.monotonic()
            time.sleep(0.1)
            probe_id = session.next_id()
            probe: dict[str, Any] = {"jsonrpc": "2.0", "id": probe_id, "method": "tools/list"}
            envelope = session.params(None)
            if envelope is not None:
                probe["params"] = envelope
            wire.send(probe)
            probe_sent = time.monotonic()
            wire.await_response(probe_id, timeout=30.0)
            probe_took = time.monotonic() - probe_sent
            wire.await_response(slow_id, timeout=30.0)
            slow_took = time.monotonic() - slow_sent

            if slow_took < 0.5:
                continue  # finished before the probe could tell us anything
            if probe_took > 0.3 and probe_took > 0.5 * (slow_took - 0.1):
                return failed(
                    f"tools/list waited {probe_took:.2f}s behind a {name} call that took "
                    f"{slow_took:.2f}s — requests on this connection do not run concurrently",
                )
            return passed(
                f"tools/list answered in {probe_took * 1000:.0f} ms while {name} ran for {slow_took:.2f}s"
            )
    except WireTimeout as error:
        return failed(str(error))
    finally:
        if faults is not None:
            faults.clear()
    return skipped("no sample call ran long enough to tell (pass fault control to slow one down)")


@register(
    id="P-019",
    title="The tools capability is declared",
    layer=1,
    severity="error",
)
def tools_capability_declared(ctx: Ctx) -> Outcome:
    if not ctx.tools():
        return skipped("the server published no tools")
    if ctx.era == "stateless":
        result = ctx.session.discover().result or {}
    else:
        result = ctx.session.initialize_result or {}
    capabilities = result.get("capabilities") or {}
    if "tools" not in capabilities:
        return failed(
            f"tools/list works but capabilities are {sorted(capabilities)}",
            json.dumps(capabilities)[:300],
        )
    return passed("tools capability advertised")


@register(
    id="P-020",
    title="An unknown tool is reported as a protocol error",
    layer=1,
    severity="info",
)
def unknown_tool(ctx: Ctx) -> Outcome:
    exchange = ctx.session.call_tool("mcpqa_no_such_tool", {})
    if exchange.error_code == INTERNAL_ERROR:
        return failed("calling an unknown tool produced -32603 (internal error)", exchange.describe())
    if exchange.error is not None:
        return passed(f"answered with JSON-RPC error {exchange.error_code}")
    if exchange.is_tool_error():
        return failed(
            "an unknown tool came back as isError rather than a JSON-RPC error — common in SDKs, "
            "and harmless for most clients, but the specification's example uses -32602",
            exchange.describe(),
        )
    return failed("an unknown tool produced a successful result", exchange.describe())
