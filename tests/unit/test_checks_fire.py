"""Every check is tested against a server that breaks its rule, and one that does not.

This is the part of the kit I would look for first if someone handed me one: a
conformance check that never fires is indistinguishable from a passing server,
and a whole suite of them is worse than nothing, because it buys confidence
without paying for it.

The fixture servers under `tests/fixture_servers/` are plain JSON-RPC with no
SDK, precisely so they *can* be wrong.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from mcpqa.checks import Profile, run_checks
from mcpqa.checks.model import REGISTRY
from mcpqa.protocol import Era
from mcpqa.target import HttpTarget, StdioTarget

FIXTURES = Path(__file__).resolve().parents[1] / "fixture_servers"
MINI_STDIO = FIXTURES / "mini_stdio.py"
MINI_HTTP = FIXTURES / "mini_http.py"

SAMPLES = {
    "echo": [{"text": "hi"}],
    "list_items": [{}],
    "add_item": [{"name": "gamma"}],
    "remove_item": [{"name": "alpha"}],
    "touch_item": [{"name": "delta"}],
}
READ_ONLY = {"echo": SAMPLES["echo"], "list_items": SAMPLES["list_items"]}

# check id -> the violation that must make it fail
STDIO_VIOLATIONS = [
    ("P-001", "no-jsonrpc"),
    ("P-002", "missing-result-type"),
    ("P-003", "method-not-found-as-result"),
    ("P-004", "bad-tool-name"),
    ("P-005", "bad-input-schema"),
    ("P-006", "bad-output-schema"),
    ("P-007", "unstable-order"),
    ("P-008", "varies-per-connection"),
    ("P-009", "no-server-info"),
    ("P-010", "no-discover"),
    ("P-011", "accepts-any-version"),
    ("P-012", "ignores-missing-envelope"),
    ("P-013", "echoes-any-version"),
    ("P-014", "stdout-noise"),
    ("P-015", "never-exits"),
    ("P-016", "dies-on-garbage"),
    ("P-017", "bad-id"),
    ("P-018", "slow-serial"),
    ("P-019", "no-capabilities"),
    ("P-020", "unknown-tool-500"),
    ("C-001", "strict-optional"),
    ("C-002", "accepts-anything"),
    ("C-003", "bad-structured-content"),
    ("C-004", "structured-only"),
    ("C-005", "empty-error"),
]

HTTP_VIOLATIONS = [
    ("H-001", "no-origin-check"),
    ("H-002", "notification-200"),
    ("H-003", "wrong-content-type"),
    ("H-004", "no-header-validation"),
    ("H-005", "no-header-validation"),
    ("H-006", "no-header-validation"),
    ("H-007", "unknown-method-200"),
    ("H-008", "no-auth"),
    ("H-009", "no-prm"),
    ("H-010", "accepts-any-token"),
]

NOT_COVERED_HERE: set[str] = set()


@dataclass
class FileOracle:
    """Reads the fixture server's state file as a set of facts.

    Facts are indexed, so appending the same value twice is a change — which is
    exactly the mistake the idempotency check is looking for.
    """

    path: Path

    def snapshot(self) -> frozenset[str]:
        if not self.path.exists():
            return frozenset()
        items = json.loads(self.path.read_text())["items"]
        return frozenset(f"item[{index}]:{value}" for index, value in enumerate(items))


@pytest.fixture
def state(tmp_path: Path) -> Path:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"items": ["alpha", "beta"]}))
    return path


def mini_profile(state: Path) -> Profile:
    return Profile(
        samples=SAMPLES,
        read_only_samples=READ_ONLY,
        oracle=FileOracle(state),
        allow_mutations=True,
    )


def stdio_fixture(state: Path, *, break_id: str = "") -> StdioTarget:
    return StdioTarget(
        command=[sys.executable, str(MINI_STDIO)],
        env={"MINI_BREAK": break_id, "MINI_STATE": str(state)},
        name="mini",
    )


@contextmanager
def http_fixture(state: Path, *, break_id: str = "") -> Iterator[HttpTarget]:
    port = _free_port()
    resource = f"http://127.0.0.1:{port}/mcp"
    env = {
        **os.environ,
        "MINI_BREAK": break_id,
        "MINI_STATE": str(state),
        "MINI_PORT": str(port),
        "MINI_RESOURCE_URL": resource,
    }
    process = subprocess.Popen(
        [sys.executable, str(MINI_HTTP)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        _wait_for_port(port, process)
        yield HttpTarget(url=resource, bearer="mini_token", name="mini-http")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


def eras_for(check_id: str) -> tuple[Era, ...]:
    return REGISTRY[check_id].eras


def assert_fires(check_id: str, break_id: str, report: object) -> None:
    """The check must fail somewhere, and must never be quietly skipped.

    "Somewhere" rather than "everywhere": several violations only exist in one
    era — a missing `resultType` is a defect in 2026-07-28 and meaningless in
    2025-11-25 — and a check that stays green where the rule does not apply is
    behaving correctly.
    """
    runs = report.runs  # type: ignore[attr-defined]
    assert runs, f"{check_id} did not run at all"
    outcomes = [(run.status, run.outcome.detail) for run in runs]
    assert any(status == "failed" for status, _ in outcomes), (
        f"{check_id} did not fail against MINI_BREAK={break_id}: {outcomes}"
    )
    assert all(status != "skipped" for status, _ in outcomes), (
        f"{check_id} skipped itself instead of judging MINI_BREAK={break_id}: {outcomes}"
    )


# --------------------------------------------------------------- the negatives


@pytest.mark.parametrize(("check_id", "break_id"), STDIO_VIOLATIONS, ids=[c for c, _ in STDIO_VIOLATIONS])
def test_check_fires_on_a_server_that_breaks_its_rule(check_id: str, break_id: str, state: Path) -> None:
    report = run_checks(
        stdio_fixture(state, break_id=break_id),
        mini_profile(state),
        eras=eras_for(check_id),
        ids=(check_id,),
        read_timeout_s=4.0,
    )
    assert_fires(check_id, break_id, report)


@pytest.mark.parametrize(("check_id", "break_id"), HTTP_VIOLATIONS, ids=[c for c, _ in HTTP_VIOLATIONS])
def test_http_check_fires_on_a_server_that_breaks_its_rule(check_id: str, break_id: str, state: Path) -> None:
    profile = mini_profile(state)
    profile.expects_auth = True
    profile.foreign_tokens = ("token_for_another_service",)
    with http_fixture(state, break_id=break_id) as target:
        report = run_checks(target, profile, eras=eras_for(check_id), ids=(check_id,), read_timeout_s=4.0)
    assert_fires(check_id, break_id, report)


# --------------------------------------------------------------- the positives


def test_every_check_has_a_fixture_that_breaks_it() -> None:
    """No check may join the kit without something that proves it can fail."""
    covered = {check_id for check_id, _ in STDIO_VIOLATIONS + HTTP_VIOLATIONS} | NOT_COVERED_HERE
    assert covered >= set(REGISTRY), f"checks with no negative fixture: {sorted(set(REGISTRY) - covered)}"


def test_clean_stdio_fixture_passes_everything(state: Path) -> None:
    report = run_checks(
        stdio_fixture(state),
        mini_profile(state),
        eras=("stateless", "handshake"),
        read_timeout_s=6.0,
    )
    failures = [
        (run.label(), run.severity, run.outcome.detail) for run in report.runs if run.status == "failed"
    ]
    assert not failures, f"the clean fixture should satisfy every check: {failures}"
    assert len(report.by_status("passed")) > 20


def test_clean_http_fixture_passes_everything(state: Path) -> None:
    profile = mini_profile(state)
    profile.expects_auth = True
    profile.foreign_tokens = ("token_for_another_service",)
    with http_fixture(state) as target:
        report = run_checks(target, profile, eras=("stateless",), read_timeout_s=6.0)
    failures = [
        (run.label(), run.severity, run.outcome.detail) for run in report.runs if run.status == "failed"
    ]
    assert not failures, f"the clean HTTP fixture should satisfy every check: {failures}"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, process: subprocess.Popen[bytes], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:  # pragma: no cover
            stderr = process.stderr.read().decode() if process.stderr else ""
            raise RuntimeError(f"fixture server exited during startup: {stderr[-800:]}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"fixture server did not open port {port}")  # pragma: no cover
