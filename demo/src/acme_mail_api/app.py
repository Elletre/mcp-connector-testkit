"""The Acme Mail provider: a small, deliberately realistic upstream API.

It exists so the connector in front of it can be tested against the things
real providers do — cursor pagination, scoped tokens, expiry and refresh, rate
limits, outages, schema drift — without anyone's real mailbox or credentials.

The `/_control/*` plane is what makes it a test double rather than a toy:
tests inject faults, expire tokens, read the audit log of every upstream call,
and take snapshots of mailbox state. It is off unless the server is started
with `control_enabled=True`.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import anyio
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from .auth import SCOPE_MODIFY, SCOPE_READ, SCOPE_SEND, AuthStore
from .faults import FaultEngine, FaultRule, RateLimiter
from .store import ApiError, Store


@dataclass
class AuditEntry:
    seq: int
    at: float
    started_at: float
    method: str
    path: str
    query: str
    token: str | None
    subject: str | None
    status: int
    duration_ms: float
    fault: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "started_at": self.started_at,
            "method": self.method,
            "path": self.path,
            "query": self.query,
            "token": self.token,
            "subject": self.subject,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 2),
            "fault": self.fault,
        }


@dataclass
class ProviderState:
    store: Store = field(default_factory=Store)
    auth: AuthStore = field(default_factory=AuthStore)
    faults: FaultEngine = field(default_factory=FaultEngine)
    limiter: RateLimiter = field(default_factory=RateLimiter)
    audit: list[AuditEntry] = field(default_factory=list)

    def reset(self) -> None:
        self.store.reset()
        self.auth.reset()
        self.faults.reset()
        self.limiter.reset()
        self.audit.clear()

    def record(self, entry: AuditEntry) -> None:
        self.audit.append(entry)

    def subject_for(self, authorization: str | None) -> tuple[str | None, str | None]:
        if not authorization or not authorization.lower().startswith("bearer "):
            return None, None
        raw = authorization[7:].strip()
        token = self.auth.access.get(raw)
        return raw, token.subject if token else None


def _error_response(error: ApiError) -> JSONResponse:
    payload: dict[str, Any] = {"error": error.code, "message": error.message}
    payload.update({k: v for k, v in error.extra.items() if v is not None})
    return JSONResponse(payload, status_code=error.status)


class ProviderMiddleware(BaseHTTPMiddleware):
    """Audit every call, and let the fault engine and rate limiter intercept it."""

    def __init__(self, app: Any, state: ProviderState) -> None:
        super().__init__(app)
        self.state = state

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        arrived_at = time.time()
        path = request.url.path
        token, subject = self.state.subject_for(request.headers.get("authorization"))
        fault_id: str | None = None
        response: Response

        rule = None if path.startswith("/_control") else self.state.faults.next_for(request.method, path)
        if rule is not None:
            fault_id = rule.id
            if rule.delay_ms and rule.kind != "delay_response":
                await anyio.sleep(rule.delay_ms / 1000)
            if rule.kind == "hang":
                await anyio.sleep(300)
            if rule.kind == "status":
                body = rule.body or {"error": "upstream_error", "message": "Injected fault"}
                response = JSONResponse(body, status_code=rule.status, headers=rule.headers)
                self._audit(request, token, subject, response.status_code, started, fault_id, arrived_at)
                return response
            if rule.kind == "garbage":
                response = PlainTextResponse(
                    "<html><body>502 Bad Gateway</body></html>",
                    status_code=rule.status,
                    media_type="text/html",
                )
                self._audit(request, token, subject, response.status_code, started, fault_id, arrived_at)
                return response

        if path.startswith("/v1/"):
            wait = self.state.limiter.take(token or "anonymous")
            if wait is not None:
                response = JSONResponse(
                    {"error": "rate_limited", "message": "Too many requests"},
                    status_code=429,
                    headers={"Retry-After": str(max(1, math.ceil(wait)))},
                )
                self._audit(request, token, subject, 429, started, fault_id, arrived_at)
                return response

        try:
            response = await call_next(request)
        except ApiError as error:  # pragma: no cover - handlers convert their own
            response = _error_response(error)
        except ClientDisconnect:
            # The caller gave up while we were working. The work still happened,
            # which is exactly the situation a connector must not retry into.
            self._audit(request, token, subject, 499, started, fault_id, arrived_at)
            return Response(status_code=499)

        if rule is not None and rule.kind == "delay_response" and rule.delay_ms:
            await anyio.sleep(rule.delay_ms / 1000)

        self._audit(request, token, subject, response.status_code, started, fault_id, arrived_at)
        return response

    def _audit(
        self,
        request: Request,
        token: str | None,
        subject: str | None,
        status: int,
        started: float,
        fault: str | None,
        arrived_at: float,
    ) -> None:
        self.state.record(
            AuditEntry(
                seq=len(self.state.audit) + 1,
                at=time.time(),
                started_at=arrived_at,
                method=request.method,
                path=request.url.path,
                query=request.url.query,
                token=token,
                subject=subject,
                status=status,
                duration_ms=(time.perf_counter() - started) * 1000,
                fault=fault,
            )
        )


# --------------------------------------------------------------------- routes


def _state(request: Request) -> ProviderState:
    return request.app.state.provider  # type: ignore[no-any-return]


def _authorize(request: Request, scope: str) -> str:
    state = _state(request)
    token = state.auth.verify(request.headers.get("authorization"), required_scope=scope)
    return token.subject


async def list_messages(request: Request) -> Response:
    state = _state(request)
    try:
        account = _authorize(request, SCOPE_READ)
        params = request.query_params
        page_size = int(params["page_size"]) if params.get("page_size") else None
        page = state.store.search(
            account,
            query=params.get("q"),
            label=params.get("label"),
            page_size=page_size,
            page_token=params.get("page_token"),
        )
    except ApiError as error:
        return _error_response(error)
    except ValueError:
        return _error_response(ApiError(400, "invalid_argument", "page_size must be an integer"))
    return JSONResponse(
        {
            "messages": [state.faults.apply_drift(m.summary()) for m in page.messages],
            "next_page_token": page.next_page_token,
        }
    )


async def get_message(request: Request) -> Response:
    state = _state(request)
    try:
        account = _authorize(request, SCOPE_READ)
        peek = request.query_params.get("peek", "false").lower() in ("1", "true", "yes")
        message = state.store.get(account, request.path_params["message_id"], peek=peek)
    except ApiError as error:
        return _error_response(error)
    return JSONResponse(state.faults.apply_drift(message.full()))


async def send_message(request: Request) -> Response:
    state = _state(request)
    try:
        account = _authorize(request, SCOPE_SEND)
        payload = await request.json()
        message = state.store.send(
            account,
            to=list(payload.get("to") or []),
            subject=str(payload.get("subject") or ""),
            body=str(payload.get("body") or ""),
        )
    except ApiError as error:
        return _error_response(error)
    return JSONResponse({"id": message.id, "thread_id": message.thread_id}, status_code=201)


async def trash_message(request: Request) -> Response:
    state = _state(request)
    try:
        account = _authorize(request, SCOPE_MODIFY)
        message = state.store.trash(account, request.path_params["message_id"])
    except ApiError as error:
        return _error_response(error)
    return JSONResponse({"id": message.id, "labels": message.labels})


async def add_label(request: Request) -> Response:
    state = _state(request)
    try:
        account = _authorize(request, SCOPE_MODIFY)
        payload = await request.json()
        message = state.store.add_label(
            account, request.path_params["message_id"], str(payload.get("label") or "")
        )
    except ApiError as error:
        return _error_response(error)
    return JSONResponse({"id": message.id, "labels": message.labels})


async def list_labels(request: Request) -> Response:
    state = _state(request)
    try:
        account = _authorize(request, SCOPE_READ)
    except ApiError as error:
        return _error_response(error)
    return JSONResponse({"labels": state.store.labels(account)})


async def oauth_token(request: Request) -> Response:
    state = _state(request)
    form = await request.form()
    grant_type = str(form.get("grant_type") or "")
    if grant_type != "refresh_token":
        return _error_response(ApiError(400, "unsupported_grant_type", f"Unsupported: {grant_type}"))
    try:
        minted = state.auth.refresh_grant(str(form.get("refresh_token") or ""))
    except ApiError as error:
        return _error_response(error)
    return JSONResponse(
        {
            "access_token": minted.token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": " ".join(minted.scopes),
        }
    )


# -------------------------------------------------------------- control plane


async def control_reset(request: Request) -> Response:
    _state(request).reset()
    return JSONResponse({"ok": True})


async def control_faults(request: Request) -> Response:
    state = _state(request)
    payload = await request.json()
    rules = [FaultRule(**rule) for rule in payload.get("rules", [])]
    state.faults.set_rules(rules)
    return JSONResponse({"ok": True, "rules": len(rules)})


async def control_drift(request: Request) -> Response:
    state = _state(request)
    payload = await request.json()
    try:
        state.faults.set_drift(payload.get("profile"))
    except ValueError as error:
        return _error_response(ApiError(400, "invalid_argument", str(error)))
    return JSONResponse({"ok": True, "profile": state.faults.drift})


async def control_rate_limit(request: Request) -> Response:
    state = _state(request)
    payload = await request.json()
    state.limiter.configure(
        capacity=float(payload.get("capacity", 1000)),
        refill_per_sec=float(payload.get("refill_per_sec", 1000)),
    )
    return JSONResponse({"ok": True})


async def control_token(request: Request) -> Response:
    state = _state(request)
    token = request.path_params["token"]
    action = request.path_params["action"]
    try:
        if action == "expire":
            state.auth.expire(token)
        elif action == "revoke":
            state.auth.revoke(token)
        elif action == "revoke-refresh":
            state.auth.revoke_refresh(token)
        else:
            return _error_response(ApiError(400, "invalid_argument", f"Unknown action {action}"))
    except ApiError as error:
        return _error_response(error)
    return JSONResponse({"ok": True})


async def control_audit(request: Request) -> Response:
    state = _state(request)
    since = int(request.query_params.get("since", 0))
    return JSONResponse({"entries": [e.as_dict() for e in state.audit if e.seq > since]})


async def control_state(request: Request) -> Response:
    state = _state(request)
    account = request.query_params.get("account")
    return JSONResponse({"facts": sorted(state.store.facts(account))})


CONTROL_ROUTES = [
    Route("/_control/reset", control_reset, methods=["POST"]),
    Route("/_control/faults", control_faults, methods=["POST"]),
    Route("/_control/drift", control_drift, methods=["POST"]),
    Route("/_control/rate_limit", control_rate_limit, methods=["POST"]),
    Route("/_control/tokens/{token}/{action}", control_token, methods=["POST"]),
    Route("/_control/audit", control_audit, methods=["GET"]),
    Route("/_control/state", control_state, methods=["GET"]),
]

API_ROUTES = [
    Route("/v1/messages", list_messages, methods=["GET"]),
    Route("/v1/messages/send", send_message, methods=["POST"]),
    Route("/v1/messages/{message_id}", get_message, methods=["GET"]),
    Route("/v1/messages/{message_id}/trash", trash_message, methods=["POST"]),
    Route("/v1/messages/{message_id}/labels", add_label, methods=["POST"]),
    Route("/v1/labels", list_labels, methods=["GET"]),
    Route("/oauth/token", oauth_token, methods=["POST"]),
]


def create_app(*, control_enabled: bool = False, state: ProviderState | None = None) -> Starlette:
    provider = state or ProviderState()
    routes = [*API_ROUTES, *(CONTROL_ROUTES if control_enabled else [])]
    app = Starlette(
        routes=routes,
        middleware=[Middleware(ProviderMiddleware, state=provider)],
    )
    app.state.provider = provider
    return app
