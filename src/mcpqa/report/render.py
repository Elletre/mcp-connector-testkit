"""Turning a conformance run into something a person or a pipeline can read."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from ..checks.runner import CheckRun, Report

LAYER_NAMES = {1: "Protocol and transport", 2: "Tool contracts and annotations"}
MARKS = {
    ("passed", None): ("✓", "green"),
    ("skipped", None): ("–", "dim"),
    ("failed", "error"): ("✗", "bold red"),
    ("failed", "warning"): ("!", "yellow"),
    ("failed", "info"): ("i", "cyan"),
}


def _mark(run: CheckRun) -> tuple[str, str]:
    key = (run.status, run.severity if run.status == "failed" else None)
    return MARKS[key]


def _shorten(text: str, limit: int = 90) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def print_report(report: Report, console: Console | None = None, *, verbose: bool = False) -> None:
    console = console or Console()
    console.print(f"[bold]mcpqa[/bold] · {report.target}")
    # One table per layer and era, so no column is spent repeating the era, and
    # one check-column width for all of them, so the tables line up down the page.
    longest = max((len(f"{run.check.id} {run.check.title}") for run in report.runs), default=10)
    check_width = min(longest, max(24, int((console.width - 8) * 0.55)))
    groups: dict[tuple[int, str, str], list[CheckRun]] = {}
    for run in sorted(report.runs, key=lambda run: run.check.layer):
        groups.setdefault((run.check.layer, run.era, run.transport), []).append(run)
    for (layer, era, transport), runs in groups.items():
        table = Table(
            title=f"Layer {layer} — {LAYER_NAMES.get(layer, '')} · {era} / {transport}",
            title_justify="left",
            header_style="bold",
            expand=True,
        )
        table.add_column("", width=1)
        table.add_column("Check", no_wrap=True, overflow="ellipsis", width=check_width)
        table.add_column("Result", ratio=1)
        for run in runs:
            symbol, style = _mark(run)
            detail = Text(run.outcome.detail if verbose else _shorten(run.outcome.detail or ""))
            if run.status == "failed":
                detail.stylize(style)
            table.add_row(Text(symbol, style=style), f"{run.check.id} {run.check.title}", detail)
        console.print(table)
    if verbose:
        for run in report.runs:
            if run.status == "failed" and run.outcome.evidence:
                console.rule(f"{run.label()} evidence")
                console.print(run.outcome.evidence, markup=False)
    summary_style = "bold green" if report.ok else "bold red"
    console.print(Text(report.summary(), style=summary_style))


def to_json(report: Report) -> dict[str, Any]:
    return {
        "target": report.target,
        "summary": report.summary(),
        "ok": report.ok,
        "runs": [
            {
                "id": run.check.id,
                "title": run.check.title,
                "layer": run.check.layer,
                "severity": run.severity,
                "era": run.era,
                "transport": run.transport,
                "status": run.status,
                "detail": run.outcome.detail,
                "evidence": run.outcome.evidence,
                "spec": {
                    "version": run.check.ref.version,
                    "url": run.check.ref.url,
                    "quote": run.check.ref.quote,
                },
                "escalation": run.check.escalation or None,
                "elapsed_s": round(run.elapsed_s, 3),
            }
            for run in report.runs
        ],
    }


def to_markdown(report: Report) -> str:
    lines = [f"# mcpqa report — {report.target}", "", f"**{report.summary()}**", ""]
    for layer in sorted({run.check.layer for run in report.runs}):
        lines += [
            f"## Layer {layer} — {LAYER_NAMES.get(layer, '')}",
            "",
            "| | Check | Era | Result |",
            "| --- | --- | --- | --- |",
        ]
        for run in (run for run in report.runs if run.check.layer == layer):
            symbol, _ = _mark(run)
            detail = (run.outcome.detail or "").replace("|", "\\|")
            lines.append(
                f"| {symbol} | [{run.check.id}]({run.check.ref.url}) {run.check.title} | "
                f"{run.era}/{run.transport} | {detail} |"
            )
        lines.append("")
    failures = [run for run in report.runs if run.status == "failed" and run.outcome.evidence]
    if failures:
        lines += ["## Evidence", ""]
        for run in failures:
            lines += [f"### {run.label()}", "", "```", run.outcome.evidence.strip(), "```", ""]
    return "\n".join(lines)


def dumps(report: Report) -> str:
    return json.dumps(to_json(report), indent=2, ensure_ascii=False)
