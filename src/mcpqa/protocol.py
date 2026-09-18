"""Protocol constants, written out rather than imported from an SDK.

A conformance kit that imports its notion of "correct" from one implementation
can only ever prove that a server agrees with that implementation. These values
come from the specification, and the version they belong to is part of the name.
"""

from __future__ import annotations

from typing import Final, Literal

Era = Literal["handshake", "stateless"]

HANDSHAKE_VERSIONS: Final = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
"""Revisions reached through the `initialize` handshake."""

STATELESS_VERSIONS: Final = ("2026-07-28",)
"""Revisions that carry protocol version and capabilities in every request."""

LATEST_HANDSHAKE: Final = HANDSHAKE_VERSIONS[-1]
LATEST_STATELESS: Final = STATELESS_VERSIONS[-1]

ERA_OF: Final[dict[str, Era]] = {
    **dict.fromkeys(HANDSHAKE_VERSIONS, "handshake"),
    **dict.fromkeys(STATELESS_VERSIONS, "stateless"),
}

# Reserved `_meta` keys of the per-request envelope (2026-07-28).
META_PROTOCOL_VERSION: Final = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES: Final = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO: Final = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO: Final = "io.modelcontextprotocol/serverInfo"

# HTTP headers the transport mirrors body fields into.
HEADER_PROTOCOL_VERSION: Final = "MCP-Protocol-Version"
HEADER_METHOD: Final = "Mcp-Method"
HEADER_NAME: Final = "Mcp-Name"
HEADER_SESSION_ID: Final = "Mcp-Session-Id"

NAME_BEARING_METHODS: Final = {
    "tools/call": "name",
    "prompts/get": "name",
    "resources/read": "uri",
}

# JSON-RPC 2.0
PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603

# MCP-allocated range (-32020..-32099), see the 2026-07-28 error code policy.
HEADER_MISMATCH: Final = -32020
MISSING_REQUIRED_CLIENT_CAPABILITY: Final = -32021
UNSUPPORTED_PROTOCOL_VERSION: Final = -32022

CLIENT_INFO: Final = {"name": "mcpqa", "version": "0.1.0"}


def era_for(version: str) -> Era:
    try:
        return ERA_OF[version]
    except KeyError:
        raise ValueError(f"unknown protocol version: {version}") from None
