"""A client for the provider's /_control plane.

It implements the two capabilities the test kit asks of an environment —
`snapshot()` (a side-effect oracle) and `clear/fail/delay/calls` (fault
control) — plus the provider-specific extras the layer 3 and 4 suites use.
The kit never imports this module; it only relies on the method names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx2 as httpx

ALICE = "alice@acme.test"


@dataclass(frozen=True)
class AuditRecord:
    """One upstream call, in the shape the kit's `UpstreamCall` protocol expects."""

    seq: int
    at: float
    started_at: float
    method: str
    path: str
    status: int
    subject: str | None = None
    fault: str | None = None


@dataclass
class ProviderControl:
    """Implements both `SideEffectOracle` and `FaultControl` over /_control."""

    base_url: str
    account: str = ALICE

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=10)

    # -- SideEffectOracle ---------------------------------------------------
    def snapshot(self) -> frozenset[str]:
        with self._client() as client:
            response = client.get("/_control/state", params={"account": self.account})
        return frozenset(response.json()["facts"])

    # -- FaultControl -------------------------------------------------------
    def clear(self) -> None:
        with self._client() as client:
            client.post("/_control/faults", json={"rules": []})

    def fail(
        self,
        *,
        path: str,
        status: int = 503,
        times: int | None = None,
        method: str | None = None,
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._rule(
            {
                "id": f"fail-{status}",
                "kind": "status",
                "path_pattern": path,
                "status": status,
                "times": times,
                "method": method,
                "body": body,
                "headers": headers or {},
            }
        )

    def delay(self, *, path: str, ms: int, times: int | None = None) -> None:
        self._rule(
            {
                "id": f"delay-{ms}",
                "kind": "delay",
                "path_pattern": path,
                "delay_ms": ms,
                "times": times,
            }
        )

    def delay_response(self, *, path: str, ms: int, times: int | None = None) -> None:
        """Let the request succeed upstream, then stall the answer to the caller."""
        self._rule(
            {
                "id": f"delay-response-{ms}",
                "kind": "delay_response",
                "path_pattern": path,
                "delay_ms": ms,
                "times": times,
            }
        )

    def hang(self, *, path: str, times: int | None = 1) -> None:
        self._rule({"id": "hang", "kind": "hang", "path_pattern": path, "times": times})

    def garbage(self, *, path: str, times: int | None = 1) -> None:
        self._rule(
            {
                "id": "garbage",
                "kind": "garbage",
                "path_pattern": path,
                "status": 502,
                "times": times,
            }
        )

    def _rule(self, rule: dict[str, Any]) -> None:
        with self._client() as client:
            client.post("/_control/faults", json={"rules": [rule]})

    def calls(self, since: int = 0) -> list[AuditRecord]:
        with self._client() as client:
            entries = client.get("/_control/audit", params={"since": since}).json()["entries"]
        return [
            AuditRecord(
                seq=entry["seq"],
                at=entry["at"],
                started_at=entry["started_at"],
                method=entry["method"],
                path=entry["path"],
                status=entry["status"],
                subject=entry["subject"],
                fault=entry["fault"],
            )
            for entry in entries
        ]

    # -- provider-specific helpers used by layer 3 and 4 --------------------
    def reset(self) -> None:
        with self._client() as client:
            client.post("/_control/reset")

    def rate_limit(self, *, capacity: float, refill_per_sec: float) -> None:
        with self._client() as client:
            client.post(
                "/_control/rate_limit",
                json={"capacity": capacity, "refill_per_sec": refill_per_sec},
            )

    def drift(self, profile: str | None) -> None:
        with self._client() as client:
            client.post("/_control/drift", json={"profile": profile})

    def expire_token(self, token: str) -> None:
        with self._client() as client:
            client.post(f"/_control/tokens/{token}/expire")

    def revoke_refresh(self, token: str) -> None:
        with self._client() as client:
            client.post(f"/_control/tokens/{token}/revoke-refresh")
