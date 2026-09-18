"""The connector's replaceable parts.

Everything the connector does that could plausibly be done *wrong* lives behind
one of these small interfaces: how it walks a cursor, when it retries, how it
formats an error, how it keys a cache, how it validates an upstream response.
The composition root wires the correct implementations; `defects.py` swaps one
of them for a broken variant. That keeps the connector itself free of
`if running_as_a_broken_demo:` branches, and it makes the catalogue of defects
readable as a list of design decisions someone could get wrong.
"""

from __future__ import annotations

import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from .errors import UpstreamError
from .schemas import UpstreamLabels, UpstreamMessage, UpstreamPage

FetchPage = Callable[[str | None, int], Awaitable[UpstreamPage]]
"""Fetch one page: (page_token, page_size) -> page."""


# ------------------------------------------------------------------ pagination


class Paginator(Protocol):
    async def collect(
        self, fetch: FetchPage, *, max_results: int, page_token: str | None, page_size_cap: int
    ) -> tuple[list[Any], str | None]: ...


@dataclass(frozen=True)
class CursorPaginator:
    """Follow `next_page_token` until the caller has what it asked for.

    The provider caps a page at 50 items; a tool asked for 100 has to walk the
    cursor. Stopping after the first page is the single most common connector
    bug there is, and it is invisible: the caller gets a short, plausible,
    wrong answer.
    """

    async def collect(
        self, fetch: FetchPage, *, max_results: int, page_token: str | None, page_size_cap: int
    ) -> tuple[list[Any], str | None]:
        collected: list[Any] = []
        cursor = page_token
        while len(collected) < max_results:
            wanted = min(page_size_cap, max_results - len(collected))
            page = await fetch(cursor, wanted)
            collected.extend(page.messages)
            cursor = page.next_page_token
            if cursor is None:
                break
        return collected[:max_results], cursor


# --------------------------------------------------------------------- retries


@dataclass(frozen=True)
class RetryPolicy:
    """When to try again, and — just as important — when not to.

    A read that times out can be repeated. A send that times out may already
    have delivered the mail, so repeating it is how people receive the same
    message twice.
    """

    max_attempts: int = 3
    base_delay_s: float = 0.2
    max_delay_s: float = 5.0
    jitter: Callable[[], float] = random.random

    def delay_for(self, *, attempt: int, error: UpstreamError, idempotent: bool) -> float | None:
        if attempt >= self.max_attempts:
            return None
        if error.kind == "rate_limited":
            # The provider told us how long to wait. Waiting less is how you
            # turn a rate limit into an outage.
            return min(self.max_delay_s, error.retry_after if error.retry_after is not None else 1.0)
        if error.kind in ("unavailable", "timeout") and idempotent:
            backoff = min(self.max_delay_s, self.base_delay_s * (2 ** (attempt - 1)))
            return backoff * (0.5 + 0.5 * self.jitter())
        return None


# ------------------------------------------------------------------ formatting


@dataclass(frozen=True)
class SecretScrubber:
    """Last line of defence: never let a credential reach a tool result or a log."""

    secrets: tuple[str, ...] = ()

    def scrub(self, text: str) -> str:
        cleaned = text
        for secret in self.secrets:
            if secret and secret in cleaned:
                cleaned = cleaned.replace(secret, "[redacted]")
        return re.sub(r"\bBearer\s+\S+", "Bearer [redacted]", cleaned)


@dataclass(frozen=True)
class DateNormalizer:
    """Pass timestamps through untouched, offset and all.

    "Helpfully" normalising to local time or to a naive string moves messages
    by a day for anyone who travels, and the tests that catch it are the boring
    ones about a flight leaving New York at 23:40.
    """

    def normalize(self, value: str) -> str:
        return value


@dataclass(frozen=True)
class BodyBudget:
    """Cap what one tool call can put into the model's context window."""

    max_chars: int = 20_000

    def apply(self, text: str) -> tuple[str, bool]:
        if len(text) <= self.max_chars:
            return text, False
        kept = text[: self.max_chars]
        note = (
            f"\n\n[truncated by the connector: {len(text) - self.max_chars} more characters, "
            f"{len(text)} in total]"
        )
        return kept + note, True


@dataclass(frozen=True)
class CacheKeyPolicy:
    """Search results are cached; the cache key must start with who is asking."""

    def key(self, subject: str, params: dict[str, Any]) -> str:
        parts = [f"{k}={params[k]!r}" for k in sorted(params)]
        return f"{subject}|{'|'.join(parts)}"


# ------------------------------------------------------------------ validation


@dataclass(frozen=True)
class ResponseValidator:
    """Strict parsing, so a provider's schema change is an error, not a null."""

    def page(self, payload: Any) -> UpstreamPage:
        return self._parse(UpstreamPage, payload)

    def message(self, payload: Any) -> UpstreamMessage:
        return self._parse(UpstreamMessage, payload)

    def labels(self, payload: Any) -> UpstreamLabels:
        return self._parse(UpstreamLabels, payload)

    @staticmethod
    def _parse(model: type, payload: Any) -> Any:
        try:
            return model.model_validate(payload)
        except ValidationError as error:
            fields = ", ".join(".".join(str(part) for part in item["loc"]) for item in error.errors()[:3])
            raise UpstreamError(
                kind="malformed",
                message=f"Acme Mail returned a response this connector does not understand ({fields})",
            ) from error
