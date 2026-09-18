#!/usr/bin/env python
"""Run the whole suite once per seeded defect, and record which layer noticed.

This is the kit's own mutation score. A layer that never fails is a layer that
is not testing anything, and a defect that no layer catches is a hole in the
method — both of which this prints in a table rather than leaving to faith.

    uv run python tools/defect_matrix.py            # every defect
    uv run python tools/defect_matrix.py --only pagination-first-page-only
    uv run python tools/defect_matrix.py --jobs 4   # pytest-xdist workers

The agent column (layer 5) is filled in from evaluation comparisons, because
those defects are only visible with a model in the loop:

    uv run python tools/defect_matrix.py --agent-results \\
        vague-tool-descriptions=evals/results/compare-vague-tool-descriptions.json

`--render-only` rebuilds the pages from the last run's JSON without running
anything — for when only the agent results changed. Writing to the default
page also refreshes the table between the MATRIX markers in the README.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo/src"))

from acme_mail_mcp.defects import REGISTRY  # noqa: E402

LAYERS = (1, 2, 3, 4)
DEFAULT_OUT = ROOT / "docs/defect-matrix.md"
LEGEND = (
    "✅ that layer's tests failed with the defect in place · `·` they all passed · "
    "— not evaluated (layer 5 is run only for the agent-level defects)"
)
MARKER = " or ".join(f"layer{layer}" for layer in LAYERS)


@dataclass
class Outcome:
    defect: str
    failing_layers: set[int] = field(default_factory=set)
    failing_tests: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def detected(self) -> bool:
        return bool(self.failing_layers)


def run_suite(defect: str, *, jobs: int, results_path: Path) -> Outcome:
    env = {**os.environ, "ACME_MCP_DEFECTS": defect, "MCPQA_RESULTS": str(results_path)}
    command = ["uv", "run", "pytest", "-m", MARKER, "-q", "--no-header", "-p", "no:randomly"]
    if jobs > 1:
        command += ["-n", str(jobs)]
    started = time.monotonic()
    subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    elapsed = time.monotonic() - started

    outcome = Outcome(defect=defect or "(none)", duration_s=elapsed)
    if not results_path.exists():  # pragma: no cover - the suite could not start
        raise RuntimeError(f"no results written for {defect!r}")
    payload = json.loads(results_path.read_text())
    for result in payload["results"]:
        if result["outcome"] != "failed":
            continue
        outcome.failing_tests.append(str(result["nodeid"]))
        for marker in result["layers"]:
            outcome.failing_layers.add(int(marker.removeprefix("layer")))
    results_path.unlink()
    return outcome


def agent_layer(comparisons: list[str] | None) -> dict[str, bool]:
    """Which defects the agent evaluation flagged.

    Each argument is `DEFECT=PATH`, where PATH is what `mcpqa evals compare
    --json` wrote for the clean run against a run with that defect. One test
    per defect, fixed before the runs: a defect is flagged when the overall
    pass rate fell significantly (the whole bootstrap interval below zero).
    Category deltas are reported alongside but do not decide — seven of them
    per defect would be seven chances at a false alarm.
    """
    flagged: dict[str, bool] = {}
    for item in comparisons or []:
        defect, _, path = item.partition("=")
        if defect not in REGISTRY or not path:
            raise SystemExit(f"--agent-results expects DEFECT=PATH with a known defect, got {item!r}")
        flagged[defect] = bool(json.loads(Path(path).read_text())["regressed"])
    return flagged


def load_outcomes(json_path: Path) -> tuple[list[Outcome], float, str]:
    """The outcomes of the last full run, as written by `write_pages`."""
    payload = json.loads(json_path.read_text())
    outcomes = [
        Outcome(
            defect=defect,
            failing_layers=set(entry["layers"]),
            failing_tests=list(entry["failing_tests"]),
            duration_s=float(entry.get("seconds", 0.0)),
        )
        for defect, entry in payload["defects"].items()
    ]
    return outcomes, float(payload["baseline_seconds"]), str(payload["generated"])


def caught_as_expected(outcome: Outcome, agent: dict[str, bool]) -> bool:
    expected = REGISTRY[outcome.defect].expected_layer
    return bool(agent.get(outcome.defect)) if expected == 5 else expected in outcome.failing_layers


def render(outcomes: list[Outcome], agent: dict[str, bool]) -> str:
    lines = [
        "| Defect | What it breaks | 1 protocol | 2 contract | 3 auth | 4 upstream | 5 agent |",
        "| --- | --- | :-: | :-: | :-: | :-: | :-: |",
    ]
    for outcome in outcomes:
        if outcome.defect == "(none)":
            continue
        defect = REGISTRY[outcome.defect]
        cells = ["✅" if layer in outcome.failing_layers else "·" for layer in LAYERS]
        flagged = agent.get(outcome.defect)
        cells.append("✅" if flagged else ("·" if flagged is not None else "—"))
        lines.append(f"| `{defect.id}` | {defect.breaks} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", help="run just these defect ids")
    parser.add_argument("--jobs", type=int, default=1, help="pytest-xdist workers per run")
    parser.add_argument(
        "--agent-results",
        nargs="*",
        metavar="DEFECT=PATH",
        help="an `mcpqa evals compare --json` file per agent-level defect",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--json-out", type=Path, default=ROOT / "docs/defect-matrix.json")
    parser.add_argument(
        "--render-only", action="store_true", help="rebuild the pages from --json-out without running"
    )
    args = parser.parse_args()

    if args.render_only:
        previous, baseline_s, generated = load_outcomes(args.json_out)
        return write_pages(
            previous,
            agent_layer(args.agent_results),
            baseline_s=baseline_s,
            generated=generated,
            out=args.out,
            json_out=args.json_out,
        )

    defects = args.only or list(REGISTRY)
    results_path = ROOT / ".mcpqa-results.json"
    outcomes: list[Outcome] = []

    baseline = run_suite("", jobs=args.jobs, results_path=results_path)
    print(f"baseline: {'clean' if not baseline.failing_tests else 'FAILING'} ({baseline.duration_s:.0f}s)")
    if baseline.failing_tests:
        print("  the suite must be green before the matrix means anything:")
        for test in baseline.failing_tests[:10]:
            print(f"    {test}")
        return 1

    for index, defect in enumerate(defects, start=1):
        outcome = run_suite(defect, jobs=args.jobs, results_path=results_path)
        outcomes.append(outcome)
        layers = ",".join(str(layer) for layer in sorted(outcome.failing_layers)) or "NONE"
        print(
            f"[{index}/{len(defects)}] {defect:32} caught by layer(s) {layers:8} "
            f"({len(outcome.failing_tests)} tests, {outcome.duration_s:.0f}s)"
        )

    return write_pages(
        outcomes,
        agent_layer(args.agent_results),
        baseline_s=baseline.duration_s,
        generated=time.strftime("%Y-%m-%d"),
        out=args.out,
        json_out=args.json_out,
    )


def write_pages(
    outcomes: list[Outcome],
    agent: dict[str, bool],
    *,
    baseline_s: float,
    generated: str,
    out: Path,
    json_out: Path,
) -> int:
    outcomes = [outcome for outcome in outcomes if outcome.defect != "(none)"]
    table = render(outcomes, agent)
    hits = sum(1 for outcome in outcomes if caught_as_expected(outcome, agent))

    def evaluated(outcome: Outcome) -> bool:
        return REGISTRY[outcome.defect].expected_layer < 5 or outcome.defect in agent

    missed = [
        outcome
        for outcome in outcomes
        if evaluated(outcome) and not outcome.detected and not agent.get(outcome.defect)
    ]
    # A defect meant for layers 1-4 that nothing caught is a hole in the kit. An
    # agent-level defect the evaluation did not flag is a statement about the model
    # and the sample size that run had — reported, but it does not fail the matrix.
    undetected = [outcome.defect for outcome in missed if REGISTRY[outcome.defect].expected_layer < 5]
    unflagged = [outcome.defect for outcome in missed if REGISTRY[outcome.defect].expected_layer == 5]
    verdict = f"Caught by the layer that was supposed to catch them: {hits}/{len(outcomes)}."
    if not all(evaluated(outcome) for outcome in outcomes):
        verdict += " Layer 5 was not evaluated in this run; its defects need a model."
    if unflagged:
        verdict += (
            f" Not flagged by the recorded agent evaluation: {', '.join(f'`{d}`' for d in unflagged)}"
            " — the agent evaluation results give the effect sizes and the reasons."
        )
    if undetected:
        verdict += f"\n\n**Not caught by any layer: {', '.join(undetected)}**"

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "<!-- generated by tools/defect_matrix.py; do not edit by hand -->\n"
        "# Seeded defect matrix\n\n"
        f"Generated {generated} · {len(outcomes)} defects · "
        f"baseline suite green in {baseline_s:.0f}s\n\n"
        f"{LEGEND}\n\n{table}\n\n{verdict}\n"
    )
    json_out.write_text(
        json.dumps(
            {
                "generated": generated,
                "baseline_seconds": round(baseline_s, 1),
                "defects": {
                    outcome.defect: {
                        "layers": sorted(outcome.failing_layers),
                        "expected_layer": REGISTRY[outcome.defect].expected_layer,
                        "failing_tests": outcome.failing_tests[:20],
                        "seconds": round(outcome.duration_s, 1),
                        "agent_flagged": agent.get(outcome.defect),
                    }
                    for outcome in outcomes
                },
            },
            indent=2,
        )
        + "\n"
    )
    readme = ROOT / "README.md"
    start, end = "<!-- MATRIX:START -->", "<!-- MATRIX:END -->"
    text = readme.read_text()
    if out.resolve() == DEFAULT_OUT and start in text and end in text:
        head, rest = text.split(start, 1)
        _, tail = rest.split(end, 1)
        readme.write_text(f"{head}{start}\n{LEGEND}\n\n{table}\n\n{verdict}\n{end}{tail}")

    def shown(path: Path) -> str:
        return str(path.resolve().relative_to(ROOT)) if path.resolve().is_relative_to(ROOT) else str(path)

    print(f"\nwrote {shown(out)} and {shown(json_out)}")
    if undetected:
        print(f"UNDETECTED by any layer: {', '.join(undetected)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
