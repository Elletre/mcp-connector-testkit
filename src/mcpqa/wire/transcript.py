"""The record of what actually went over the wire.

Every check gets its evidence from here, and a failing check prints the exact
bytes that produced it. A conformance report nobody can reproduce is an opinion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Exchange:
    """One request and whatever came back for it."""

    request: dict[str, Any] | None
    response: dict[str, Any] | None
    raw_request: str = ""
    raw_response: str = ""
    status: int | None = None
    """HTTP status, or `None` on stdio."""
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_s: float = 0.0
    note: str | None = None

    @property
    def method(self) -> str | None:
        return None if self.request is None else str(self.request.get("method"))

    @property
    def result(self) -> dict[str, Any] | None:
        if self.response is None:
            return None
        result = self.response.get("result")
        return result if isinstance(result, dict) else None

    @property
    def error(self) -> dict[str, Any] | None:
        if self.response is None:
            return None
        error = self.response.get("error")
        return error if isinstance(error, dict) else None

    @property
    def error_code(self) -> int | None:
        error = self.error
        return None if error is None else int(error.get("code", 0))

    def is_tool_error(self) -> bool:
        result = self.result
        return bool(result and result.get("isError"))

    def describe(self, limit: int = 600) -> str:
        parts = [f"→ {self.raw_request[:limit]}"]
        if self.status is not None:
            parts.append(f"← HTTP {self.status}")
        parts.append(f"← {self.raw_response[:limit]}")
        if self.note:
            parts.append(f"note: {self.note}")
        return "\n".join(parts)


@dataclass
class Transcript:
    """Everything one session said and heard, plus anything odd about it."""

    exchanges: list[Exchange] = field(default_factory=list)
    stderr: str = ""
    stdout_violations: list[str] = field(default_factory=list)
    """Lines a stdio server wrote to stdout that were not JSON-RPC messages."""
    unsolicited: list[dict[str, Any]] = field(default_factory=list)
    """Messages that arrived without a matching request id (notifications, strays)."""

    def add(self, exchange: Exchange) -> Exchange:
        self.exchanges.append(exchange)
        return exchange

    def last(self) -> Exchange | None:
        return self.exchanges[-1] if self.exchanges else None

    def texts(self) -> list[str]:
        """Every byte the server produced, for scanning (secrets, canaries)."""
        chunks = [exchange.raw_response for exchange in self.exchanges]
        chunks.extend(self.stdout_violations)
        chunks.extend(json.dumps(message) for message in self.unsolicited)
        if self.stderr:
            chunks.append(self.stderr)
        return chunks

    def tail(self, count: int = 3) -> str:
        return "\n\n".join(exchange.describe() for exchange in self.exchanges[-count:])
