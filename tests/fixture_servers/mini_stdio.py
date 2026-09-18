#!/usr/bin/env python
"""A minimal MCP server over stdio, written without the SDK — and breakable.

This exists to test the tests. Every check in the kit is supposed to fail when
a server violates the rule it enforces; the only way to know that it does is to
point it at a server that violates it on purpose.

An SDK-based server cannot help here, because the SDK is what stops most of
these mistakes from being possible. So this is about two hundred lines of plain
JSON-RPC that speak both protocol eras, plus one flag:

    MINI_BREAK=stdout-noise python mini_stdio.py

Each id in BREAKS maps to exactly one violation, named after the check that
should catch it. `MINI_STATE` points at a JSON file holding the mutable state,
so the annotation checks have something to observe.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

BREAKS = {item for item in os.environ.get("MINI_BREAK", "").split(",") if item}
STATE_PATH = Path(os.environ["MINI_STATE"]) if os.environ.get("MINI_STATE") else None

PROTOCOL_STATELESS = "2026-07-28"
PROTOCOL_HANDSHAKE = "2025-11-25"
SERVER_INFO = {"name": "mini-fixture", "version": "0.1.0"}

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

HANDSHAKE_SEEN = False
"""Set once `initialize` arrives: after that, this connection is a handshake-era one.

On stdio there is no header to route by, so the era of a connection is decided
by whether it opened with a handshake — which is also how the reference SDK
reads it."""


def broken(name: str) -> bool:
    return name in BREAKS


# --------------------------------------------------------------------- state


def read_items() -> list[str]:
    if STATE_PATH is None or not STATE_PATH.exists():
        return []
    return list(json.loads(STATE_PATH.read_text())["items"])


def write_items(items: list[str]) -> None:
    if STATE_PATH is not None:
        STATE_PATH.write_text(json.dumps({"items": items}))


# --------------------------------------------------------------------- tools


def tool_definitions() -> list[dict[str, Any]]:
    echo_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "times": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
        },
        "required": ["text"],
    }
    if broken("bad-input-schema"):
        echo_schema["properties"]["times"]["type"] = "int"  # not a JSON Schema type

    item_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    items_output = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "string"}}},
        "required": ["items"],
    }
    if broken("bad-output-schema"):
        items_output = {"type": "object", "properties": {"items": {"type": "nope"}}}

    tools: list[dict[str, Any]] = [
        {
            "name": "Echo It!" if broken("bad-tool-name") else "echo",
            "description": "Repeat the text back.",
            "inputSchema": echo_schema,
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "list_items",
            "description": "List the items held by this server.",
            "inputSchema": {"type": "object", "properties": {}},
            "outputSchema": items_output,
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        },
        {
            "name": "add_item",
            "description": "Add an item. Additive and repeatable.",
            "inputSchema": item_schema,
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
            },
        },
        {
            "name": "remove_item",
            "description": "Remove an item.",
            "inputSchema": item_schema,
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": not broken("destructive-lies"),
                "idempotentHint": True,
            },
        },
    ]
    if broken("unannotated-mutation"):
        tools.append(
            {
                "name": "touch_item",
                "description": "Add an item, with nothing said about what that does.",
                "inputSchema": item_schema,
            }
        )
    if broken("varies-per-connection"):
        # A tool set that depends on which process answered: the same server,
        # two connections, two different catalogues.
        tools.append(
            {
                "name": f"per_connection_{os.getpid()}",
                "description": "Exists only on this connection.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            }
        )
    if broken("unstable-order"):
        random.shuffle(tools)
    return tools


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "echo":
        if broken("strict-optional") and "times" not in arguments:
            return error_payload(-32602, "times is required")
        if not broken("accepts-anything") and not isinstance(arguments.get("text"), str):
            return tool_error("" if broken("empty-error") else "text must be a string")
        text = str(arguments.get("text", ""))
        return {"content": [{"type": "text", "text": text * int(arguments.get("times", 1))}]}

    if name == "list_items":
        items = read_items()
        if broken("readonly-mutates"):
            write_items([*items, f"peeked-{len(items)}"])
        structured: dict[str, Any] = {"items": items}
        if broken("bad-structured-content"):
            structured = {"items": "not-a-list"}
        content = [] if broken("structured-only") else [{"type": "text", "text": json.dumps(structured)}]
        return {"content": content, "structuredContent": structured}

    if name in ("add_item", "touch_item", "remove_item") and (
        not broken("accepts-anything") and not isinstance(arguments.get("name"), str)
    ):
        return tool_error("name must be a string")

    if name in ("add_item", "touch_item"):
        items = read_items()
        item = str(arguments.get("name", ""))
        if broken("idempotent-lies") or item not in items:
            items.append(item)
        write_items(items)
        return {"content": [{"type": "text", "text": f"added {item}"}]}

    if name == "remove_item":
        item = str(arguments.get("name", ""))
        write_items([existing for existing in read_items() if existing != item])
        return {"content": [{"type": "text", "text": f"removed {item}"}]}

    if broken("unknown-tool-500"):
        return error_payload(-32603, f"unknown tool {name}")
    return error_payload(-32602, f"Unknown tool: {name}")


def tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def error_payload(code: int, message: str) -> dict[str, Any]:
    return {"__error__": {"code": code, "message": message}}


# ------------------------------------------------------------------ dispatch


def decorate(result: dict[str, Any], *, stateless: bool, cacheable: bool) -> dict[str, Any]:
    if not stateless:
        return result
    if not broken("missing-result-type"):
        result["resultType"] = "complete"
    if cacheable:
        result.setdefault("ttlMs", 0)
        result.setdefault("cacheScope", "private")
    if not broken("no-server-info"):
        result.setdefault("_meta", {})[META_SERVER_INFO] = SERVER_INFO
    return result


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    global HANDSHAKE_SEEN
    method = message.get("method")
    params = message.get("params") or {}
    meta = params.get("_meta") or {}
    if method == "initialize":
        HANDSHAKE_SEEN = True
    stateless = not HANDSHAKE_SEEN

    if method is not None and "id" not in message:
        return None  # a notification

    if stateless and method != "initialize":
        if not broken("ignores-missing-envelope") and (
            META_VERSION not in meta or META_CAPABILITIES not in meta
        ):
            return reply(message, error=(-32602, "params._meta is missing required envelope keys"))
        version = meta.get(META_VERSION)
        if version != PROTOCOL_STATELESS and not broken("accepts-any-version"):
            return reply(
                message,
                error=(-32022, "Unsupported protocol version"),
                error_data={"supported": [PROTOCOL_STATELESS], "requested": version},
            )

    if method == "initialize":
        requested = params.get("protocolVersion")
        if broken("echoes-any-version"):
            agreed = requested  # whatever the client asked for, supported or not
        else:
            agreed = requested if requested == PROTOCOL_HANDSHAKE else PROTOCOL_HANDSHAKE
        return reply(
            message,
            result={
                "protocolVersion": agreed,
                "capabilities": {} if broken("no-capabilities") else {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )

    if method == "server/discover":
        if broken("no-discover"):
            return reply(message, error=(-32601, "Method not found"))
        return reply(
            message,
            result=decorate(
                {
                    "supportedVersions": [PROTOCOL_STATELESS],
                    "capabilities": {} if broken("no-capabilities") else {"tools": {}},
                },
                stateless=True,
                cacheable=True,
            ),
        )

    if method == "tools/list":
        if broken("stdout-noise"):
            sys.stdout.write("[mini] listing tools\n")
            sys.stdout.flush()
        return reply(
            message,
            result=decorate({"tools": tool_definitions()}, stateless=stateless, cacheable=True),
        )

    if method == "tools/call":
        if broken("slow-serial"):
            time.sleep(0.6)
        outcome = call_tool(str(params.get("name")), dict(params.get("arguments") or {}))
        if "__error__" in outcome:
            failure = outcome["__error__"]
            return reply(message, error=(failure["code"], failure["message"]))
        return reply(message, result=decorate(outcome, stateless=stateless, cacheable=False))

    if broken("method-not-found-as-result"):
        return reply(message, result=decorate({}, stateless=stateless, cacheable=False))
    return reply(message, error=(-32601, "Method not found"))


def reply(
    message: dict[str, Any],
    *,
    result: dict[str, Any] | None = None,
    error: tuple[int, str] | None = None,
    error_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_id = message.get("id")
    if broken("bad-id"):
        request_id = "definitely-not-your-id"
    response: dict[str, Any] = {"id": request_id}
    if not broken("no-jsonrpc"):
        response["jsonrpc"] = "2.0"
    if error is not None:
        payload: dict[str, Any] = {"code": error[0], "message": error[1]}
        if error_data is not None:
            payload["data"] = error_data
        response["error"] = payload
    else:
        response["result"] = result or {}
    return response


def main() -> None:
    for line in sys.stdin:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except json.JSONDecodeError:
            if broken("dies-on-garbage"):
                raise SystemExit(1) from None
            continue
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    if broken("never-exits"):
        time.sleep(30)


if __name__ == "__main__":
    main()
