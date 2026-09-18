"""Fault injection and schema drift for the Acme Mail provider.

A connector is mostly a thing that survives someone else's bad day: rate
limits, 503s, slow responses, a field that changed name last night. None of
that can be tested against a provider that always behaves, so the provider
here can be told to misbehave in specific, repeatable ways.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

FaultKind = Literal["status", "delay", "delay_response", "hang", "garbage"]
"""`delay` stalls before the handler runs; `delay_response` runs it first and
then stalls, which is how a write gets committed upstream while the caller
times out and never learns that it did."""

DRIFT_PROFILES = {
    "rename_subject_to_title": "`subject` is delivered as `title`",
    "received_at_epoch": "`received_at` becomes an integer epoch instead of RFC 3339",
    "drop_snippet": "`snippet` disappears from search results",
}


@dataclass
class FaultRule:
    id: str
    kind: FaultKind
    path_pattern: str = ".*"
    method: str | None = None
    status: int = 503
    body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    delay_ms: int = 0
    times: int | None = None
    """How many matching requests to affect; `None` means every one."""

    remaining: int | None = None

    def __post_init__(self) -> None:
        self.remaining = self.times
        self._regex = re.compile(self.path_pattern)

    def matches(self, method: str, path: str) -> bool:
        if self.remaining is not None and self.remaining <= 0:
            return False
        if self.method and self.method.upper() != method.upper():
            return False
        return bool(self._regex.search(path))

    def consume(self) -> None:
        if self.remaining is not None:
            self.remaining -= 1


@dataclass
class FaultEngine:
    rules: list[FaultRule] = field(default_factory=list)
    drift: str | None = None

    def reset(self) -> None:
        self.rules = []
        self.drift = None

    def set_rules(self, rules: list[FaultRule]) -> None:
        self.rules = rules

    def next_for(self, method: str, path: str) -> FaultRule | None:
        for rule in self.rules:
            if rule.matches(method, path):
                rule.consume()
                return rule
        return None

    def set_drift(self, profile: str | None) -> None:
        if profile is not None and profile not in DRIFT_PROFILES:
            raise ValueError(f"unknown drift profile: {profile}")
        self.drift = profile

    def apply_drift(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Rewrite one message payload the way a provider's v2 rollout would."""
        if self.drift is None:
            return payload
        drifted = dict(payload)
        if self.drift == "rename_subject_to_title" and "subject" in drifted:
            drifted["title"] = drifted.pop("subject")
        elif self.drift == "received_at_epoch" and "received_at" in drifted:
            from datetime import datetime

            drifted["received_at"] = int(datetime.fromisoformat(drifted["received_at"]).timestamp())
        elif self.drift == "drop_snippet":
            drifted.pop("snippet", None)
        return drifted


@dataclass
class TokenBucket:
    """Per-token rate limiting, with the `Retry-After` a real API would send."""

    capacity: float = 1000.0
    refill_per_sec: float = 1000.0
    tokens: float = 1000.0
    updated_at: float = field(default_factory=time.monotonic)

    def take(self) -> float | None:
        """Return `None` when allowed, or the seconds to wait when limited."""
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated_at) * self.refill_per_sec)
        self.updated_at = now
        if self.tokens >= 1:
            self.tokens -= 1
            return None
        return max(0.001, (1 - self.tokens) / self.refill_per_sec)


@dataclass
class RateLimiter:
    capacity: float = 1000.0
    refill_per_sec: float = 1000.0
    buckets: dict[str, TokenBucket] = field(default_factory=dict)

    def reset(self) -> None:
        self.capacity = 1000.0
        self.refill_per_sec = 1000.0
        self.buckets = {}

    def configure(self, *, capacity: float, refill_per_sec: float) -> None:
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self.buckets = {}

    def take(self, key: str) -> float | None:
        bucket = self.buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(
                capacity=self.capacity, refill_per_sec=self.refill_per_sec, tokens=self.capacity
            )
            self.buckets[key] = bucket
        return bucket.take()
