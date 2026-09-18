"""Layer 2 against the demo connector: the published contract versus behaviour."""

from __future__ import annotations

import pytest

from mcpqa.checks import Report, all_checks

pytestmark = pytest.mark.layer2

LAYER_2 = [check.id for check in all_checks() if check.layer == 2]


@pytest.mark.parametrize("check_id", LAYER_2)
def test_stdio(stdio_conformance: Report, check_id: str) -> None:
    runs = [run for run in stdio_conformance.runs if run.check.id == check_id]
    assert runs, f"{check_id} never ran"
    failures = [f"{run.label()}: {run.outcome.detail}" for run in runs if run.status == "failed"]
    assert not failures, "\n".join(failures)


def test_the_annotation_checks_were_able_to_observe_something(stdio_conformance: Report) -> None:
    """A green A-00x that silently skipped would be worth nothing."""
    for check_id in ("A-001", "A-002", "A-003"):
        runs = [run for run in stdio_conformance.runs if run.check.id == check_id]
        assert any(run.status == "passed" for run in runs), (
            f"{check_id} never actually ran against the connector: "
            f"{[(r.status, r.outcome.detail) for r in runs]}"
        )
