#!/usr/bin/env python
"""The same minimal server, over HTTP, with the transport-level rules breakable.

The HTTP binding is where a server can be perfectly correct in JSON and still
be unsafe: no Origin validation, a 200 where the spec says 202, a token it was
never issued. Those checks need something that gets them wrong on purpose too.

    MINI_BREAK=no-origin-check MINI_PORT=8123 python mini_http.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).parent))

from mini_stdio import (
    BREAKS,
    PROTOCOL_STATELESS,
    broken,
    handle,
)

RESOURCE_URL = os.environ.get("MINI_RESOURCE_URL", "http://127.0.0.1:8123/mcp")
ISSUER_URL = "https://auth.mini.test"
VALID_TOKEN = "mini_token"
ALLOWED_ORIGINS = {"http://127.0.0.1", "http://localhost"}


def _authorized(request: Request) -> Response | None:
    if broken("no-auth"):
        return None
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if token == VALID_TOKEN or (token and broken("accepts-any-token")):
        return None
    challenge = 'Bearer error="invalid_token"'
    if not broken("no-prm"):
        challenge += f', resource_metadata="{_metadata_url()}"'
    return JSONResponse({"error": "invalid_token"}, status_code=401, headers={"WWW-Authenticate": challenge})


def _metadata_url() -> str:
    origin, _, path = RESOURCE_URL.partition("/mcp")
    return f"{origin}/.well-known/oauth-protected-resource/mcp{path}"


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None or broken("no-origin-check"):
        return True
    return any(origin.startswith(allowed) for allowed in ALLOWED_ORIGINS)


def _header_problem(request: Request, body: dict[str, Any]) -> str | None:
    if broken("no-header-validation"):
        return None
    meta = (body.get("params") or {}).get("_meta") or {}
    if "io.modelcontextprotocol/protocolVersion" not in meta:
        return None  # the envelope check belongs to the dispatcher
    if request.headers.get("mcp-protocol-version") != meta["io.modelcontextprotocol/protocolVersion"]:
        return "mcp-protocol-version header does not match the request envelope"
    if request.headers.get("mcp-method") != body.get("method"):
        return "mcp-method header does not match the request body's method"
    if body.get("method") == "tools/call":
        name = (body.get("params") or {}).get("name")
        if name is not None and request.headers.get("mcp-name") != name:
            return "mcp-name header does not match the request body's name parameter"
    return None


async def endpoint(request: Request) -> Response:
    if not _origin_ok(request):
        return JSONResponse({"error": "forbidden origin"}, status_code=403)
    unauthorized = _authorized(request)
    if unauthorized is not None:
        return unauthorized

    raw = (await request.body()).decode("utf-8")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    problem = _header_problem(request, body)
    if problem is not None:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32020, "message": problem}},
            status_code=400,
        )

    response = handle(body)
    if response is None:  # a notification
        if broken("notification-200"):
            return JSONResponse({"ok": True}, status_code=200)
        return Response(status_code=202)

    status = 200
    error = response.get("error")
    if error is not None:
        code = error.get("code")
        if code == -32601:
            status = 200 if broken("unknown-method-200") else 404
        elif code in (-32602, -32022, -32020, -32700):
            status = 400

    if broken("wrong-content-type"):
        return PlainTextResponse(json.dumps(response), status_code=status, media_type="text/plain")
    return JSONResponse(response, status_code=status)


async def metadata(request: Request) -> Response:
    if broken("no-prm"):
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(
        {
            "resource": RESOURCE_URL,
            "authorization_servers": [ISSUER_URL],
            "scopes_supported": ["items"],
            "bearer_methods_supported": ["header"],
        }
    )


app = Starlette(
    routes=[
        Route("/mcp", endpoint, methods=["POST"]),
        Route("/.well-known/oauth-protected-resource/mcp", metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", metadata, methods=["GET"]),
    ]
)


def main() -> None:
    port = int(os.environ.get("MINI_PORT", "8123"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


if __name__ == "__main__":
    print(f"mini_http breaking: {sorted(BREAKS)} on {PROTOCOL_STATELESS}", file=sys.stderr)
    main()
