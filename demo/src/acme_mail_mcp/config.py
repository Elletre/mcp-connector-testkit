"""Connector settings, and where they come from.

Two deployment shapes, because they have different security properties and the
test kit exercises both:

* **stdio** — one user, credentials handed to the process by whoever launched
  it (`ACME_MAIL_ACCESS_TOKEN`).
* **streamable HTTP** — many users, each request carrying a token *this server*
  issued. Upstream credentials never travel on the wire; they are looked up
  server-side from the caller's identity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Settings:
    upstream_base_url: str = "http://127.0.0.1:8100"
    access_token: str | None = None
    refresh_token: str | None = None
    account: str = "local"
    """The subject used in stdio mode, where there is no per-request identity."""

    defects: frozenset[str] = field(default_factory=frozenset)

    resource_url: str = "http://127.0.0.1:8200/mcp"
    """This server's resource identifier: tokens issued for anything else are refused."""
    issuer_url: str = "https://auth.acme.test"

    max_results_cap: int = 100
    provider_page_cap: int = 50
    search_output_char_budget: int = 40_000
    body_char_budget: int = 20_000
    request_timeout_s: float = 10.0
    max_attempts: int = 3
    cache_ttl_s: float = 15.0

    @classmethod
    def from_env(cls, **overrides: object) -> Settings:
        env = os.environ
        raw_defects = env.get("ACME_MCP_DEFECTS", "")
        settings = cls(
            upstream_base_url=env.get("ACME_MAIL_BASE_URL", cls.upstream_base_url),
            access_token=env.get("ACME_MAIL_ACCESS_TOKEN"),
            refresh_token=env.get("ACME_MAIL_REFRESH_TOKEN"),
            account=env.get("ACME_MAIL_ACCOUNT", cls.account),
            defects=frozenset(d.strip() for d in raw_defects.split(",") if d.strip()),
            resource_url=env.get("ACME_MCP_RESOURCE_URL", cls.resource_url),
            issuer_url=env.get("ACME_MCP_ISSUER_URL", cls.issuer_url),
            request_timeout_s=float(env.get("ACME_MAIL_TIMEOUT_S", cls.request_timeout_s)),
        )
        return replace(settings, **overrides) if overrides else settings
