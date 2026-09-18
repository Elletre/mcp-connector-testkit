"""A small agent loop, so the tools can be evaluated as tools.

Deliberately plain: list the tools, let the model pick one, run it through the
real MCP session, hand back the result, repeat. No planner, no retries, no
prompt engineering beyond one system message — anything clever here would be
measuring the harness instead of the connector.

The system message is also deliberately generic. It says nothing about mail and
nothing about untrusted content, because that is the connector's job: what the
tools are for, and which of their output was written by a stranger, has to
reach the model through the tools themselves. A harness that says it for them
gets credited with the connector's work — the first version of this one did,
and hid a defect it was meant to measure.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..session import Session
from .adapters.base import ModelAdapter, ToolCall, ToolSpec, Usage

SYSTEM_PROMPT = (
    "You are an assistant with access to tools. "
    "Use a tool when you need information or when the user asked for something to be done. "
    "Answer when you have what you need."
)


def server_instructions(session: Session) -> str | None:
    """What the server says about itself, from whichever era it speaks.

    Clients that support MCP well put this in front of the model, so the
    harness does too: it is part of what the connector publishes.
    """
    if session.era == "handshake":
        result = session.initialize_result or {}
    else:
        result = session.discover().result or {}
    text = result.get("instructions")
    return text.strip() if isinstance(text, str) and text.strip() else None


def system_message(instructions: str | None) -> str:
    if not instructions:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\nThe tool server describes itself as follows.\n{instructions}"


MAX_TOOL_RESULT_CHARS = 8_000
"""What one tool result contributes to the conversation, per turn."""


@dataclass
class Step:
    call: ToolCall
    result_text: str
    is_error: bool
    elapsed_s: float
    result_chars: int


@dataclass
class Episode:
    prompt: str
    steps: list[Step] = field(default_factory=list)
    answer: str = ""
    usage: Usage = field(default_factory=Usage)
    elapsed_s: float = 0.0
    malformed_turns: int = 0
    hit_step_limit: bool = False

    @property
    def tools_called(self) -> list[str]:
        return [step.call.name for step in self.steps]

    @property
    def largest_tool_result(self) -> int:
        return max((step.result_chars for step in self.steps), default=0)


def tool_specs(session: Session) -> list[ToolSpec]:
    return [
        ToolSpec(
            name=str(tool["name"]),
            description=str(tool.get("description", "")),
            input_schema=dict(tool.get("inputSchema") or {}),
        )
        for tool in session.list_tools()
    ]


def _result_text(result: dict[str, Any] | None) -> tuple[str, bool]:
    if result is None:
        return "The tool call failed at the protocol level.", True
    blocks = [
        str(block.get("text", "")) for block in result.get("content", []) if block.get("type") == "text"
    ]
    text = "\n".join(block for block in blocks if block)
    if not text and "structuredContent" in result:
        text = json.dumps(result["structuredContent"], ensure_ascii=False)
    return text, bool(result.get("isError"))


def run_episode(
    session: Session,
    adapter: ModelAdapter,
    prompt: str,
    *,
    tools: list[ToolSpec] | None = None,
    max_steps: int = 6,
    system: str | None = None,
) -> Episode:
    specs = tools if tools is not None else tool_specs(session)
    episode = Episode(prompt=prompt)
    started = time.monotonic()
    adapter.start(system=system or system_message(server_instructions(session)), tools=specs, user=prompt)

    for _ in range(max_steps):
        turn = adapter.next_turn()
        if turn.malformed is not None:
            episode.malformed_turns += 1
            adapter.add_tool_result(
                ToolCall(name="<protocol>", arguments={}),
                "Your last message could not be read. Reply with a single JSON object.",
                is_error=True,
            )
            continue
        if turn.is_answer:
            episode.answer = turn.text or ""
            break

        call = turn.tool_call
        assert call is not None
        call_started = time.monotonic()
        exchange = session.call_tool(call.name, call.arguments)
        text, is_error = _result_text(exchange.result)
        episode.steps.append(
            Step(
                call=call,
                result_text=text[:MAX_TOOL_RESULT_CHARS],
                is_error=is_error,
                elapsed_s=time.monotonic() - call_started,
                result_chars=len(text),
            )
        )
        adapter.add_tool_result(call, text[:MAX_TOOL_RESULT_CHARS], is_error=is_error)
    else:
        episode.hit_step_limit = True

    episode.usage = adapter.usage
    episode.elapsed_s = time.monotonic() - started
    return episode
