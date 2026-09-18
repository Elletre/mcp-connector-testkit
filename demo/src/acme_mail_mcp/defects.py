"""A catalogue of realistic ways to build this connector wrong.

Each entry models a failure I would expect to meet in a real connector review,
and each one is introduced by swapping a single part of the composition — not
by sprinkling `if broken:` through the connector. Set `ACME_MCP_DEFECTS=<id>`
(comma-separated for several) and the server starts with that behaviour.

`expected_layer` records which layer of the test kit is supposed to notice.
The defect matrix (`tools/defect_matrix.py`) checks that claim by running the
whole suite against every defect, one at a time.
"""

from __future__ import annotations

import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

import httpx2 as httpx
from mcp.server.mcpserver.tools.base import Tool
from mcp.shared.exceptions import MCPError
from mcp.types import ToolAnnotations
from mcp_types import INVALID_PARAMS

from .components import (
    CacheKeyPolicy,
    DateNormalizer,
    ResponseValidator,
    RetryPolicy,
    SecretScrubber,
)
from .config import Settings
from .errors import UpstreamError
from .schemas import UpstreamLabels, UpstreamMessage, UpstreamPage
from .server import BuildOptions, Connector
from .service import Components
from .upstream import AcmeMailClient

# ----------------------------------------------------------- broken variants


@dataclass(frozen=True)
class FirstPageOnlyPaginator:
    """Asks for one page and calls it a day."""

    async def collect(
        self,
        fetch: Callable[[str | None, int], Awaitable[UpstreamPage]],
        *,
        max_results: int,
        page_token: str | None,
        page_size_cap: int,
    ) -> tuple[list[Any], str | None]:
        page = await fetch(page_token, min(page_size_cap, max_results))
        return list(page.messages), page.next_page_token


@dataclass(frozen=True)
class ImmediateRetryPolicy(RetryPolicy):
    """Retries a rate limit at once, ignoring the wait the provider asked for."""

    def delay_for(
        self,
        *,
        attempt: int,
        error: UpstreamError,
        idempotent: bool,  # noqa: ARG002 - the point of this variant is ignoring it
    ) -> float | None:
        if attempt >= self.max_attempts:
            return None
        if error.kind in ("rate_limited", "unavailable", "timeout"):
            return 0.0
        return None


@dataclass(frozen=True)
class RetryAnythingPolicy(RetryPolicy):
    """Retries every failure, including the ones that may already have happened."""

    def delay_for(
        self,
        *,
        attempt: int,
        error: UpstreamError,
        idempotent: bool,  # noqa: ARG002 - retrying regardless is the defect
    ) -> float | None:
        if attempt >= self.max_attempts:
            return None
        if error.kind in ("rate_limited", "unavailable", "timeout"):
            return 0.05
        return None


@dataclass(frozen=True)
class QueryOnlyCacheKey(CacheKeyPolicy):
    """Caches search results by query alone, with no idea who asked."""

    def key(
        self,
        subject: str,  # noqa: ARG002 - forgetting the subject is the defect
        params: dict[str, Any],
    ) -> str:
        return "|".join(f"{k}={params[k]!r}" for k in sorted(params))


@dataclass(frozen=True)
class NaiveDateNormalizer(DateNormalizer):
    """Strips the UTC offset while "normalising" the timestamp."""

    def normalize(self, value: str) -> str:
        return value[:19]


@dataclass(frozen=True)
class LenientValidator(ResponseValidator):
    """Reads upstream responses with `.get()` and fills the gaps with blanks."""

    @staticmethod
    def _patch(payload: dict[str, Any]) -> dict[str, Any]:
        filled = dict(payload)
        filled.setdefault("subject", filled.pop("title", ""))
        filled.setdefault("snippet", "")
        filled.setdefault("received_at", "")
        return filled

    def page(self, payload: Any) -> UpstreamPage:
        messages = [self._patch(m) for m in payload.get("messages", [])]
        return UpstreamPage.model_validate({**payload, "messages": messages})

    def message(self, payload: Any) -> UpstreamMessage:
        return UpstreamMessage.model_validate(self._patch(payload))

    def labels(self, payload: Any) -> UpstreamLabels:
        return UpstreamLabels.model_validate(payload)


@dataclass(frozen=True)
class NullScrubber(SecretScrubber):
    """The redaction step someone removed because it "slowed down logging"."""

    def scrub(self, text: str) -> str:
        return text


class LeakyClient(AcmeMailClient):
    """Puts the full request context, credential included, into the error text."""

    def _error_for(self, response: httpx.Response) -> UpstreamError:
        error = super()._error_for(response)
        return replace(
            error,
            message=(
                f"{error.message} (request: {response.request.method} {response.request.url} "
                f"with Authorization: Bearer {self.tokens.access_token})"
            ),
        )


# ------------------------------------------------------------------- helpers


def _tool(connector: Connector, name: str) -> Tool:
    tool = connector.server._tool_manager.get_tool(name)
    if tool is None:  # pragma: no cover - the catalogue names tools that exist
        raise KeyError(name)
    return tool


def _wrap(connector: Connector, name: str, before: Callable[[], None]) -> None:
    tool = _tool(connector, name)
    original = tool.fn

    async def wrapper(**kwargs: Any) -> Any:
        before()
        return await original(**kwargs)

    tool.fn = wrapper


# --------------------------------------------------------------- the defects


@dataclass(frozen=True)
class Defect:
    id: str
    title: str
    breaks: str
    real_world: str
    expected_layer: int
    pre: Callable[[BuildOptions], None] | None = None
    post: Callable[[Connector], None] | None = None


def _blocking_call(connector: Connector) -> None:
    _wrap(connector, "search_messages", lambda: time.sleep(1.5))


def _invalid_input_schema(connector: Connector) -> None:
    schema = _tool(connector, "search_messages").parameters
    schema["properties"]["max_results"]["type"] = "int"  # not a JSON Schema type


def _shuffle_tools(connector: Connector) -> None:
    tools = connector.server._tool_manager._tools
    names = list(tools)
    random.Random(time.time_ns()).shuffle(names)
    reordered = {name: tools[name] for name in names}
    tools.clear()
    tools.update(reordered)


def _annotate(connector: Connector, name: str, **changes: Any) -> None:
    tool = _tool(connector, name)
    current = tool.annotations.model_dump(by_alias=True) if tool.annotations else {}
    tool.annotations = ToolAnnotations.model_validate({**current, **changes})


def _require_optional_param(connector: Connector) -> None:
    """The implementation rejects what the published schema says is optional."""
    tool = _tool(connector, "search_messages")
    original = tool.fn

    async def wrapper(**kwargs: Any) -> Any:
        if kwargs.get("label") is None:
            raise MCPError(INVALID_PARAMS, "label is required")
        return await original(**kwargs)

    tool.fn = wrapper


def _output_schema_drift(connector: Connector) -> None:
    """Publishes a schema the connector's own output does not satisfy."""
    schema = _tool(connector, "get_message").fn_metadata.output_schema
    assert schema is not None
    schema["properties"]["received_at"] = {"type": "integer", "title": "Received At"}


def _vague_descriptions(connector: Connector) -> None:
    for name in ("search_messages", "get_message", "list_labels", "add_label", "trash_message"):
        _tool(connector, name).description = "Mail operation."
    _tool(connector, "send_message").description = "Mail operation. Sends."


def _instructions_without_the_warning(options: BuildOptions) -> None:
    options.instructions = (
        "Tools for one connected Acme Mail account. Search returns summaries; fetch a message to "
        "read its body. Sending mail and moving mail to the trash change the user's mailbox: ask "
        "before doing either."
    )


def _unmarked_untrusted_content(connector: Connector) -> None:
    connector.service.components.mark_untrusted_content = False
    tool = _tool(connector, "get_message")
    tool.description = "Fetch one message in full, with its body and attachment metadata."


def _leaky_errors(options: BuildOptions) -> None:
    # Two changes, because that is how this reaches production: someone adds
    # request context to an error, and the redaction that would have caught it
    # was removed earlier for being noisy.
    options.components.client_class = LeakyClient
    options.components.make_scrubber = lambda _secrets: NullScrubber()


REGISTRY: dict[str, Defect] = {
    defect.id: defect
    for defect in [
        Defect(
            id="schema-invalid-type",
            title="inputSchema is not valid JSON Schema",
            breaks='max_results is published as {"type": "int"}, which no validator accepts.',
            real_world="A schema written by hand, or ported from another language's type names.",
            expected_layer=1,
            post=_invalid_input_schema,
        ),
        Defect(
            id="bind-all-interfaces",
            title="Bound to 0.0.0.0, so rebinding protection switches itself off",
            breaks="The SDK enables Origin validation only for localhost binds; on 0.0.0.0 it is off.",
            real_world="Containerising a server: the bind address changes, and the defaults change with it.",
            expected_layer=1,
            pre=lambda options: setattr(options, "bind_host", "0.0.0.0"),
        ),
        Defect(
            id="nondeterministic-tool-order",
            title="tools/list comes back in a different order each run",
            breaks="Tools are published in a per-process random order, so no client can cache the list.",
            real_world="Registering tools from a set, or from unordered plugin discovery.",
            expected_layer=1,
            post=_shuffle_tools,
        ),
        Defect(
            id="blocking-event-loop",
            title="A synchronous call blocks the event loop",
            breaks="One slow search freezes every other request on the connection.",
            real_world="A sync HTTP client left inside an async handler.",
            expected_layer=1,
            post=_blocking_call,
        ),
        Defect(
            id="readonly-get-marks-read",
            title="A read-only tool marks mail as read",
            breaks="get_message fetches without peek, so reading mutates the mailbox.",
            real_world="IMAP's BODY[] instead of BODY.PEEK[] — the classic silent mutation.",
            expected_layer=2,
            pre=lambda options: setattr(options.components.client_options, "peek_on_read", False),
        ),
        Defect(
            id="trash-not-destructive",
            title="A destructive tool advertises itself as safe",
            breaks="trash_message declares destructiveHint=false, so clients skip the confirmation.",
            real_world="Annotations copied from a neighbouring tool and never revisited.",
            expected_layer=2,
            post=lambda connector: _annotate(connector, "trash_message", destructiveHint=False),
        ),
        Defect(
            id="send-marked-idempotent",
            title="Sending mail claims to be idempotent",
            breaks="send_message declares idempotentHint=true, inviting clients to retry it.",
            real_world="Idempotency treated as a property of the code path rather than of the effect.",
            expected_layer=2,
            post=lambda connector: _annotate(connector, "send_message", idempotentHint=True),
        ),
        Defect(
            id="optional-param-required",
            title="The implementation is stricter than the published schema",
            breaks="label is optional in inputSchema but rejected as missing at call time.",
            real_world="Hand-written validation that drifted from the generated schema.",
            expected_layer=2,
            post=_require_optional_param,
        ),
        Defect(
            id="output-schema-drift",
            title="structuredContent does not match outputSchema",
            breaks="received_at is published as an integer while the connector sends RFC 3339 text.",
            real_world="A schema edited by hand after the serializer changed.",
            expected_layer=2,
            post=_output_schema_drift,
        ),
        Defect(
            id="cache-key-missing-tenant",
            title="Search cache keyed without the user",
            breaks="One tenant's search results are served to another.",
            real_world="A cache added for latency, keyed by the query that was easy to reach.",
            expected_layer=3,
            pre=lambda options: setattr(options.components, "cache_key", QueryOnlyCacheKey()),
        ),
        Defect(
            id="token-in-error-message",
            title="The access token travels inside an error",
            breaks="Upstream failures carry the Authorization header into tool results and logs.",
            real_world="Debug context added during an incident and never removed.",
            expected_layer=3,
            pre=_leaky_errors,
        ),
        Defect(
            id="no-token-refresh",
            title="An expired token is not renewed",
            breaks="Every tool call fails once the upstream access token expires mid-session.",
            real_world="Refresh implemented at login only, never on the request path.",
            expected_layer=3,
            pre=lambda options: setattr(options.components.client_options, "allow_refresh", False),
        ),
        Defect(
            id="accepts-foreign-audience",
            title="Token passthrough",
            breaks="An upstream credential is accepted as proof of identity by the MCP endpoint.",
            real_world="The antipattern the MCP authorization spec names explicitly.",
            expected_layer=3,
            pre=lambda options: setattr(options, "accept_upstream_tokens", True),
        ),
        Defect(
            id="pagination-first-page-only",
            title="Only the first page is ever read",
            breaks="Results are silently truncated at the provider's page cap.",
            real_world="Pagination tested on a small mailbox, shipped against a large one.",
            expected_layer=4,
            pre=lambda options: setattr(options.components, "paginator", FirstPageOnlyPaginator()),
        ),
        Defect(
            id="ignores-retry-after",
            title="Rate limits are retried instantly",
            breaks="The connector hammers the provider instead of waiting the requested interval.",
            real_world="A retry loop written before anyone read the rate-limit documentation.",
            expected_layer=4,
            pre=lambda options: setattr(options.components, "retry", ImmediateRetryPolicy()),
        ),
        Defect(
            id="retries-send-on-timeout",
            title="A send is retried after a timeout",
            breaks="An ambiguous failure becomes a duplicate email.",
            real_world="One retry policy applied to every verb.",
            expected_layer=4,
            pre=lambda options: setattr(options.components, "retry", RetryAnythingPolicy()),
        ),
        Defect(
            id="timezone-offset-dropped",
            title="Timestamps lose their UTC offset",
            breaks="received_at is truncated to a naive string, moving late-evening mail to another day.",
            real_world="Dates 'normalised' on the way through a connector.",
            expected_layer=4,
            pre=lambda options: setattr(options.components, "dates", NaiveDateNormalizer()),
        ),
        Defect(
            id="drift-returns-nulls",
            title="Schema drift becomes empty fields",
            breaks="A renamed upstream field turns into an empty subject rather than an error.",
            real_world="Defensive .get() calls that hide a provider's v2 rollout.",
            expected_layer=4,
            pre=lambda options: setattr(options.components, "validator", LenientValidator()),
        ),
        Defect(
            id="unbounded-body",
            title="No limit on how much a tool can return",
            breaks="A multi-megabyte message is handed to the model in full.",
            real_world="Budgets that exist in the design document and nowhere else.",
            expected_layer=4,
            pre=lambda options: setattr(
                options, "settings", replace(options.settings, body_char_budget=10**9)
            ),
        ),
        Defect(
            id="vague-tool-descriptions",
            title="Tool descriptions say nothing",
            breaks="Every tool is described as 'Mail operation.', so the model has to guess.",
            real_world="Descriptions trimmed to save tokens.",
            expected_layer=5,
            post=_vague_descriptions,
        ),
        Defect(
            id="untrusted-content-unmarked",
            title="Message bodies arrive unlabelled",
            breaks="Nothing tells the model that a message body is text written by a stranger.",
            real_world="Tool output treated as data the model can trust.",
            expected_layer=5,
            pre=_instructions_without_the_warning,
            post=_unmarked_untrusted_content,
        ),
    ]
}


@dataclass
class DefectSelection:
    ids: tuple[str, ...] = ()
    unknown: tuple[str, ...] = field(default_factory=tuple)


def select(settings: Settings) -> DefectSelection:
    known = tuple(d for d in sorted(settings.defects) if d in REGISTRY)
    unknown = tuple(d for d in sorted(settings.defects) if d not in REGISTRY)
    return DefectSelection(ids=known, unknown=unknown)


def prepare_options(settings: Settings) -> BuildOptions:
    """Build the options a connector is assembled from, defects applied."""
    selection = select(settings)
    if selection.unknown:
        raise SystemExit(f"unknown defect id(s): {', '.join(selection.unknown)}")
    options = BuildOptions(settings=settings, components=Components())
    for defect_id in selection.ids:
        defect = REGISTRY[defect_id]
        if defect.pre is not None:
            defect.pre(options)
    return options


def apply_defects(connector: Connector) -> None:
    """Apply the defects that can only be introduced once the server exists."""
    for defect_id in select(connector.settings).ids:
        defect = REGISTRY[defect_id]
        if defect.post is not None:
            defect.post(connector)
