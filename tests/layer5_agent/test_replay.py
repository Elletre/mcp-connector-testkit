"""Layer 5 in CI, without a model.

The committed baseline run recorded every decision the model made. Replaying
those decisions against today's connector must produce today's grades: if a
change to the connector alters what a tool returns or does, a case that passed
in the recording stops passing here — and nobody had to pay for a model call
to find out.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mcpqa.evals.adapters.replay import ReplayAdapter
from mcpqa.evals.dataset import load_cases
from mcpqa.evals.runner import run

pytestmark = pytest.mark.layer5

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "evals/results/2026-09-18-llama3-8b-baseline"


def recorded_trials() -> dict[tuple[str, int], dict[str, object]]:
    trials = {}
    for line in (BASELINE / "trials.jsonl").read_text().splitlines():
        trial = json.loads(line)
        trials[(trial["case_id"], trial["repeat"])] = trial
    return trials


@pytest.mark.skipif(not BASELINE.exists(), reason="no recorded baseline run in the repository")
def test_replaying_the_recorded_run_reproduces_its_grades() -> None:
    import sys

    sys.path.insert(0, str(ROOT / "evals"))
    from acme_environment import AcmeEnvironment

    os.environ.pop("ACME_MCP_DEFECTS", None)  # the recording was made against the clean connector
    cases = load_cases(ROOT / "evals/cases")
    recordings = ReplayAdapter.load(BASELINE / "turns.jsonl")
    environment = AcmeEnvironment(defects="")

    replayed = run(
        cases,
        environment,
        lambda repeat: ReplayAdapter(recordings=recordings, repeat=repeat),
        repeats=1,
    )

    expected = recorded_trials()
    mismatches = [
        f"{trial.case_id}: recorded {'pass' if expected[(trial.case_id, 1)]['passed'] else 'fail'}, "
        f"replayed {'pass' if trial.passed else 'fail'} {trial.failures[:1]}"
        for trial in replayed.trials
        if trial.passed != expected[(trial.case_id, 1)]["passed"]
        or trial.tools_called != expected[(trial.case_id, 1)]["tools_called"]
    ]
    assert not mismatches, (
        "the connector no longer behaves as it did when the run was recorded:\n" + "\n".join(mismatches)
    )
