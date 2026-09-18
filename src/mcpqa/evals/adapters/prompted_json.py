"""Tool calling for models that do not have it.

Small local models are often completion-only: no `tools` parameter, no
tool-call encoding. They can still be evaluated, by describing the tools in the
prompt and asking for one JSON object per turn. The format failures that come
with that are counted separately from the task failures, because "the model
could not follow the protocol" and "the model picked the wrong tool" are
different findings and only one of them is about the connector.

Written against the Ollama chat API, which every local runner speaks.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx

from .base import ToolCall, ToolSpec, Turn, Usage

PROTOCOL = """
Reply with exactly one JSON object and nothing else.

To use a tool:      {"tool": "<tool name>", "arguments": {<arguments>}}
To answer the user: {"answer": "<your answer>"}

Call one tool at a time and wait for its result. When you have what you need,
answer. Never invent a tool name that is not in the list.
""".strip()


def _render_tools(tools: list[ToolSpec]) -> str:
    lines = []
    for tool in tools:
        schema = json.dumps(tool.input_schema, ensure_ascii=False)
        lines.append(f"- {tool.name}: {tool.description.strip()}\n  arguments schema: {schema}")
    return "\n".join(lines)


@dataclass
class PromptedJsonAdapter:
    """Ollama, driven through a JSON protocol in the prompt."""

    model: str = "llama3:latest"
    host: str = field(default_factory=lambda: os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    temperature: float = 0.0
    seed: int | None = 7
    timeout_s: float = 180.0
    context_tokens: int = 8192
    """Set explicitly: Ollama's default window is smaller than llama3's, and a prompt
    that overflows it is cut from the front — taking the tool descriptions with it."""
    max_output_tokens: int = 2048
    """In JSON mode a small model can emit whitespace forever, and with context
    shifting the server lets it; a bound turns that into a malformed turn."""

    name: str = field(init=False)
    usage: Usage = field(default_factory=Usage, init=False)
    messages: list[dict[str, Any]] = field(default_factory=list, init=False)
    last_raw: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.name = f"ollama/{self.model}" + (
            f" at temperature {self.temperature:g}" if self.temperature else ""
        )

    def start(self, *, system: str, tools: list[ToolSpec], user: str) -> None:
        self.messages = [
            {
                "role": "system",
                "content": f"{system}\n\nTools available:\n{_render_tools(tools)}\n\n{PROTOCOL}",
            },
            {"role": "user", "content": user},
        ]

    def next_turn(self) -> Turn:
        options: dict[str, Any] = {
            "temperature": self.temperature,
            "num_ctx": self.context_tokens,
            "num_predict": self.max_output_tokens,
        }
        if self.seed is not None:
            options["seed"] = self.seed
        payload = {
            "model": self.model,
            "messages": self.messages,
            "stream": False,
            "format": "json",
            "options": options,
        }
        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.post(f"{self.host}/api/chat", json=payload)
            response.raise_for_status()
            body = response.json()

        content = str(body.get("message", {}).get("content", "")).strip()
        self.last_raw = content
        self.messages.append({"role": "assistant", "content": content})
        self.usage.add(
            input_tokens=int(body.get("prompt_eval_count", 0)),
            output_tokens=int(body.get("eval_count", 0)),
        )
        return parse_turn(content)

    def add_tool_result(self, call: ToolCall, result: str, *, is_error: bool) -> None:
        label = "TOOL ERROR" if is_error else "TOOL RESULT"
        self.messages.append({"role": "user", "content": f"{label} ({call.name}):\n{result}"})


def parse_turn(content: str) -> Turn:
    """Read one model turn, being forgiving about shape but not about meaning."""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return Turn(malformed=f"not JSON: {content[:160]}")
    if not isinstance(parsed, dict):
        return Turn(malformed=f"not a JSON object: {content[:160]}")

    if parsed.get("tool"):
        arguments = parsed.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        return Turn(tool_call=ToolCall(name=str(parsed["tool"]), arguments=arguments))

    for key in ("answer", "response", "text"):
        value = parsed.get(key)
        if isinstance(value, str):
            return Turn(text=value)
        if value is not None:
            # The model meant to answer and shaped it as data; keep what it said.
            return Turn(text=json.dumps(value, ensure_ascii=False))
    return Turn(malformed=f"neither a tool call nor an answer: {content[:160]}")
