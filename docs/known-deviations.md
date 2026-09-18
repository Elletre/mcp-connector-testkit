# Known deviations

What the kit reports against the demo connector on a clean run, and why each item is
accepted rather than fixed. A clean run has no errors; everything below is a warning or
a note, and each one is a property of the official Python SDK (`mcp` 2.2.0) that the
connector is built on rather than of the connector's own code.

## P-020 · an unknown tool comes back as `isError`, not as a JSON-RPC error — note

The specification's error-handling section classifies an unknown tool as a protocol
error and shows `-32602` in its example. The SDK answers a call to a tool that does not
exist with a successful JSON-RPC response carrying `isError: true` and the text
`Unknown tool: <name>`. The sentence has no MUST attached to it, so the kit reports it as
a note. In practice most clients handle both shapes, and the `isError` form has one
advantage: the model sees the message and can correct itself.

## What the kit does not report, and why that matters

Three SDK behaviours came to light while building the kit. None shows up as a finding
against the demo — the first two because the demo's defaults avoid them, the third
because the kit tests around it — but all three are worth knowing if you build on the
same SDK.

**The stdio stream is protected from stray output.** While `stdio_server()` is serving,
the SDK points file descriptor 1 at stderr and writes protocol messages through a private
duplicate, so a `print()` in a handler cannot corrupt the stream. The most common stdio
failure mode is therefore impossible on this SDK — and still common on others, which is
why P-014 stays in the kit.

**DNS-rebinding protection depends on the bind address.** `streamable_http_app()` turns
Origin validation on automatically only when the host is `127.0.0.1`, `localhost` or
`::1`. Pass `host="0.0.0.0"` — the usual first step when containerising a service — and
the protection is silently off unless `transport_security` is configured explicitly. The
`bind-all-interfaces` defect reproduces this, and H-001 catches it.

**An era-ambiguous request is routed by its header.** A request whose
`MCP-Protocol-Version` header names a handshake-era version is treated as a legacy
request, even if its body carries a 2026-07-28 envelope; the SDK then asks for a session
id (`-32600`) rather than reporting the header mismatch (`-32020`). H-006 therefore tests
a mismatch inside the current era, where the specification's answer is unambiguous.
