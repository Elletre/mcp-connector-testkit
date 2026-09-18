"""What the evaluation needs from a model, and nothing more.

Four methods. Everything provider-specific — message shapes, tool-call
encodings, token accounting — stays inside an adapter, so the same cases run
against a hosted model, a local one, or a recording.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolSpec:
    """A tool as the model sees it: exactly what `tools/list` published."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class Turn:
    """One decision: call a tool, or answer."""

    tool_call: ToolCall | None = None
    text: str | None = None
    malformed: str | None = None
    """Set when the model's output could not be read as either — tracked
    separately, because a model that cannot follow the protocol is a different
    problem from one that chooses the wrong tool."""

    @property
    def is_answer(self) -> bool:
        return self.tool_call is None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0

    def add(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.requests += 1


class ModelAdapter(Protocol):
    name: str
    usage: Usage

    def start(self, *, system: str, tools: list[ToolSpec], user: str) -> None:
        """Begin an episode."""

    def next_turn(self) -> Turn:
        """Ask the model what to do next."""

    def add_tool_result(self, call: ToolCall, result: str, *, is_error: bool) -> None:
        """Hand back what the tool returned."""


@dataclass
class RecordedTurn:
    case_id: str
    index: int
    turn: Turn
    raw: str = ""


@dataclass
class Recording:
    """Every decision a model made, so a run can be replayed exactly."""

    model: str
    turns: list[RecordedTurn] = field(default_factory=list)

    def for_case(self, case_id: str) -> list[Turn]:
        return [item.turn for item in sorted(self.turns, key=lambda t: t.index) if item.case_id == case_id]
