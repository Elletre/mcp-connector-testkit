"""The two HTTP-based model adapters, against a mocked server.

What matters here is the conversation shape each API expects — how a tool call
comes back, how its result must be returned, how usage is counted — because a
mistake there shows up as a model that "cannot use tools", which is a very
different finding.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx
import pytest

from mcpqa.evals.adapters import openai_compat, prompted_json
from mcpqa.evals.adapters.base import ToolCall, ToolSpec
from mcpqa.evals.adapters.openai_compat import OpenAICompatibleAdapter
from mcpqa.evals.adapters.prompted_json import PromptedJsonAdapter

TOOLS = [ToolSpec("list_items", "List items.", {"type": "object", "properties": {}})]


def mock_server(
    monkeypatch: pytest.MonkeyPatch, module: Any, replies: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=replies.pop(0))

    real_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(module.httpx, "Client", client)
    return requests


def test_openai_adapter_round_trips_a_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = mock_server(
        monkeypatch,
        openai_compat,
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "list_items", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 7},
            },
            {
                "choices": [{"message": {"role": "assistant", "content": "alpha and beta"}}],
                "usage": {"prompt_tokens": 70, "completion_tokens": 5},
            },
        ],
    )
    adapter = OpenAICompatibleAdapter(model="local-model", base_url="http://llm.test/v1")
    adapter.start(system="sys", tools=TOOLS, user="what do you hold?")

    assert adapter.next_turn().tool_call == ToolCall("list_items", {})
    adapter.add_tool_result(ToolCall("list_items", {}), "alpha, beta", is_error=False)
    assert adapter.next_turn().text == "alpha and beta"

    first, second = requests
    assert first["tools"][0]["function"]["name"] == "list_items"
    tool_message = second["messages"][-1]
    assert tool_message == {"role": "tool", "tool_call_id": "call_1", "content": "alpha, beta"}
    assert adapter.usage.input_tokens == 120 and adapter.usage.requests == 2


def test_openai_adapter_reports_unparseable_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_server(
        monkeypatch,
        openai_compat,
        [
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"id": "c", "function": {"name": "list_items", "arguments": "{not json"}}
                            ]
                        }
                    }
                ]
            }
        ],
    )
    adapter = OpenAICompatibleAdapter(model="m", base_url="http://llm.test/v1")
    adapter.start(system="s", tools=TOOLS, user="u")
    assert adapter.next_turn().malformed is not None


def test_prompted_adapter_describes_tools_and_feeds_results_back(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = mock_server(
        monkeypatch,
        prompted_json,
        [
            {
                "message": {"content": '{"tool": "list_items", "arguments": {}}'},
                "prompt_eval_count": 90,
                "eval_count": 9,
            },
            {
                "message": {"content": '{"answer": "alpha and beta"}'},
                "prompt_eval_count": 120,
                "eval_count": 6,
            },
        ],
    )
    adapter = PromptedJsonAdapter(model="llama3:latest", host="http://ollama.test")
    adapter.start(system="sys", tools=TOOLS, user="what do you hold?")

    assert adapter.next_turn().tool_call == ToolCall("list_items", {})
    adapter.add_tool_result(ToolCall("list_items", {}), "alpha, beta", is_error=True)
    assert adapter.next_turn().text == "alpha and beta"

    first, second = requests
    system_prompt = first["messages"][0]["content"]
    assert "list_items" in system_prompt and '"answer"' in system_prompt
    assert first["format"] == "json" and first["options"]["temperature"] == 0.0
    assert first["options"]["num_ctx"] >= 8192, "the default window truncates long prompts from the front"
    assert first["options"]["num_predict"] > 0, "an unbounded turn can run forever in JSON mode"
    assert second["messages"][-1]["content"].startswith("TOOL ERROR (list_items)")
    assert adapter.usage.output_tokens == 15
