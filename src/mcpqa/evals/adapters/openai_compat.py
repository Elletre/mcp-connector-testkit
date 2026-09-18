"""Native tool calling over the OpenAI-compatible chat completions API.

Most local runners (Ollama's /v1, llama.cpp's server, vLLM, LM Studio) and many
hosted APIs speak this dialect, so one adapter covers every tool-capable model
that is not Claude.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx

from .base import ToolCall, ToolSpec, Turn, Usage


@dataclass
class OpenAICompatibleAdapter:
    model: str
    base_url: str = field(
        default_factory=lambda: os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")
    )
    api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", "not-needed"))
    temperature: float = 0.0
    timeout_s: float = 180.0

    name: str = field(init=False)
    usage: Usage = field(default_factory=Usage, init=False)
    messages: list[dict[str, Any]] = field(default_factory=list, init=False)
    tools: list[dict[str, Any]] = field(default_factory=list, init=False)
    _pending_call_id: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.name = f"openai-compatible/{self.model}" + (
            f" at temperature {self.temperature:g}" if self.temperature else ""
        )

    def start(self, *, system: str, tools: list[ToolSpec], user: str) -> None:
        self.messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema or {"type": "object", "properties": {}},
                },
            }
            for tool in tools
        ]

    def next_turn(self) -> Turn:
        payload = {
            "model": self.model,
            "messages": self.messages,
            "tools": self.tools,
            "temperature": self.temperature,
        }
        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            body = response.json()

        usage = body.get("usage") or {}
        self.usage.add(
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )
        message = body["choices"][0]["message"]
        self.messages.append(message)
        calls = message.get("tool_calls") or []
        if calls:
            call = calls[0]
            self._pending_call_id = call.get("id")
            raw_arguments = call["function"].get("arguments") or "{}"
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            except json.JSONDecodeError:
                return Turn(malformed=f"tool arguments are not JSON: {raw_arguments[:160]}")
            return Turn(tool_call=ToolCall(name=call["function"]["name"], arguments=dict(arguments or {})))
        return Turn(text=str(message.get("content") or ""))

    def add_tool_result(self, call: ToolCall, result: str, *, is_error: bool) -> None:
        if self._pending_call_id is None:
            self.messages.append({"role": "user", "content": result})
            return
        prefix = "ERROR: " if is_error else ""
        self.messages.append(
            {"role": "tool", "tool_call_id": self._pending_call_id, "content": prefix + result}
        )
        self._pending_call_id = None
