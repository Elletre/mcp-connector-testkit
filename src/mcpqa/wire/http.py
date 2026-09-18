"""A streamable-HTTP client that keeps the status line and the headers.

The HTTP binding puts real requirements at the transport level — 403 for a bad
Origin, 202 for a notification, 404 with `-32601` for an unknown method, 400
for a header that disagrees with the body — so the checks need the response,
not just the JSON-RPC payload inside it.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx

from .transcript import Exchange, Transcript

_HEADER_SAFE = re.compile(r"^[\x20-\x7E]*$")


def encode_header_value(value: str) -> str:
    """Spec's base64 sentinel for values that cannot ride in an HTTP header."""
    if _HEADER_SAFE.fullmatch(value) and value == value.strip():
        return value
    return f"=?base64?{base64.b64encode(value.encode('utf-8')).decode('ascii')}?="


def parse_sse(text: str) -> list[dict[str, Any]]:
    """Pull JSON payloads out of an `text/event-stream` body."""
    messages: list[dict[str, Any]] = []
    for block in re.split(r"\r?\n\r?\n", text):
        data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
        if not data:
            continue
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            messages.append(parsed)
    return messages


@dataclass
class HttpWire:
    url: str
    bearer: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 15.0

    transcript: Transcript = field(default_factory=Transcript)
    _client: httpx.Client | None = field(default=None, init=False, repr=False)

    def start(self) -> HttpWire:
        self._client = httpx.Client(timeout=self.timeout_s, follow_redirects=False)
        return self

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self.start()
        assert self._client is not None
        return self._client

    def base_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.bearer}"
        return headers

    def post(
        self,
        body: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        expect_response: bool = True,
    ) -> Exchange:
        merged = {**self.base_headers(), **(headers or {})}
        raw_request = json.dumps(body, ensure_ascii=False)
        started = time.monotonic()
        response = self.client.post(self.url, content=raw_request.encode("utf-8"), headers=merged)
        elapsed = time.monotonic() - started
        payload, note = self._decode(response, expect_response=expect_response, request_id=body.get("id"))
        return self.transcript.add(
            Exchange(
                request=body,
                response=payload,
                raw_request=raw_request,
                raw_response=response.text,
                status=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                elapsed_s=elapsed,
                note=note,
            )
        )

    def post_raw(self, content: str, *, headers: dict[str, str] | None = None) -> Exchange:
        """Send a body verbatim, for malformed-JSON checks."""
        merged = {**self.base_headers(), **(headers or {})}
        started = time.monotonic()
        response = self.client.post(self.url, content=content.encode("utf-8"), headers=merged)
        payload, note = self._decode(response, expect_response=False)
        return self.transcript.add(
            Exchange(
                request=None,
                response=payload,
                raw_request=content,
                raw_response=response.text,
                status=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                elapsed_s=time.monotonic() - started,
                note=note,
            )
        )

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> Exchange:
        started = time.monotonic()
        response = self.client.get(url, headers={**self.base_headers(), **(headers or {})})
        payload, note = self._decode(response, expect_response=False)
        return self.transcript.add(
            Exchange(
                request=None,
                response=payload,
                raw_request=f"GET {url}",
                raw_response=response.text,
                status=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                elapsed_s=time.monotonic() - started,
                note=note,
            )
        )

    @staticmethod
    def _decode(
        response: httpx.Response, *, expect_response: bool, request_id: Any = None
    ) -> tuple[dict[str, Any] | None, str | None]:
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/event-stream"):
            messages = parse_sse(response.text)
            if not messages:
                return None, "SSE stream carried no JSON payload"
            answers = [m for m in messages if "method" not in m and m.get("id") == request_id]
            chosen = answers[-1] if answers else messages[-1]
            return chosen, None if len(messages) == 1 else f"{len(messages)} SSE events"
        if not response.text.strip():
            return None, None if not expect_response else "empty body"
        try:
            parsed = json.loads(response.text)
        except json.JSONDecodeError:
            return None, f"body is not JSON (content-type: {content_type or 'unset'})"
        return (parsed, None) if isinstance(parsed, dict) else (None, "body is not a JSON object")

    def __enter__(self) -> HttpWire:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()
