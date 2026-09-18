"""The HTTP client that talks to Acme Mail.

Most of a connector's real work is here: presenting a credential, renewing it
when it expires, deciding whether a failure may be retried, and refusing to
guess when the response does not look like what it should.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx2 as httpx

from .components import ResponseValidator, RetryPolicy
from .errors import UpstreamError
from .schemas import (
    UpstreamLabelChange,
    UpstreamLabels,
    UpstreamMessage,
    UpstreamPage,
    UpstreamSendResult,
)


@dataclass
class TokenPair:
    access: str
    refresh: str | None = None


class TokenManager:
    """Holds the upstream credential and renews it exactly once per failure.

    Single-flight: when three tool calls hit an expired token at the same
    moment, one refresh happens and the other two wait for it, instead of three
    refreshes racing and two of them being thrown away.
    """

    def __init__(
        self,
        *,
        base_url: str,
        tokens: TokenPair,
        http: httpx.AsyncClient,
        allow_refresh: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._tokens = tokens
        self._http = http
        self._allow_refresh = allow_refresh
        self._lock = asyncio.Lock()
        self.refresh_count = 0

    @property
    def access_token(self) -> str:
        return self._tokens.access

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(t for t in (self._tokens.access, self._tokens.refresh) if t)

    @property
    def can_refresh(self) -> bool:
        return self._allow_refresh and self._tokens.refresh is not None

    async def refresh(self) -> bool:
        if not self.can_refresh:
            return False
        stale = self._tokens.access
        async with self._lock:
            if self._tokens.access != stale:
                return True  # someone else refreshed while we waited
            response = await self._http.post(
                f"{self._base_url}/oauth/token",
                data={"grant_type": "refresh_token", "refresh_token": self._tokens.refresh},
            )
            if response.status_code != 200:
                return False
            payload = response.json()
            self._tokens.access = str(payload["access_token"])
            self.refresh_count += 1
            return True


@dataclass
class AcmeMailClient:
    base_url: str
    http: httpx.AsyncClient
    tokens: TokenManager
    retry: RetryPolicy
    validator: ResponseValidator
    timeout_s: float = 10.0
    peek_on_read: bool = True
    """Read with `peek=true`, so fetching a message does not mark it read."""
    attempts: list[str] = field(default_factory=list)
    """Every upstream call this client made, for tests that count retries."""

    # ------------------------------------------------------------- operations
    async def search_page(
        self,
        *,
        query: str | None,
        label: str | None,
        page_token: str | None,
        page_size: int,
    ) -> UpstreamPage:
        params: dict[str, Any] = {"page_size": page_size}
        if query:
            params["q"] = query
        if label:
            params["label"] = label
        if page_token:
            params["page_token"] = page_token
        payload = await self._request("GET", "/v1/messages", params=params, idempotent=True)
        return self.validator.page(payload)

    async def get_message(self, message_id: str) -> UpstreamMessage:
        payload = await self._request(
            "GET",
            f"/v1/messages/{message_id}",
            params={"peek": "true"} if self.peek_on_read else None,
            idempotent=True,
        )
        return self.validator.message(payload)

    async def labels(self) -> UpstreamLabels:
        payload = await self._request("GET", "/v1/labels", idempotent=True)
        return self.validator.labels(payload)

    async def send(self, *, to: list[str], subject: str, body: str) -> UpstreamSendResult:
        payload = await self._request(
            "POST",
            "/v1/messages/send",
            json={"to": to, "subject": subject, "body": body},
            idempotent=False,  # a delivered message cannot be un-delivered
        )
        return UpstreamSendResult.model_validate(payload)

    async def trash(self, message_id: str) -> UpstreamLabelChange:
        payload = await self._request("POST", f"/v1/messages/{message_id}/trash", idempotent=True)
        return UpstreamLabelChange.model_validate(payload)

    async def add_label(self, message_id: str, label: str) -> UpstreamLabelChange:
        payload = await self._request(
            "POST", f"/v1/messages/{message_id}/labels", json={"label": label}, idempotent=True
        )
        return UpstreamLabelChange.model_validate(payload)

    # ---------------------------------------------------------------- plumbing
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        idempotent: bool,
    ) -> Any:
        url = f"{self.base_url.rstrip('/')}{path}"
        attempt = 1
        refreshed = False
        while True:
            self.attempts.append(f"{method} {path}")
            error: UpstreamError
            try:
                response = await self.http.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {self.tokens.access_token}"},
                    timeout=self.timeout_s,
                )
            except httpx.TimeoutException:
                error = UpstreamError(kind="timeout", message="upstream timed out")
            except httpx.TransportError as exc:
                error = UpstreamError(kind="unavailable", message=f"transport error: {exc!s}")
            else:
                if response.status_code < 400:
                    return self._decode(response)
                error = self._error_for(response)
                if (
                    error.kind == "unauthorized"
                    and error.code == "token_expired"
                    and not refreshed
                    and self.tokens.can_refresh
                ):
                    # A 401 means the call never ran, so replaying it is safe
                    # even for a send.
                    refreshed = True
                    if await self.tokens.refresh():
                        continue
                    raise error

            delay = self.retry.delay_for(attempt=attempt, error=error, idempotent=idempotent)
            if delay is None:
                raise error
            await anyio.sleep(delay)
            attempt += 1

    def _decode(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(
                kind="malformed",
                message="Acme Mail returned a body that is not JSON",
            ) from exc

    def _error_for(self, response: httpx.Response) -> UpstreamError:
        status = response.status_code
        try:
            body = response.json()
            code = str(body.get("error", ""))
            detail = str(body.get("message", ""))
        except ValueError:
            code, detail = "", ""

        if status == 429:
            header = response.headers.get("Retry-After")
            retry_after = float(header) if header and header.isdigit() else None
            return UpstreamError(
                kind="rate_limited",
                message="rate limited",
                status=status,
                retry_after=retry_after,
                code=code or "rate_limited",
            )
        if status == 401:
            return UpstreamError(
                kind="unauthorized", message=detail or "unauthorized", status=status, code=code
            )
        if status == 403:
            return UpstreamError(kind="forbidden", message=detail or "forbidden", status=status, code=code)
        if status == 404:
            return UpstreamError(kind="not_found", message=detail or "not found", status=status, code=code)
        if status < 500:
            return UpstreamError(
                kind="invalid_request",
                message=detail or f"request rejected ({status})",
                status=status,
                code=code,
            )
        return UpstreamError(
            kind="unavailable",
            message=detail or f"upstream error ({status})",
            status=status,
            code=code,
        )
