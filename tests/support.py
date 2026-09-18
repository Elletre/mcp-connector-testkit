"""Wiring the kit to the demo target.

This is the adapter layer a team would write once for their own connector: it
teaches the kit how to look at the state behind the server, how to make the
upstream misbehave, and what a valid call to each tool looks like.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from acme_mail_api.control import ProviderControl
from acme_mail_api.server import ProviderServer
from mcpqa.checks import Profile
from mcpqa.target import HttpTarget, StdioTarget

ALICE = "alice@acme.test"
BOB = "bob@globex.test"
ALICE_UPSTREAM = "at_alice_rw"
MCP_ALICE = "mcp_alice"
MCP_BOB = "mcp_bob"

CANARY_ALICE = "CANARY-ALICE-7f3a91"
CANARY_BOB = "CANARY-BOB-4d2e60"

SAMPLES: dict[str, list[dict[str, Any]]] = {
    "search_messages": [
        {"query": "invoice", "max_results": 5},
        {"label": "INBOX", "max_results": 3},
    ],
    "get_message": [{"message_id": "msg_00901"}],
    "list_labels": [{}],
    "add_label": [{"message_id": "msg_00901", "label": "Later"}],
    "send_message": [{"to": ["dana.ruiz@acme.test"], "subject_line": "mcpqa probe", "body": "probe"}],
    "trash_message": [{"message_id": "msg_00902"}],
}

READ_ONLY_SAMPLES = {name: SAMPLES[name] for name in ("search_messages", "get_message", "list_labels")}


DEFECTS_FROM_ENV = os.environ.get("ACME_MCP_DEFECTS", "")
"""The defect matrix runs the whole suite once per defect by setting this."""


def stdio_target(
    provider: ProviderServer,
    *,
    defects: str = DEFECTS_FROM_ENV,
    access_token: str = ALICE_UPSTREAM,
    refresh_token: str = "rt_alice",
    timeout_s: float | None = None,
) -> StdioTarget:
    env = {
        "ACME_MAIL_BASE_URL": provider.base_url,
        "ACME_MAIL_ACCESS_TOKEN": access_token,
        "ACME_MAIL_REFRESH_TOKEN": refresh_token,
        "ACME_MAIL_ACCOUNT": ALICE,
        "ACME_MCP_DEFECTS": defects,
    }
    if timeout_s is not None:
        env["ACME_MAIL_TIMEOUT_S"] = str(timeout_s)
    return StdioTarget(command=[sys.executable, "-m", "acme_mail_mcp"], env=env, name="acme-mail")


@contextmanager
def http_connector(
    provider: ProviderServer, *, defects: str = DEFECTS_FROM_ENV, bearer: str = MCP_ALICE
) -> Iterator[HttpTarget]:
    """Run the connector's HTTP transport in a subprocess on a free port."""
    port = _free_port()
    resource = f"http://127.0.0.1:{port}/mcp"
    env = {
        **os.environ,
        "ACME_MAIL_BASE_URL": provider.base_url,
        "ACME_MCP_RESOURCE_URL": resource,
        "ACME_MCP_DEFECTS": defects,
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "acme_mail_mcp", "--transport", "http", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_port(port, process)
        yield HttpTarget(url=resource, bearer=bearer, name="acme-mail-http")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


def demo_profile(
    provider: ProviderServer,
    *,
    allow_mutations: bool = True,
    expects_auth: bool = False,
    account: str = ALICE,
) -> Profile:
    control = ProviderControl(provider.base_url, account=account)
    return Profile(
        samples=SAMPLES,
        read_only_samples=READ_ONLY_SAMPLES,
        secrets=(ALICE_UPSTREAM, "rt_alice", "at_bob_rw", "rt_bob"),
        oracle=control,
        faults=control,
        expects_auth=expects_auth,
        foreign_tokens=(ALICE_UPSTREAM, "at_bob_rw") if expects_auth else (),
        allow_mutations=allow_mutations,
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, process: subprocess.Popen[bytes], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:  # pragma: no cover - startup failure
            stderr = process.stderr.read().decode() if process.stderr else ""
            raise RuntimeError(f"connector exited during startup: {stderr[-800:]}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"connector did not open port {port} in {timeout}s")  # pragma: no cover
