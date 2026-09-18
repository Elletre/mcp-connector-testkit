"""One session object that can speak either era of the protocol.

The two eras differ in three places and nowhere else: how a session opens
(`initialize` handshake versus nothing at all), what rides in `params._meta`,
and which HTTP headers a request must carry. Everything a check wants to do —
list tools, call one, send something deliberately malformed — is the same on
both sides, so the checks are written once and run twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .protocol import (
    CLIENT_INFO,
    HEADER_METHOD,
    HEADER_NAME,
    HEADER_PROTOCOL_VERSION,
    HEADER_SESSION_ID,
    LATEST_HANDSHAKE,
    LATEST_STATELESS,
    META_CLIENT_CAPABILITIES,
    META_CLIENT_INFO,
    META_PROTOCOL_VERSION,
    NAME_BEARING_METHODS,
    Era,
    era_for,
)
from .target import StdioTarget, Target
from .wire.http import HttpWire, encode_header_value
from .wire.stdio import StdioWire
from .wire.transcript import Exchange, Transcript


@dataclass
class Session:
    target: Target
    protocol_version: str = LATEST_STATELESS
    read_timeout_s: float = 15.0
    wire: StdioWire | HttpWire = field(init=False)
    era: Era = field(init=False)
    session_id: str | None = field(default=None, init=False)
    initialize_result: dict[str, Any] | None = field(default=None, init=False)
    _next_id: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.era = era_for(self.protocol_version)
        if isinstance(self.target, StdioTarget):
            self.wire = StdioWire(
                command=self.target.command,
                env=dict(self.target.env),
                cwd=self.target.cwd,
                read_timeout_s=self.read_timeout_s,
            )
        else:
            self.wire = HttpWire(
                url=self.target.url,
                bearer=self.target.bearer,
                extra_headers=dict(self.target.headers),
                timeout_s=self.read_timeout_s,
            )

    # ------------------------------------------------------------- lifecycle
    @property
    def transcript(self) -> Transcript:
        return self.wire.transcript

    @property
    def is_stdio(self) -> bool:
        return isinstance(self.wire, StdioWire)

    @property
    def alive(self) -> bool:
        """False once a stdio server has exited; an HTTP endpoint is assumed up."""
        return not isinstance(self.wire, StdioWire) or self.wire.returncode is None

    def open(self, *, handshake: bool = True) -> Session:
        self.wire.start()
        if self.era == "handshake" and handshake:
            self.initialize()
        return self

    def initialize(self, *, version: str | None = None) -> Exchange:
        exchange = self.call(
            "initialize",
            {
                "protocolVersion": version or self.protocol_version,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
            envelope=False,
        )
        if exchange.status is not None:
            self.session_id = exchange.headers.get(HEADER_SESSION_ID.lower())
        if exchange.result is not None:
            self.initialize_result = exchange.result
            self.notify("notifications/initialized")
        return exchange

    def discover(self) -> Exchange:
        return self.call("server/discover")

    def close(self) -> None:
        self.wire.close()

    def __enter__(self) -> Session:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- calls
    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: Any | None = None,
        envelope: bool = True,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Exchange:
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self.next_id() if request_id is None else request_id,
            "method": method,
        }
        payload = self.params(params, envelope=envelope)
        if payload is not None:
            body["params"] = payload
        if isinstance(self.wire, HttpWire):
            return self.wire.post(body, headers={**self.http_headers(body), **(headers or {})})
        return self.wire.request(body, timeout=timeout)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> Exchange:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        payload = self.params(params, envelope=self.era == "stateless")
        if payload is not None:
            body["params"] = payload
        if isinstance(self.wire, HttpWire):
            return self.wire.post(body, headers=self.http_headers(body), expect_response=False)
        return self.wire.notify(body)

    # ------------------------------------------------------------ convenience
    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(20):
            params = {"cursor": cursor} if cursor else None
            exchange = self.call("tools/list", params)
            result = exchange.result
            if result is None:
                break
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Exchange:
        return self.call("tools/call", {"name": name, "arguments": arguments or {}})

    def structured(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.call_tool(name, arguments).result or {}
        content = result.get("structuredContent")
        return content if isinstance(content, dict) else {}

    # --------------------------------------------------------------- framing
    def params(self, params: dict[str, Any] | None, *, envelope: bool = True) -> dict[str, Any] | None:
        """Add the per-request envelope when the era calls for one."""
        if self.era != "stateless" or not envelope:
            return params
        merged = dict(params or {})
        merged["_meta"] = {
            META_PROTOCOL_VERSION: self.protocol_version,
            META_CLIENT_CAPABILITIES: {},
            META_CLIENT_INFO: CLIENT_INFO,
            **(merged.get("_meta") or {}),
        }
        return merged

    def http_headers(self, body: dict[str, Any]) -> dict[str, str]:
        headers: dict[str, str] = {}
        method = str(body.get("method", ""))
        if self.era == "stateless":
            headers[HEADER_PROTOCOL_VERSION] = self.protocol_version
            headers[HEADER_METHOD] = method
            name_key = NAME_BEARING_METHODS.get(method)
            if name_key:
                value = (body.get("params") or {}).get(name_key)
                if value is not None:
                    headers[HEADER_NAME] = encode_header_value(str(value))
        else:
            if method != "initialize":
                headers[HEADER_PROTOCOL_VERSION] = self.protocol_version
            if self.session_id:
                headers[HEADER_SESSION_ID] = self.session_id
        return headers


def open_session(
    target: Target, *, era: Era = "stateless", version: str | None = None, handshake: bool = True
) -> Session:
    chosen = version or (LATEST_STATELESS if era == "stateless" else LATEST_HANDSHAKE)
    return Session(target=target, protocol_version=chosen).open(handshake=handshake)


def probe_era(target: Target, *, timeout: float = 5.0) -> Era:
    """Follow the spec's own backward-compatibility probe.

    `server/discover` first: a `DiscoverResult` means a stateless server, any
    other error means a handshake-era one. The fallback is deliberately not
    keyed to a particular error code, because legacy servers answer an unknown
    pre-`initialize` method however they like.
    """
    session = Session(target=target, protocol_version=LATEST_STATELESS)
    session.wire.start()
    try:
        exchange = session.call("server/discover", timeout=timeout)
        result = exchange.result
        if result and any(isinstance(v, str) for v in (result.get("supportedVersions") or [])):
            return "stateless"
        return "handshake"
    except Exception:
        return "handshake"
    finally:
        session.close()
