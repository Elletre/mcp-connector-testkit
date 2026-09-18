"""Layer 1, HTTP binding.

The streamable HTTP transport puts requirements below JSON-RPC: a status code
for a notification, a status code for an unknown method, a 403 for a browser
origin nobody authorised, a 401 that tells the client where to go and get a
token. A server can be perfectly well-behaved in JSON and still fail all of them.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..protocol import (
    HEADER_METHOD,
    HEADER_MISMATCH,
    HEADER_PROTOCOL_VERSION,
    META_CLIENT_CAPABILITIES,
    META_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    UNSUPPORTED_PROTOCOL_VERSION,
)
from ..target import HttpTarget
from ..wire.http import HttpWire
from .model import Ctx, Outcome, failed, passed, register, skipped

EVIL_ORIGIN = "https://mcpqa-not-your-origin.test"


def _http_target(ctx: Ctx) -> HttpTarget:
    assert isinstance(ctx.target, HttpTarget)
    return ctx.target


def _anonymous_wire(ctx: Ctx) -> HttpWire:
    target = _http_target(ctx)
    return HttpWire(url=target.url, bearer=None, extra_headers=dict(target.headers)).start()


@register(
    id="H-001",
    title="A foreign Origin is rejected with 403",
    layer=1,
    severity="error",
    transports=("http",),
)
def origin_rejected(ctx: Ctx) -> Outcome:
    exchange = ctx.session.call("tools/list", headers={"Origin": EVIL_ORIGIN})
    if exchange.status == 403:
        return passed("403 for an unrecognised Origin")
    return failed(
        f"Origin {EVIL_ORIGIN} got HTTP {exchange.status} instead of 403 — a page on any site "
        "could drive this server through the user's browser",
        exchange.describe(),
    )


@register(
    id="H-002",
    title="A notification is acknowledged with 202 and no body",
    layer=1,
    severity="error",
    transports=("http",),
)
def notification_accepted(ctx: Ctx) -> Outcome:
    exchange = ctx.session.notify("notifications/cancelled", {"requestId": 999_999})
    if exchange.status != 202:
        return failed(f"a notification returned HTTP {exchange.status}, expected 202", exchange.describe())
    if exchange.raw_response.strip():
        return failed("202 came back with a body", exchange.describe())
    return passed("202 Accepted, empty body")


@register(
    id="H-003",
    title="Responses use a content type the client must support",
    layer=1,
    severity="error",
    transports=("http",),
)
def response_content_type(ctx: Ctx) -> Outcome:
    exchange = ctx.session.call("tools/list")
    content_type = exchange.headers.get("content-type", "")
    if content_type.startswith(("application/json", "text/event-stream")):
        return passed(content_type)
    return failed(f"content-type was {content_type!r}", exchange.describe())


@register(
    id="H-004",
    title="Mcp-Method must agree with the body",
    layer=1,
    severity="error",
    transports=("http",),
    eras=("stateless",),
)
def method_header_mismatch(ctx: Ctx) -> Outcome:
    exchange = ctx.session.call("tools/list", headers={HEADER_METHOD: "prompts/list"})
    return _expect_header_mismatch(exchange, "Mcp-Method that disagrees with the body")


@register(
    id="H-005",
    title="tools/call must carry Mcp-Name",
    layer=1,
    severity="error",
    transports=("http",),
    eras=("stateless",),
)
def name_header_required(ctx: Ctx) -> Outcome:
    tools = ctx.tools()
    if not tools:
        return skipped("the server published no tools")
    name = str(tools[0]["name"])
    wire = ctx.session.wire
    assert isinstance(wire, HttpWire)
    body: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": ctx.session.next_id(),
        "method": "tools/call",
        "params": ctx.session.params({"name": name, "arguments": {}}),
    }
    headers = {
        HEADER_PROTOCOL_VERSION: ctx.session.protocol_version,
        HEADER_METHOD: "tools/call",
    }
    exchange = wire.post(body, headers=headers)
    return _expect_header_mismatch(exchange, "tools/call without Mcp-Name")


@register(
    id="H-006",
    title="MCP-Protocol-Version must agree with the envelope",
    layer=1,
    severity="error",
    transports=("http",),
    eras=("stateless",),
)
def version_header_mismatch(ctx: Ctx) -> Outcome:
    """The envelope names a different version than the header does.

    The disagreeing value stays inside the current era on purpose. Naming a
    handshake-era version in the header makes the request ambiguous — servers
    that route by era read it as a legacy request and answer accordingly —
    whereas this pair can only be one thing: a client contradicting itself.
    """
    other_version = "2027-07-28"
    params = {
        "_meta": {
            META_PROTOCOL_VERSION: other_version,
            META_CLIENT_CAPABILITIES: {},
        }
    }
    exchange = ctx.session.call(
        "tools/list",
        params,
        envelope=False,
        headers={
            HEADER_PROTOCOL_VERSION: ctx.session.protocol_version,
            HEADER_METHOD: "tools/list",
        },
    )
    if exchange.error_code == UNSUPPORTED_PROTOCOL_VERSION:
        return failed(
            "the server answered -32022 (unsupported version) for a request whose header and "
            "envelope disagree; the header check comes first, so the client can be told what it "
            "actually got wrong",
            exchange.describe(),
        )
    return _expect_header_mismatch(exchange, "a version header that disagrees with the envelope")


def _expect_header_mismatch(exchange: Any, what: str) -> Outcome:
    if exchange.status == 400 and exchange.error_code == HEADER_MISMATCH:
        return passed(f"{what} → 400 with -32020")
    return failed(
        f"{what} produced HTTP {exchange.status} / error {exchange.error_code}, expected 400 with -32020",
        exchange.describe(),
    )


@register(
    id="H-007",
    title="An unknown method returns HTTP 404",
    layer=1,
    severity="error",
    transports=("http",),
    eras=("stateless",),
)
def unknown_method_status(ctx: Ctx) -> Outcome:
    exchange = ctx.session.call("mcpqa/does-not-exist", headers={HEADER_METHOD: "mcpqa/does-not-exist"})
    if exchange.status == 404 and exchange.error_code == METHOD_NOT_FOUND:
        return passed("404 with -32601")
    return failed(
        f"got HTTP {exchange.status} / error {exchange.error_code}, expected 404 with -32601",
        exchange.describe(),
    )


@register(
    id="H-008",
    title="A missing or invalid token is refused with 401",
    layer=1,
    severity="error",
    transports=("http",),
    needs=("auth",),
)
def unauthenticated_401(ctx: Ctx) -> Outcome:
    target = _http_target(ctx)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": ctx.session.params(None)}
    for token, label in ((None, "no token"), ("mcpqa-not-a-real-token", "an invalid token")):
        wire = HttpWire(url=target.url, bearer=token, extra_headers=dict(target.headers)).start()
        try:
            exchange = wire.post(body, headers=ctx.session.http_headers(body))
        finally:
            wire.close()
        if exchange.status != 401:
            return failed(f"a request with {label} returned HTTP {exchange.status}", exchange.describe())
    return passed("401 for a missing token and for an invalid one")


def _challenge_metadata_url(challenge: str) -> str | None:
    match = re.search(r'resource_metadata="([^"]+)"', challenge)
    return match.group(1) if match else None


@register(
    id="H-009",
    title="A client can find out where to get a token",
    layer=1,
    severity="error",
    transports=("http",),
    needs=("auth",),
)
def authorization_server_discoverable(ctx: Ctx) -> Outcome:
    """Either mechanism satisfies the specification; having neither does not.

    The 401 challenge may point at the metadata document, or the document may
    sit at a well-known URI. Whichever is used, the document has to name at
    least one authorization server, or the client has found nothing.
    """
    target = _http_target(ctx)
    wire = _anonymous_wire(ctx)
    try:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": ctx.session.params(None)}
        challenge = wire.post(body, headers=ctx.session.http_headers(body)).headers.get(
            "www-authenticate", ""
        )
        path = target.url[len(target.base_origin()) :] or "/"
        header_url = _challenge_metadata_url(challenge)
        candidates = (
            [(header_url, "the WWW-Authenticate challenge")]
            if header_url
            else [
                (f"{target.base_origin()}/.well-known/oauth-protected-resource{path}", "a well-known URI"),
                (f"{target.base_origin()}/.well-known/oauth-protected-resource", "a well-known URI"),
            ]
        )
        for url, mechanism in candidates:
            exchange = wire.get(url)
            if exchange.status != 200 or exchange.response is None:
                continue
            servers = exchange.response.get("authorization_servers")
            if not isinstance(servers, list) or not servers:
                return failed(
                    f"the metadata at {url} names no authorization server",
                    json.dumps(exchange.response)[:300],
                )
            return passed(f"found through {mechanism}: {servers[0]}")
        where = header_url or " or ".join(url for url, _ in candidates)
        return failed(
            "no protected resource metadata: the 401 carries no resource_metadata parameter and "
            f"nothing is served at {where}"
        )
    finally:
        wire.close()


@register(
    id="H-010",
    title="A token issued for someone else is refused",
    layer=1,
    severity="error",
    transports=("http",),
    needs=("auth", "foreign_tokens"),
)
def foreign_token_refused(ctx: Ctx) -> Outcome:
    target = _http_target(ctx)
    tokens = ctx.profile.foreign_tokens
    accepted: list[str] = []
    for token in tokens:
        wire = HttpWire(url=target.url, bearer=token, extra_headers=dict(target.headers)).start()
        try:
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": ctx.session.params(None),
            }
            exchange = wire.post(body, headers=ctx.session.http_headers(body))
            if exchange.status == 200 and exchange.result is not None:
                accepted.append(token)
        finally:
            wire.close()
    if accepted:
        return failed(
            f"the server served tools to {len(accepted)} of {len(tokens)} credential(s) that were "
            "not issued for it — this is token passthrough",
        )
    return passed(f"{len(tokens)} foreign credential(s) refused")
