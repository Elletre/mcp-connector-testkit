"""Every sentence of the specification the kit enforces, in one place.

Kept together on purpose: this file is what a reviewer should be able to read
top to bottom and check against the specification without opening a single
check. `tests/unit/test_spec_quotes.py` does that check mechanically, against
vendored copies of the pages, on every run.

Where a check is stricter than the keyword in its quote — an error where the
specification only describes, or a warning where it says nothing — the reason
is written in `ESCALATIONS`, and the report prints it next to the finding.
"""

from __future__ import annotations

from .model import SpecRef

CURRENT = "2026-07-28"
HANDSHAKE = "2025-11-25"
HTTP = "basic/transports/streamable-http"

RESPONSE_ID = SpecRef(
    CURRENT,
    "basic",
    "Result responses MUST include the same ID as the request they correspond to.",
    "result-responses",
)
UNKNOWN_METHOD = SpecRef(
    CURRENT,
    HTTP,
    "If the server does not implement the requested RPC method, it MUST respond with 404 Not Found "
    "and a JSON-RPC error with code -32601 (Method not found).",
    "protocol-version-header",
)

CITATIONS: dict[str, SpecRef] = {
    # --------------------------------------------------------------- protocol
    "P-001": RESPONSE_ID,
    "P-002": SpecRef(
        CURRENT,
        "",
        "This specification defines the authoritative protocol requirements, based on the TypeScript "
        "schema in schema.ts.",
    ),
    "P-003": UNKNOWN_METHOD,
    "P-004": SpecRef(
        CURRENT,
        "server/tools",
        "Tool names SHOULD be between 1 and 128 characters in length (inclusive). [...] The following "
        "SHOULD be the only allowed characters: uppercase and lowercase ASCII letters (A-Z, a-z), digits "
        "(0-9), underscore (_), hyphen (-), and dot (.)",
        "tool-names",
    ),
    "P-005": SpecRef(
        CURRENT,
        "server/tools",
        "inputSchema: JSON Schema defining expected parameters [...] MUST be a valid JSON Schema object "
        "(not null)",
        "tool",
    ),
    "P-006": SpecRef(
        CURRENT,
        "basic",
        "Schemas MUST be valid according to their declared or default dialect",
        "schema-validation",
    ),
    "P-007": SpecRef(
        CURRENT,
        "server/tools",
        "Servers SHOULD return tools in a deterministic order (i.e., the same ordering across requests "
        "when the underlying set of tools has not changed).",
        "listing-tools",
    ),
    "P-008": SpecRef(
        CURRENT,
        "server/tools",
        "This set MAY be empty and MAY change over time [...] but MUST NOT vary per-connection or as a "
        "side effect of other requests on the connection.",
        "listing-tools",
    ),
    "P-009": SpecRef(
        CURRENT,
        "basic",
        "Servers SHOULD include the following io.modelcontextprotocol/* field in every result's _meta, "
        "unless specifically configured not to do so, to identify themselves without relying on any "
        "prior connection state",
        "_meta",
    ),
    "P-010": SpecRef(
        CURRENT,
        "server/discover",
        "server/discover lets a client query a server's supported protocol versions, capabilities, and "
        "identity before sending any other requests. Servers MUST implement it.",
    ),
    "P-011": SpecRef(
        CURRENT,
        HTTP,
        "If the server does not implement the requested protocol version [...] it MUST respond with "
        "400 Bad Request and an UnsupportedProtocolVersionError listing its supported versions.",
        "protocol-version-header",
    ),
    "P-012": SpecRef(
        CURRENT,
        "basic",
        "A request missing any required field is malformed; the server MUST reject it with JSON-RPC "
        "error code -32602 (Invalid params). On HTTP, the response status MUST be 400 Bad Request.",
        "_meta",
    ),
    "P-013": SpecRef(
        HANDSHAKE,
        "basic/lifecycle",
        "If the server supports the requested protocol version, it MUST respond with the same version. "
        "Otherwise, the server MUST respond with another protocol version it supports.",
        "version-negotiation",
    ),
    "P-014": SpecRef(
        CURRENT,
        "basic/transports/stdio",
        "The server MUST NOT write anything to its stdout that is not a valid MCP message.",
    ),
    "P-015": SpecRef(
        CURRENT,
        "basic/transports/stdio",
        "Servers SHOULD exit promptly when their standard input is closed or reads return end-of-file.",
        "shutdown",
    ),
    "P-016": SpecRef(
        CURRENT,
        "basic",
        "MCP uses the standard JSON-RPC 2.0 error codes (-32700, -32600 to -32603) for general protocol "
        "failures.",
        "error-codes",
    ),
    "P-017": RESPONSE_ID,
    "P-018": SpecRef(CURRENT, "basic", None),
    "P-019": SpecRef(
        CURRENT,
        "server/tools",
        "Servers that support tools MUST declare the tools capability",
        "capabilities",
    ),
    "P-020": SpecRef(
        CURRENT,
        "server/tools",
        "Protocol Errors indicate issues with the request structure itself that models are less likely "
        "to be able to fix: [...] They are returned as standard JSON-RPC errors",
        "error-handling",
    ),
    # ------------------------------------------------------------------- http
    "H-001": SpecRef(
        CURRENT,
        HTTP,
        "Servers MUST validate the Origin header on all incoming connections to prevent DNS rebinding "
        "attacks. If the Origin header is present and invalid, servers MUST respond with HTTP 403 "
        "Forbidden.",
        "security--endpoint",
    ),
    "H-002": SpecRef(
        CURRENT,
        HTTP,
        "If the server accepts it, the server MUST return HTTP status code 202 Accepted with no body.",
        "sending-messages",
    ),
    "H-003": SpecRef(
        CURRENT,
        HTTP,
        "the server MUST return either Content-Type: application/json (a single JSON object) or "
        "Content-Type: text/event-stream (an SSE response stream).",
        "sending-messages",
    ),
    "H-004": SpecRef(
        CURRENT,
        HTTP,
        "Servers that process the request body MUST reject requests where the values specified in the "
        "headers do not match the corresponding values in the request body. [...] servers MUST return "
        "HTTP status 400 Bad Request and MUST include a JSON-RPC error response using the following "
        "error code",
        "server-validation",
    ),
    "H-005": SpecRef(CURRENT, HTTP, "These headers are REQUIRED for compliance.", "standard-request-headers"),
    "H-006": SpecRef(
        CURRENT,
        HTTP,
        "The header value MUST match the io.modelcontextprotocol/protocolVersion field carried in the "
        "request body's _meta. If the values do not match, the server MUST reject the request with 400 "
        "Bad Request and a HeaderMismatch JSON-RPC error",
        "protocol-version-header",
    ),
    "H-007": UNKNOWN_METHOD,
    "H-008": SpecRef(
        CURRENT,
        "basic/authorization",
        "Invalid or expired tokens MUST receive a HTTP 401 response.",
        "token-handling",
    ),
    "H-009": SpecRef(
        CURRENT,
        "basic/authorization/authorization-server-discovery",
        "MCP servers MUST implement one of the following discovery mechanisms to provide authorization "
        "server location information to MCP clients",
        "protected-resource-metadata-discovery-requirements",
    ),
    "H-010": SpecRef(
        CURRENT,
        "basic/authorization/security-considerations",
        "MCP servers MUST only accept tokens specifically intended for themselves and MUST reject tokens "
        "that do not include them in the audience claim or otherwise verify that they are the intended "
        "recipient of the token.",
        "access-token-privilege-restriction",
    ),
    # --------------------------------------------------------------- contract
    "C-001": SpecRef(
        CURRENT, "server/tools", "inputSchema: JSON Schema defining expected parameters", "tool"
    ),
    "C-002": SpecRef(
        CURRENT, "server/tools", "Servers MUST: Validate all tool inputs", "security-considerations"
    ),
    "C-003": SpecRef(
        CURRENT,
        "server/tools",
        "If an output schema is provided: Servers MUST provide structured results that conform to this "
        "schema.",
        "output-schema",
    ),
    "C-004": SpecRef(
        CURRENT,
        "server/tools",
        "For backwards compatibility, a tool that returns structured content SHOULD also return the "
        "serialized JSON in a TextContent block.",
        "structured-content",
    ),
    "C-005": SpecRef(
        CURRENT,
        "server/tools",
        "Tool Execution Errors contain actionable feedback that language models can use to self-correct "
        "and retry with adjusted parameters",
        "error-handling",
    ),
    # ------------------------------------------------------------ annotations
    "A-001": SpecRef(
        CURRENT, "schema", "If true, the tool does not modify its environment.", "toolannotations"
    ),
    "A-002": SpecRef(
        CURRENT,
        "schema",
        "If true, the tool may perform destructive updates to its environment. If false, the tool "
        "performs only additive updates.",
        "toolannotations",
    ),
    "A-003": SpecRef(
        CURRENT,
        "schema",
        "If true, calling the tool repeatedly with the same arguments will have no additional effect on "
        "its environment.",
        "toolannotations",
    ),
    "A-004": SpecRef(
        CURRENT, "server/tools", "annotations: Optional properties describing tool behavior", "tool"
    ),
}

_ANNOTATIONS_MATTER = (
    "The specification calls annotations hints and tells clients not to trust them from untrusted "
    "servers. Clients still decide from them whether to ask the user before a call runs, so a false "
    "one removes that question; the kit treats it as a defect rather than a matter of style."
)

ESCALATIONS: dict[str, str] = {
    "P-002": (
        "The schema is the specification's own definition of every message; a result that fails it is "
        "malformed by the specification's definition, whatever keyword the prose around it uses."
    ),
    "P-016": (
        "The specification defines the parse error but not what happens after it; a server that exits "
        "on one malformed line takes every in-flight request on the connection with it."
    ),
    "P-018": (
        "Nothing in the specification forbids handling one request at a time. In practice a connection "
        "that stalls behind its slowest call makes every client sharing it hang, and it is almost always "
        "a blocking call left in async code."
    ),
    "C-001": (
        "A published schema is the only contract a client has. Rejecting input the schema allows breaks "
        "every client that relied on it, and the client cannot tell from the schema that it will happen."
    ),
    "C-005": "An error result with no text gives the model nothing to correct, so it guesses or gives up.",
    "A-001": _ANNOTATIONS_MATTER,
    "A-002": _ANNOTATIONS_MATTER,
    "A-003": _ANNOTATIONS_MATTER,
}
