"""The command line, end to end, against the fixture servers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mcpqa.cli import main

ROOT = Path(__file__).resolve().parents[2]
MINI = ROOT / "tests/fixture_servers/mini_stdio.py"


@pytest.fixture
def state(tmp_path: Path) -> Path:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"items": ["alpha", "beta"]}))
    return path


def mini_command(break_id: str = "") -> str:
    prefix = f"env MINI_BREAK={break_id} " if break_id else ""
    return f"{prefix}{sys.executable} {MINI}"


def test_list_checks_prints_every_check(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list-checks"]) == 0
    assert "P-001" in capsys.readouterr().out
    assert main(["list-checks", "--markdown", "--layer", "2"]) == 0
    table = capsys.readouterr().out
    assert "| A-001 |" in table and "| P-001 |" not in table


def test_check_passes_a_clean_server_and_writes_both_reports(tmp_path: Path) -> None:
    json_out, markdown_out = tmp_path / "report.json", tmp_path / "report.md"
    code = main(
        [
            "check",
            "--stdio",
            mini_command(),
            "--era",
            "both",
            "--layers",
            "1",
            "--json",
            str(json_out),
            "--markdown",
            str(markdown_out),
        ]
    )
    assert code == 0
    report = json.loads(json_out.read_text())
    assert report["ok"] is True
    assert {run["era"] for run in report["runs"]} == {"stateless", "handshake"}
    assert all(run["spec"]["url"].startswith("https://modelcontextprotocol.io/") for run in report["runs"])
    assert "# mcpqa report" in markdown_out.read_text()


def test_check_fails_a_broken_server_and_shows_the_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    markdown_out = tmp_path / "report.md"
    code = main(
        [
            "check",
            "--stdio",
            mini_command("no-jsonrpc"),
            "--era",
            "stateless",
            "--only",
            "P-001",
            "--markdown",
            str(markdown_out),
            "-v",
        ]
    )
    assert code == 1
    output = capsys.readouterr().out
    assert "P-001" in output and "evidence" in output
    assert "## Evidence" in markdown_out.read_text()


def test_auto_era_detects_both_eras(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "--stdio", mini_command(), "--only", "P-001"]) == 0
    assert "eras answered: stateless, handshake" in capsys.readouterr().out


def test_warnings_can_be_made_to_fail_the_run() -> None:
    arguments = ["check", "--stdio", mini_command("unstable-order"), "--era", "stateless", "--only", "P-007"]
    assert main(arguments) == 0, "a SHOULD violation is a warning, not a failed run"
    assert main([*arguments, "--fail-on", "warning"]) == 1


def test_samples_are_read_from_a_file(tmp_path: Path, state: Path) -> None:
    samples = tmp_path / "samples.json"
    samples.write_text(json.dumps({"echo": [{"text": "hi"}]}))
    code = main(
        [
            "check",
            "--stdio",
            f"env MINI_STATE={state} {mini_command()}",
            "--era",
            "stateless",
            "--only",
            "C-001",
            "--samples",
            str(samples),
        ]
    )
    assert code == 0
