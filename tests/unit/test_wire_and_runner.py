"""The raw clients and the runner, where they meet servers that do unusual things."""

from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx2 as httpx
import pytest

from mcpqa.checks import run_checks
from mcpqa.checks.model import REGISTRY, Ctx, Outcome, passed
from mcpqa.target import StdioTarget
from mcpqa.wire.http import HttpWire
from mcpqa.wire.stdio import StdioWire

MINI = Path(__file__).resolve().parents[1] / "fixture_servers/mini_stdio.py"

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


def test_one_check_that_kills_the_server_does_not_fail_the_checks_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def kill(ctx: Ctx) -> Outcome:
        assert isinstance(ctx.session.wire, StdioWire)
        ctx.session.wire.send_raw("this is not JSON\n")
        deadline = time.monotonic() + 5
        while ctx.session.alive and time.monotonic() < deadline:
            time.sleep(0.05)
        return passed("the server is gone")

    monkeypatch.setitem(REGISTRY, "P-000", replace(REGISTRY["P-001"], id="P-000", run=kill))
    target = StdioTarget(command=[sys.executable, str(MINI)], env={"MINI_BREAK": "dies-on-garbage"})

    report = run_checks(target, ids=("P-000", "P-001"), eras=("stateless",))

    assert [(run.check.id, run.status) for run in report.runs] == [("P-000", "passed"), ("P-001", "passed")]
