"""Shared fixtures for the layered suites.

Two things here are worth copying into any connector's own suite:

* the provider is started once and reset between tests, so the suite is fast
  and still isolated;
* every session is scanned for credentials when it closes, so a leak fails the
  test that provoked it rather than being noticed months later in a log.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from acme_mail_api.server import ProviderServer

sys.path.insert(0, str(Path(__file__).parent))

from mcpqa.checks import Report, run_checks
from mcpqa.leaks import scan
from mcpqa.session import Session
from mcpqa.target import HttpTarget, StdioTarget
from support import (
    MCP_ALICE,
    ProviderControl,
    demo_profile,
    http_connector,
    stdio_target,
)


@pytest.fixture(scope="session")
def provider() -> Iterator[ProviderServer]:
    server = ProviderServer().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def control(provider: ProviderServer) -> Iterator[ProviderControl]:
    handle = ProviderControl(provider.base_url)
    handle.reset()
    yield handle
    handle.reset()


@pytest.fixture
def stdio(provider: ProviderServer) -> StdioTarget:
    return stdio_target(provider)


@pytest.fixture
def profile(provider: ProviderServer):
    return demo_profile(provider)


@pytest.fixture
def session(stdio: StdioTarget, profile) -> Iterator[Session]:
    """A connected session that fails its test if the server leaked a credential."""
    connected = Session(target=stdio, protocol_version="2026-07-28").open()
    try:
        yield connected
    finally:
        connected.close()
        leaks = scan(connected.transcript.texts(), profile.secrets)
        assert not leaks, "credentials in the server's output: " + "; ".join(
            leak.describe() for leak in leaks
        )


@pytest.fixture
def handshake_session(stdio: StdioTarget, profile) -> Iterator[Session]:
    connected = Session(target=stdio, protocol_version="2025-11-25").open()
    try:
        yield connected
    finally:
        connected.close()
        leaks = scan(connected.transcript.texts(), profile.secrets)
        assert not leaks, "credentials in the server's output: " + "; ".join(
            leak.describe() for leak in leaks
        )


@pytest.fixture(scope="module")
def http_target(provider: ProviderServer) -> Iterator[HttpTarget]:
    with http_connector(provider) as target:
        yield target


@pytest.fixture
def alice_session(http_target: HttpTarget) -> Iterator[Session]:
    connected = Session(target=http_target, protocol_version="2026-07-28").open()
    try:
        yield connected
    finally:
        connected.close()


@pytest.fixture
def bob_session(http_target: HttpTarget) -> Iterator[Session]:
    target = HttpTarget(url=http_target.url, bearer="mcp_bob", name="acme-mail-http-bob")
    connected = Session(target=target, protocol_version="2026-07-28").open()
    try:
        yield connected
    finally:
        connected.close()


@pytest.fixture
def alice_token() -> str:
    return MCP_ALICE


# The conformance checks are run once per transport and then asserted one at a
# time, so the report costs two runs but reads as one test per rule.


@pytest.fixture(scope="session")
def stdio_conformance(provider: ProviderServer) -> Report:
    control = ProviderControl(provider.base_url)
    control.reset()
    report = run_checks(
        stdio_target(provider),
        demo_profile(provider),
        eras=("stateless", "handshake"),
    )
    control.reset()
    return report


@pytest.fixture(scope="session")
def http_conformance(provider: ProviderServer) -> Report:
    control = ProviderControl(provider.base_url)
    control.reset()
    with http_connector(provider) as target:
        report = run_checks(
            target,
            demo_profile(provider, expects_auth=True),
            eras=("stateless", "handshake"),
        )
    control.reset()
    return report
