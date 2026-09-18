"""Record a model's decisions once; replay them forever.

Continuous integration has no model in it, and should not. What it can do is
replay the decisions a real model made during a recorded run — every tool
call, every answer — against the current connector, and grade the result
again. If the connector still behaves the same, the grades match the recording;
if a change broke something the model relied on, they do not.

This tests the evaluation pipeline and the connector together, deterministically,
for free.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import ModelAdapter, ToolCall, ToolSpec, Turn, Usage


def turn_to_dict(turn: Turn) -> dict[str, Any]:
    if turn.malformed is not None:
        return {"malformed": turn.malformed}
    if turn.tool_call is not None:
        return {"tool": turn.tool_call.name, "arguments": turn.tool_call.arguments}
    return {"answer": turn.text or ""}


def turn_from_dict(data: dict[str, Any]) -> Turn:
    if "malformed" in data:
        return Turn(malformed=str(data["malformed"]))
    if "tool" in data:
        return Turn(tool_call=ToolCall(name=str(data["tool"]), arguments=dict(data.get("arguments") or {})))
    return Turn(text=str(data.get("answer", "")))


@dataclass
class RecordingAdapter:
    """Wraps a real adapter and writes down every turn it takes."""

    inner: ModelAdapter
    sink: list[dict[str, Any]]
    repeat: int = 1
    name: str = field(init=False)
    _prompt: str = field(default="", init=False)
    _turns: list[dict[str, Any]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.name = self.inner.name

    @property
    def usage(self) -> Usage:
        return self.inner.usage

    def start(self, *, system: str, tools: list[ToolSpec], user: str) -> None:
        self._prompt = user
        self._turns = []
        self.sink.append({"prompt": user, "repeat": self.repeat, "turns": self._turns})
        self.inner.start(system=system, tools=tools, user=user)

    def next_turn(self) -> Turn:
        turn = self.inner.next_turn()
        self._turns.append(turn_to_dict(turn))
        return turn

    def add_tool_result(self, call: ToolCall, result: str, *, is_error: bool) -> None:
        self.inner.add_tool_result(call, result, is_error=is_error)


@dataclass
class ReplayAdapter:
    """Plays back recorded turns, keyed by prompt and repeat number."""

    recordings: dict[tuple[str, int], list[dict[str, Any]]]
    repeat: int = 1
    model: str = "replay"
    name: str = field(init=False)
    usage: Usage = field(default_factory=Usage, init=False)
    _queue: list[dict[str, Any]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.name = f"replay/{self.model}"

    @classmethod
    def load(cls, path: Path) -> dict[tuple[str, int], list[dict[str, Any]]]:
        recordings: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            recordings[(entry["prompt"], int(entry["repeat"]))] = list(entry["turns"])
        return dict(recordings)

    def start(self, *, system: str, tools: list[ToolSpec], user: str) -> None:
        key = (user, self.repeat)
        if key not in self.recordings:
            raise KeyError(f"no recording for repeat {self.repeat} of prompt {user[:60]!r}")
        self._queue = list(self.recordings[key])

    def next_turn(self) -> Turn:
        self.usage.add()
        if not self._queue:
            return Turn(text="")
        return turn_from_dict(self._queue.pop(0))

    def add_tool_result(self, call: ToolCall, result: str, *, is_error: bool) -> None:
        return None
