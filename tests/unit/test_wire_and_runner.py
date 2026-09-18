"""The raw clients, where they meet servers that do unusual things."""

from __future__ import annotations

import sys

import httpx2 as httpx

from mcpqa.wire.http import HttpWire
from mcpqa.wire.stdio import StdioWire

ASKS_BEFORE_ANSWERING = r"""
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    # A server-to-client request that happens to reuse the client's id.
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "method": "ping"}), flush=True)
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"ok": True}}), flush=True)
"""


def test_a_server_request_is_never_mistaken_for_the_answer() -> None:
    wire = StdioWire(command=[sys.executable, "-c", ASKS_BEFORE_ANSWERING]).start()
    try:
        exchange = wire.request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    finally:
        wire.close()
    assert exchange.response == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    assert [message["method"] for message in wire.transcript.unsolicited] == ["ping"]


def test_the_answer_is_picked_out_of_an_event_stream() -> None:
    events = [
        '{"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}',
        '{"jsonrpc": "2.0", "id": 7, "result": {"ok": true}}',
        '{"jsonrpc": "2.0", "method": "notifications/message", "params": {}}',
    ]
    body = "".join(f"event: message\ndata: {event}\n\n" for event in events)
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    payload, note = HttpWire._decode(response, expect_response=True, request_id=7)

    assert payload == {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
    assert note == "3 SSE events"
