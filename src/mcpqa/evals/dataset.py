"""The evaluation cases, and what counts as passing one.

Expectations are written against *outcomes* wherever possible — which tools ran
and what the mailbox looks like afterwards — rather than against the words in
the answer. "Trash the newsletter" is graded by checking that exactly that
message moved to the trash, which is true regardless of how the model phrases
its reply, and stays true when the wording changes next month.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CATEGORIES = (
    "tool_selection",
    "argument_fidelity",
    "multi_step",
    "safety_restraint",
    "prompt_injection",
    "error_recovery",
    "context_pressure",
)


@dataclass(frozen=True)
class Fault:
    """An upstream failure injected for the duration of one case."""

    path: str
    status: int | None = None
    delay_ms: int | None = None
    times: int | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Expectation:
    first_tool: str | None = None
    tools_used: tuple[str, ...] = ()
    """Every one of these tools must have been called."""
    tools_used_any: tuple[str, ...] = ()
    """At least one of these must have been called: the model has to have looked."""
    forbidden_tools: tuple[str, ...] = ()
    arguments_contain: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Tool argument -> any of these substrings must appear in its value."""
    state: str | None = None
    """`unchanged` when the episode must leave the mailbox exactly as it was."""
    state_added: tuple[str, ...] = ()
    state_removed: tuple[str, ...] = ()
    max_state_removed: int | None = None
    answer_contains: tuple[str, ...] = ()
    answer_contains_any: tuple[str, ...] = ()
    answer_excludes: tuple[str, ...] = ()
    max_tool_output_chars: int | None = None


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    prompt: str
    expect: Expectation
    faults: tuple[Fault, ...] = ()
    note: str = ""
    null_agent_ok: bool = False
    """True only when doing nothing is a correct response — restraint cases.

    Every other case must fail for an agent that calls no tools and says
    nothing useful; a unit test enforces it."""

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"{self.id}: unknown category {self.category!r}")


def _expectation(raw: dict[str, Any]) -> Expectation:
    return Expectation(
        first_tool=raw.get("first_tool"),
        tools_used=tuple(raw.get("tools_used", ())),
        tools_used_any=tuple(raw.get("tools_used_any", ())),
        forbidden_tools=tuple(raw.get("forbidden_tools", ())),
        arguments_contain={
            key: tuple(values) for key, values in (raw.get("arguments_contain") or {}).items()
        },
        state=raw.get("state"),
        state_added=tuple(raw.get("state_added", ())),
        state_removed=tuple(raw.get("state_removed", ())),
        max_state_removed=raw.get("max_state_removed"),
        answer_contains=tuple(raw.get("answer_contains", ())),
        answer_contains_any=tuple(raw.get("answer_contains_any", ())),
        answer_excludes=tuple(raw.get("answer_excludes", ())),
        max_tool_output_chars=raw.get("max_tool_output_chars"),
    )


def load_cases(path: Path) -> list[Case]:
    """Load every case from a YAML file or a directory of them."""
    files = sorted(path.glob("*.yaml")) if path.is_dir() else [path]
    cases: list[Case] = []
    seen: set[str] = set()
    for file in files:
        payload = yaml.safe_load(file.read_text(encoding="utf-8")) or []
        for raw in payload:
            case = Case(
                id=raw["id"],
                category=raw["category"],
                prompt=raw["prompt"],
                expect=_expectation(raw.get("expect") or {}),
                faults=tuple(Fault(**fault) for fault in raw.get("faults", ())),
                note=raw.get("note", ""),
                null_agent_ok=bool(raw.get("null_agent_ok", False)),
            )
            if case.id in seen:
                raise ValueError(f"duplicate case id: {case.id}")
            seen.add(case.id)
            cases.append(case)
    return cases
