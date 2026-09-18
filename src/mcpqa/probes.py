"""What the kit needs from the world around the server under test.

Most of layers 1 and 2 can be run against a bare endpoint. The rest cannot: to
tell a read from a write you need to see the state behind the server, and to
test what a connector does when its upstream misbehaves you need to be able to
make it misbehave. Rather than assume a particular provider, the kit asks for
these two small capabilities and skips the checks that need one when it is not
supplied.

For a real connector, a `SideEffectOracle` is usually "list the objects in a
dedicated test account through the provider's own API", and a `FaultControl` is
a proxy in front of the upstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class StateDiff:
    """What changed between two snapshots, in the vocabulary the kit reasons in."""

    added: frozenset[str]
    removed: frozenset[str]

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)

    @property
    def additive(self) -> bool:
        """Facts appeared and none disappeared: nothing was overwritten or lost."""
        return bool(self.added) and not self.removed

    @property
    def destructive(self) -> bool:
        """Something that existed no longer does."""
        return bool(self.removed)

    def describe(self, limit: int = 4) -> str:
        parts = []
        if self.added:
            parts.append(f"+{len(self.added)} {sorted(self.added)[:limit]}")
        if self.removed:
            parts.append(f"-{len(self.removed)} {sorted(self.removed)[:limit]}")
        return "; ".join(parts) or "no change"


def diff_states(before: frozenset[str], after: frozenset[str]) -> StateDiff:
    return StateDiff(added=frozenset(after - before), removed=frozenset(before - after))


@runtime_checkable
class SideEffectOracle(Protocol):
    """Reads the state behind the server as a set of facts.

    A fact is any string that is true of the system right now — the kit never
    interprets them, it only compares sets. That is enough to decide whether a
    call was read-only (empty diff), additive (nothing removed) or destructive
    (something removed), which is exactly what tool annotations claim.
    """

    def snapshot(self) -> frozenset[str]: ...


@dataclass(frozen=True)
class UpstreamCall:
    """One call the connector made to its upstream, as the control plane saw it."""

    seq: int
    at: float
    """When the upstream finished handling it."""
    started_at: float
    """When it arrived. With `at`, this is what makes overlap visible."""
    method: str
    path: str
    status: int
    subject: str | None = None
    fault: str | None = None


@runtime_checkable
class FaultControl(Protocol):
    """Makes the upstream misbehave on purpose, and reports what was called."""

    def clear(self) -> None: ...

    def fail(
        self,
        *,
        path: str,
        status: int = 503,
        times: int | None = None,
        method: str | None = None,
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None: ...

    def delay(self, *, path: str, ms: int, times: int | None = None) -> None: ...

    def calls(self, since: int = 0) -> list[UpstreamCall]: ...
