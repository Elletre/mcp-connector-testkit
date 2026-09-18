"""Who is calling, and which upstream account that maps to.

The MCP authorization spec is explicit that a server must not forward the
token it was given to an upstream API, and must not accept a token that was
issued for someone else's audience. So there are two separate credentials
here: the MCP token the caller presents (validated against this server's own
resource identifier) and the Acme Mail token the connector holds on that
user's behalf, which the caller never sees.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from mcp.server.auth.provider import AccessToken

from .upstream import TokenPair


@dataclass(frozen=True)
class Principal:
    """A caller of the MCP server, as this server knows them."""

    mcp_token: str
    subject: str
    scopes: tuple[str, ...]
    audience: str


@dataclass
class CredentialStore:
    """Subject -> upstream credentials. In production this is a secrets store."""

    upstream: dict[str, TokenPair] = field(default_factory=dict)
    principals: dict[str, Principal] = field(default_factory=dict)

    def tokens_for(self, subject: str) -> TokenPair:
        try:
            pair = self.upstream[subject]
        except KeyError:
            raise LookupError(f"no Acme Mail account connected for {subject}") from None
        return pair

    def principal(self, mcp_token: str) -> Principal | None:
        return self.principals.get(mcp_token)

    def subject_for_upstream_token(self, token: str) -> str | None:
        """Reverse lookup, used only by the token-passthrough defect."""
        for subject, pair in self.upstream.items():
            if pair.access == token:
                return subject
        return None


def demo_store(resource_url: str) -> CredentialStore:
    """The two demo users, as an identity provider would have handed them over."""
    return CredentialStore(
        upstream={
            "alice@acme.test": TokenPair(access="at_alice_rw", refresh="rt_alice"),
            "bob@globex.test": TokenPair(access="at_bob_rw", refresh="rt_bob"),
        },
        principals={
            "mcp_alice": Principal(
                mcp_token="mcp_alice",
                subject="alice@acme.test",
                scopes=("mail",),
                audience=resource_url,
            ),
            "mcp_bob": Principal(
                mcp_token="mcp_bob",
                subject="bob@globex.test",
                scopes=("mail",),
                audience=resource_url,
            ),
        },
    )


@dataclass
class StaticTokenVerifier:
    """Stands in for token introspection against the authorization server.

    `resource` is what makes audience validation possible: the bearer
    middleware refuses a token that was minted for a different resource, which
    is the check that stops an upstream token (or another service's token) from
    being replayed here.

    `accept_upstream_tokens` exists only for the token-passthrough defect: it
    makes the server treat an Acme Mail credential as proof of identity, which
    is the antipattern the authorization spec calls out by name.
    """

    store: CredentialStore
    resource_url: str
    accept_upstream_tokens: bool = False

    async def verify_token(self, token: str) -> AccessToken | None:
        principal = self.store.principal(token)
        if principal is not None:
            return AccessToken(
                token=token,
                client_id="acme-mail-demo-client",
                scopes=list(principal.scopes),
                subject=principal.subject,
                resource=principal.audience,
                expires_at=int(time.time()) + 3600,
            )
        if self.accept_upstream_tokens:
            subject = self.store.subject_for_upstream_token(token)
            if subject is not None:
                return AccessToken(
                    token=token,
                    client_id="passthrough",
                    scopes=["mail"],
                    subject=subject,
                    resource=self.resource_url,
                    expires_at=int(time.time()) + 3600,
                )
        return None
