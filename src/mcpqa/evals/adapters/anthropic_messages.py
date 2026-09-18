"""Claude, through the Messages API with native tool use.

Three details worth knowing if you change this file:

* The assistant turn is appended back *exactly as returned* — every content
  block, thinking included. Current Claude models think by default, and a
  tool-use conversation that drops those blocks is not the conversation the
  model had.
* Claude may ask for several tools in one turn. The agent loop hands out one
  call at a time, so the adapter queues them and sends all their results back
  in a single user message, as the API expects.
* `claude-opus-5` rejects sampling parameters, so there is no temperature
  knob here: repeats are how the run measures consistency instead. Refusal
  fallbacks are switched on by default (`fallbacks="default"`), so a request a
  safety classifier declines is re-run server-side rather than silently scored
  as a failure of the connector.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import ToolCall, ToolSpec, Turn, Usage

DEFAULT_MODEL = "claude-opus-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class AnthropicAdapter:
    model: str = DEFAULT_MODEL
    max_tokens: int = 16_000
    effort: str | None = None
    """`output_config.effort`; `None` keeps the model's default (`high`)."""
    fallbacks: bool = True
    client: Any = None
    """An `anthropic.Anthropic` client; created from the environment when omitted."""

    name: str = field(init=False)
    usage: Usage = field(default_factory=Usage, init=False)
    system: str = field(default="", init=False)
    tools: list[dict[str, Any]] = field(default_factory=list, init=False)
    messages: list[dict[str, Any]] = field(default_factory=list, init=False)
    _pending: list[Any] = field(default_factory=list, init=False)
    _results: list[dict[str, Any]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.name = f"anthropic/{self.model}"
        if self.client is None:
            import anthropic  # optional dependency: `pip install mcpqa[anthropic]`

            self.client = anthropic.Anthropic()

    def start(self, *, system: str, tools: list[ToolSpec], user: str) -> None:
        self.system = system
        self.tools = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema or {"type": "object", "properties": {}},
            }
            for tool in tools
        ]
        self.messages = [{"role": "user", "content": user}]
        self._pending = []
        self._results = []

    def next_turn(self) -> Turn:
        # Hand out the rest of a parallel batch before asking the model again.
        if self._pending:
            return self._turn_for(self._pending[0])
        if self._results:
            self.messages.append({"role": "user", "content": self._results})
            self._results = []

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "tools": self.tools,
            "messages": self.messages,
        }
        if self.effort:
            request["output_config"] = {"effort": self.effort}
        if self.fallbacks:
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"
            response = self.client.beta.messages.create(**request)
        else:
            response = self.client.messages.create(**request)

        usage = getattr(response, "usage", None)
        self.usage.add(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )
        # Append the assistant turn verbatim: thinking and tool_use blocks included.
        self.messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "refusal":
            return Turn(text="[the model declined this request]")
        tool_uses = [block for block in response.content if getattr(block, "type", None) == "tool_use"]
        if tool_uses:
            self._pending = tool_uses
            return self._turn_for(tool_uses[0])
        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        return Turn(text=text)

    def add_tool_result(self, call: ToolCall, result: str, *, is_error: bool) -> None:
        if not self._pending:
            return
        block = self._pending.pop(0)
        self._results.append(
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
                "is_error": is_error,
            }
        )

    @staticmethod
    def _turn_for(block: Any) -> Turn:
        return Turn(tool_call=ToolCall(name=str(block.name), arguments=dict(block.input or {})))
