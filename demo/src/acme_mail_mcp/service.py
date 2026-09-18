"""What the tools actually do, independent of MCP.

Keeping the mail logic out of the tool functions means the tools stay thin
(argument in, model out, upstream error translated) and this layer can be
tested directly. It also gives the defect catalogue a clean place to intervene:
every replaceable part arrives through `Components`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx

from .components import (
    BodyBudget,
    CacheKeyPolicy,
    CursorPaginator,
    DateNormalizer,
    Paginator,
    ResponseValidator,
    RetryPolicy,
    SecretScrubber,
)
from .config import Settings
from .credentials import CredentialStore
from .errors import UpstreamError
from .schemas import (
    Address,
    Attachment,
    LabelChange,
    LabelInfo,
    LabelList,
    MessageDetail,
    MessageSummary,
    SearchResult,
    SendResult,
    UpstreamMessage,
    UpstreamSummary,
)
from .upstream import AcmeMailClient, TokenManager, TokenPair


@dataclass
class ClientOptions:
    peek_on_read: bool = True
    """Read with `peek=true` so that fetching a message does not mark it read."""
    allow_refresh: bool = True
    """Renew an expired upstream token instead of failing the tool call."""


@dataclass
class Components:
    """Every part of the connector that could be built wrong, in one place."""

    paginator: Paginator = field(default_factory=CursorPaginator)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    validator: ResponseValidator = field(default_factory=ResponseValidator)
    dates: DateNormalizer = field(default_factory=DateNormalizer)
    body_budget: BodyBudget = field(default_factory=BodyBudget)
    cache_key: CacheKeyPolicy = field(default_factory=CacheKeyPolicy)
    client_options: ClientOptions = field(default_factory=ClientOptions)
    make_scrubber: Callable[[tuple[str, ...]], SecretScrubber] = SecretScrubber
    client_class: type[AcmeMailClient] = AcmeMailClient
    mark_untrusted_content: bool = True


@dataclass
class _CacheEntry:
    expires_at: float
    value: SearchResult


class SearchCache:
    """A short-lived cache in front of a chatty search endpoint."""

    def __init__(self, ttl_s: float) -> None:
        self._ttl = ttl_s
        self._entries: dict[str, _CacheEntry] = {}

    def get(self, key: str) -> SearchResult | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            del self._entries[key]
            return None
        return entry.value

    def set(self, key: str, value: SearchResult) -> None:
        self._entries[key] = _CacheEntry(expires_at=time.monotonic() + self._ttl, value=value)

    def clear(self) -> None:
        self._entries.clear()


class MailService:
    def __init__(
        self,
        settings: Settings,
        credentials: CredentialStore,
        components: Components | None = None,
        *,
        client_factory: Callable[[str, TokenPair], AcmeMailClient] | None = None,
    ) -> None:
        self.settings = settings
        self.credentials = credentials
        self.components = components or Components()
        self._http = httpx.AsyncClient(follow_redirects=False)
        self._clients: dict[str, AcmeMailClient] = {}
        self._cache = SearchCache(settings.cache_ttl_s)
        self._client_factory = client_factory or self._default_client

    # ------------------------------------------------------------------ setup
    def _default_client(
        self,
        subject: str,  # noqa: ARG002 - part of the factory signature
        tokens: TokenPair,
    ) -> AcmeMailClient:
        options = self.components.client_options
        manager = TokenManager(
            base_url=self.settings.upstream_base_url,
            tokens=tokens,
            http=self._http,
            allow_refresh=options.allow_refresh,
        )
        return self.components.client_class(
            base_url=self.settings.upstream_base_url,
            http=self._http,
            tokens=manager,
            retry=self.components.retry,
            validator=self.components.validator,
            timeout_s=self.settings.request_timeout_s,
            peek_on_read=options.peek_on_read,
        )

    def client_for(self, subject: str) -> AcmeMailClient:
        client = self._clients.get(subject)
        if client is None:
            client = self._client_factory(subject, self.credentials.tokens_for(subject))
            self._clients[subject] = client
        return client

    def scrub(self, subject: str, text: str) -> str:
        secrets = self._clients[subject].tokens.secrets if subject in self._clients else ()
        return self.components.make_scrubber(secrets).scrub(text)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ reads
    async def search(
        self,
        subject: str,
        *,
        query: str | None,
        label: str | None,
        max_results: int,
        page_token: str | None,
    ) -> SearchResult:
        wanted = max(1, min(max_results, self.settings.max_results_cap))
        key = self.components.cache_key.key(
            subject,
            {"q": query, "label": label, "max": wanted, "page": page_token},
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        client = self.client_for(subject)

        async def fetch(token: str | None, size: int) -> Any:
            return await client.search_page(query=query, label=label, page_token=token, page_size=size)

        messages, next_token = await self.components.paginator.collect(
            fetch,
            max_results=wanted,
            page_token=page_token,
            page_size_cap=self.settings.provider_page_cap,
        )
        summaries = [self._summary(m) for m in messages]
        kept, truncated = self._fit_budget(summaries)
        result = SearchResult(
            messages=kept,
            returned=len(kept),
            next_page_token=next_token,
            truncated=truncated,
        )
        self._cache.set(key, result)
        return result

    async def get_message(self, subject: str, message_id: str) -> MessageDetail:
        message = await self.client_for(subject).get_message(message_id)
        body, truncated = self.components.body_budget.apply(message.body_text)
        return MessageDetail(
            **self._summary(message).model_dump(),
            to=[Address(**a.model_dump()) for a in message.to],
            cc=[Address(**a.model_dump()) for a in message.cc],
            body=body,
            body_truncated=truncated,
            attachments=[Attachment(**a.model_dump()) for a in message.attachments],
            untrusted_content=self.components.mark_untrusted_content,
        )

    async def labels(self, subject: str) -> LabelList:
        upstream = await self.client_for(subject).labels()
        return LabelList(
            labels=[LabelInfo(name=item.name, message_count=item.message_count) for item in upstream.labels]
        )

    # -------------------------------------------------------------- mutations
    async def send(self, subject: str, *, to: list[str], subject_line: str, body: str) -> SendResult:
        result = await self.client_for(subject).send(to=to, subject=subject_line, body=body)
        self._cache.clear()
        return SendResult(id=result.id, delivered_to=list(to))

    async def trash(self, subject: str, message_id: str) -> LabelChange:
        result = await self.client_for(subject).trash(message_id)
        self._cache.clear()
        return LabelChange(id=result.id, labels=result.labels)

    async def add_label(self, subject: str, message_id: str, label: str) -> LabelChange:
        result = await self.client_for(subject).add_label(message_id, label)
        self._cache.clear()
        return LabelChange(id=result.id, labels=result.labels)

    # ----------------------------------------------------------------- mapping
    def _summary(self, message: UpstreamSummary | UpstreamMessage) -> MessageSummary:
        return MessageSummary(
            id=message.id,
            thread_id=message.thread_id,
            sender=Address(name=message.sender.name, email=message.sender.email),
            subject=message.subject,
            snippet=message.snippet,
            received_at=self.components.dates.normalize(message.received_at),
            labels=list(message.labels),
            has_attachments=message.has_attachments,
        )

    def _fit_budget(self, summaries: list[MessageSummary]) -> tuple[list[MessageSummary], bool]:
        """Drop results from the tail until the payload fits the output budget.

        A search that matches a hundred long threads should not be able to fill
        a model's context window on its own.
        """
        budget = self.settings.search_output_char_budget
        kept = list(summaries)
        truncated = False
        while kept and sum(len(s.model_dump_json()) for s in kept) > budget:
            kept.pop()
            truncated = True
        return kept, truncated


def upstream_failure(service: MailService, subject: str, error: UpstreamError) -> str:
    """The message a tool returns when the provider would not cooperate."""
    return service.scrub(subject, error.user_message())
