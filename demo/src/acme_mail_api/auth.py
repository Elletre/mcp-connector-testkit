"""Access tokens, scopes and the refresh grant of the Acme Mail provider.

Deliberately close to how a real provider behaves, because the connector's
token handling is what layer 3 tests: an expired token and a revoked token are
different errors, a missing scope is a third, and only one of the three is
worth retrying after a refresh.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .store import ApiError

SCOPE_READ = "mail.read"
SCOPE_SEND = "mail.send"
SCOPE_MODIFY = "mail.modify"
ALL_SCOPES = (SCOPE_READ, SCOPE_SEND, SCOPE_MODIFY)


@dataclass
class AccessToken:
    token: str
    subject: str
    scopes: list[str]
    expires_at: float | None = None
    revoked: bool = False

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= time.time()


@dataclass
class RefreshToken:
    token: str
    subject: str
    scopes: list[str]
    revoked: bool = False


@dataclass
class AuthStore:
    access: dict[str, AccessToken] = field(default_factory=dict)
    refresh: dict[str, RefreshToken] = field(default_factory=dict)
    _minted: int = 0

    def __post_init__(self) -> None:
        if not self.access:
            self.reset()

    def reset(self) -> None:
        self.access = {}
        self.refresh = {}
        self._minted = 0
        self._seed("alice@acme.test", "at_alice_rw", "rt_alice", list(ALL_SCOPES))
        self._seed("alice@acme.test", "at_alice_ro", None, [SCOPE_READ])
        self._seed("bob@globex.test", "at_bob_rw", "rt_bob", list(ALL_SCOPES))

    def _seed(self, subject: str, access: str, refresh: str | None, scopes: list[str]) -> None:
        self.access[access] = AccessToken(token=access, subject=subject, scopes=scopes)
        if refresh:
            self.refresh[refresh] = RefreshToken(token=refresh, subject=subject, scopes=scopes)

    # ------------------------------------------------------------ verification
    def verify(self, header: str | None, *, required_scope: str) -> AccessToken:
        if not header or not header.lower().startswith("bearer "):
            raise ApiError(401, "invalid_token", "Missing bearer token")
        token = self.access.get(header[7:].strip())
        if token is None or token.revoked:
            raise ApiError(401, "invalid_token", "Unknown or revoked access token")
        if token.expired:
            raise ApiError(401, "token_expired", "The access token has expired")
        if required_scope not in token.scopes:
            raise ApiError(
                403,
                "insufficient_scope",
                f"This token is not allowed to {required_scope}",
                required_scope=required_scope,
            )
        return token

    # ------------------------------------------------------------------ grants
    def refresh_grant(self, refresh_token: str) -> AccessToken:
        grant = self.refresh.get(refresh_token)
        if grant is None or grant.revoked:
            raise ApiError(400, "invalid_grant", "Unknown or revoked refresh token")
        self._minted += 1
        minted = AccessToken(
            token=f"at_{grant.subject.split('@')[0]}_refreshed_{self._minted}",
            subject=grant.subject,
            scopes=list(grant.scopes),
            expires_at=time.time() + 3600,
        )
        self.access[minted.token] = minted
        return minted

    # ----------------------------------------------------------------- control
    def expire(self, token: str) -> None:
        self._require(token).expires_at = time.time() - 1

    def revoke(self, token: str) -> None:
        self._require(token).revoked = True

    def revoke_refresh(self, token: str) -> None:
        grant = self.refresh.get(token)
        if grant is None:
            raise ApiError(404, "not_found", f"No refresh token {token}")
        grant.revoked = True

    def _require(self, token: str) -> AccessToken:
        found = self.access.get(token)
        if found is None:
            raise ApiError(404, "not_found", f"No access token {token}")
        return found
