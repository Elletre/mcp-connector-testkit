"""Layer 1 against the demo connector, one test per rule.

The checks themselves live in the kit and are proved to fire by
`tests/unit/test_checks_fire.py`; this is where they are pointed at the
connector this repository ships, in both protocol eras and on both transports.
"""

from __future__ import annotations

import pytest

from mcpqa.checks import Report, all_checks

pytestmark = pytest.mark.layer1

LAYER_1 = [check for check in all_checks() if check.layer == 1]
STDIO_CHECKS = [check.id for check in LAYER_1 if "stdio" in check.transports]
HTTP_CHECKS = [check.id for check in LAYER_1 if "http" in check.transports]


def assert_clean(report: Report, check_id: str) -> None:
    runs = [run for run in report.runs if run.check.id == check_id]
    assert runs, f"{check_id} never ran"
    for run in runs:
        if run.severity == "error":
            assert run.status != "failed", f"{run.label()}: {run.outcome.detail}\n{run.outcome.evidence}"
        elif run.status == "failed":
            # Warnings and notes are recorded rather than enforced; the ones the
            # demo connector actually produces are listed in docs/known-deviations.md.
            assert check_id in {"P-020"}, f"unexpected {run.severity}: {run.label()}: {run.outcome.detail}"


@pytest.mark.parametrize("check_id", STDIO_CHECKS)
def test_stdio(stdio_conformance: Report, check_id: str) -> None:
    assert_clean(stdio_conformance, check_id)


@pytest.mark.parametrize("check_id", HTTP_CHECKS)
def test_http(http_conformance: Report, check_id: str) -> None:
    assert_clean(http_conformance, check_id)


def test_nothing_was_skipped_for_want_of_a_probe(stdio_conformance: Report) -> None:
    """The demo supplies an oracle, fault control and samples, so nothing should skip."""
    skipped = [
        (run.label(), run.outcome.detail)
        for run in stdio_conformance.runs
        if run.status == "skipped" and run.check.layer == 1
    ]
    assert not skipped, f"checks skipped despite a complete profile: {skipped}"
