"""Summaries, comparisons and the files a run leaves behind.

A run writes three things: `trials.jsonl` (every episode, for anyone who wants
to disagree with a grade), `summary.json` (the numbers, machine-readable) and
`report.md` (the numbers, for people). Every figure quoted anywhere else in the
repository is copied from one of these files.
"""

from __future__ import annotations

import json
import platform
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .dataset import CATEGORIES
from .runner import RunResult, Trial
from .stats import Interval, bootstrap_delta, by_case, case_level, pass_hat_k, repeat_agreement


def _rate(trials: list[Trial], attribute: str = "passed") -> Interval:
    return case_level(by_case((trial.case_id, bool(getattr(trial, attribute))) for trial in trials))


def summarise(run: RunResult) -> dict[str, Any]:
    trials = run.graded
    categories: dict[str, list[Trial]] = defaultdict(list)
    for trial in trials:
        categories[trial.category].append(trial)
    cases = by_case((trial.case_id, trial.passed) for trial in trials)
    overall = _rate(trials)
    return {
        "model": run.model,
        "environment": run.environment,
        "started_at": run.started_at,
        "duration_s": round(run.duration_s, 1),
        "repeats": run.repeats,
        "machine": f"{platform.system()} {platform.machine()}",
        "trials": len(trials),
        "errored_trials": len(run.trials) - len(trials),
        "stopped_early": run.stopped_early,
        "cases": len(cases),
        "pass_rate": vars(overall),
        "pass_hat_k": pass_hat_k(cases),
        "repeat_agreement": repeat_agreement(cases),
        "unsafe_rate": vars(_rate(trials, "unsafe")),
        "protocol_failure_rate": vars(_rate(trials, "protocol_failure")),
        "mean_input_tokens": round(sum(t.input_tokens for t in trials) / max(1, len(trials))),
        "mean_output_tokens": round(sum(t.output_tokens for t in trials) / max(1, len(trials))),
        "mean_seconds": round(sum(t.seconds for t in trials) / max(1, len(trials)), 2),
        "episode_seconds": round(sum(t.seconds for t in trials), 1),
        "categories": {
            name: {"n": len(group), "cases": len({t.case_id for t in group}), "pass_rate": vars(_rate(group))}
            for name, group in sorted(categories.items(), key=lambda item: CATEGORIES.index(item[0]))
        },
        "case_outcomes": dict(sorted(cases.items())),
    }


@dataclass(frozen=True)
class Comparison:
    overall: Interval
    categories: dict[str, Interval]

    @property
    def regressed(self) -> bool:
        """Significantly worse overall: the whole interval sits below zero."""
        return self.overall.high < 0

    def regressed_categories(self) -> list[str]:
        return [name for name, delta in self.categories.items() if delta.high < 0]


def compare(baseline: RunResult, candidate: RunResult) -> Comparison:
    def grouped(run: RunResult, category: str | None = None) -> dict[str, list[bool]]:
        return by_case(
            (trial.case_id, trial.passed)
            for trial in run.graded
            if category is None or trial.category == category
        )

    names = sorted({trial.category for trial in baseline.trials} | {t.category for t in candidate.trials})
    return Comparison(
        overall=bootstrap_delta(grouped(baseline), grouped(candidate)),
        categories={
            name: bootstrap_delta(grouped(baseline, name), grouped(candidate, name)) for name in names
        },
    )


def render_markdown(summary: dict[str, Any], trials: list[Trial]) -> str:
    def rate(block: dict[str, float]) -> str:
        return f"{block['estimate']:.0%} ({block['low']:.0%}–{block['high']:.0%})"

    lines = [
        f"# Agent evaluation — {summary['model']}",
        "",
        f"Environment: `{summary['environment']}` · {summary['cases']} cases × {summary['repeats']} "
        f"repeats · run {summary['started_at']} on {summary['machine']} · "
        f"{summary.get('episode_seconds', summary['duration_s'])}s of model and tool time",
        "",
        "| Measure | Value |",
        "| --- | --- |",
        f"| Pass rate (95% interval over cases) | {rate(summary['pass_rate'])} |",
        f"| pass^k — cases passed on every repeat | {summary['pass_hat_k']:.0%} |",
        f"| Cases with the same verdict on every repeat | {summary.get('repeat_agreement', 0):.0%} |",
        f"| Unsafe trials (forbidden tool, unexpected state change) | {rate(summary['unsafe_rate'])} |",
        f"| Protocol failures (no readable turn) | {rate(summary['protocol_failure_rate'])} |",
        "| Mean tokens in / out per trial | "
        f"{summary['mean_input_tokens']} / {summary['mean_output_tokens']} |",
        f"| Mean seconds per trial | {summary['mean_seconds']} |",
        f"| Trials that could not run (excluded above) | {summary['errored_trials']} |",
        "",
        "## By category",
        "",
        "| Category | Cases | Trials | Pass rate |",
        "| --- | ---: | ---: | --- |",
    ]
    for name, block in summary["categories"].items():
        lines.append(f"| {name} | {block.get('cases', '')} | {block['n']} | {rate(block['pass_rate'])} |")

    lines += ["", "## By case", "", "| Case | Passed | First failure |", "| --- | :-: | --- |"]
    first_failure: dict[str, str] = {}
    for trial in trials:
        if trial.error is None and not trial.passed and trial.case_id not in first_failure:
            first_failure[trial.case_id] = trial.failures[0] if trial.failures else ""
    for case_id, outcomes in summary["case_outcomes"].items():
        mark = f"{sum(outcomes)}/{len(outcomes)}"
        reason = first_failure.get(case_id, "").replace("|", "\\|")
        lines.append(f"| `{case_id}` | {mark} | {reason} |")
    return "\n".join(lines) + "\n"


def write_summary(run: RunResult, directory: Path) -> dict[str, Any]:
    """Write summary.json and report.md; the trial log is written by the caller."""
    directory.mkdir(parents=True, exist_ok=True)
    summary = summarise(run)
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (directory / "report.md").write_text(render_markdown(summary, run.trials))
    return summary


def write(run: RunResult, directory: Path) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    summary = summarise(run)
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    with (directory / "trials.jsonl").open("w") as handle:
        for trial in run.trials:
            handle.write(json.dumps(trial.as_dict(), ensure_ascii=False) + "\n")
    (directory / "report.md").write_text(render_markdown(summary, run.trials))
    return summary


def load(directory: Path) -> RunResult:
    summary = json.loads((directory / "summary.json").read_text())
    trials = [
        Trial(**json.loads(line))
        for line in (directory / "trials.jsonl").read_text().splitlines()
        if line.strip()
    ]
    return RunResult(
        model=summary["model"],
        environment=summary["environment"],
        repeats=summary["repeats"],
        started_at=summary["started_at"],
        trials=trials,
        duration_s=summary["duration_s"],
        stopped_early=summary.get("stopped_early"),
    )


def load_trials(path: Path) -> list[Trial]:
    """Trials from a (possibly partial) `trials.jsonl`, for resuming a run."""
    if not path.exists():
        return []
    return [Trial(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
