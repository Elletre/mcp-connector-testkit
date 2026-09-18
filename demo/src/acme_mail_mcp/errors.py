"""Upstream failures, and how they are told to a model.

The rule the connector follows: a tool error is a sentence the model can act
on. "Upstream returned 429" is not actionable; "Acme Mail is rate limiting this
account, try again in 2 seconds" is. And no error text ever carries a token.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ErrorKind = Literal[
    "rate_limited",
    "unauthorized",
    "forbidden",
    "not_found",
    "invalid_request",
    "unavailable",
    "timeout",
    "malformed",
]


@dataclass
class UpstreamError(Exception):
    kind: ErrorKind
    message: str
    status: int | None = None
    retry_after: float | None = None
    code: str | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def user_message(self) -> str:
        """The sentence a model receives when this error ends the call."""
        if self.kind == "rate_limited":
            wait = f" Retry in about {self.retry_after:.0f}s." if self.retry_after else ""
            return f"Acme Mail is rate limiting this account.{wait}"
        if self.kind == "unauthorized":
            return (
                "The Acme Mail account is no longer connected: its access could not be renewed. "
                "Reconnect the account, then try again."
            )
        if self.kind == "forbidden":
            return f"This connection is not permitted to do that. {self.message}"
        if self.kind == "not_found":
            return self.message
        if self.kind == "invalid_request":
            return self.message
        if self.kind == "timeout":
            return "Acme Mail did not respond in time. The request was not completed."
        if self.kind == "malformed":
            return f"{self.message}. This usually means the provider changed its API."
        return "Acme Mail is temporarily unavailable. The request was not completed."
