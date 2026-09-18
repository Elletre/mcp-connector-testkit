"""The test double has to be trustworthy before anything built on it means much."""

from __future__ import annotations

import json
import runpy
import time
from collections.abc import Iterator
from pathlib import Path

import httpx2 as httpx
import pytest

from acme_mail_api.server import ProviderServer
from acme_mail_api.store import Store

ALICE = "at_alice_rw"
ALICE_RO = "at_alice_ro"
BOB = "at_bob_rw"


@pytest.fixture(scope="module")
def provider() -> Iterator[ProviderServer]:
    with ProviderServer() as server:
        yield server


@pytest.fixture
def client(provider: ProviderServer) -> Iterator[httpx.Client]:
    httpx.post(f"{provider.base_url}/_control/reset")
    with httpx.Client(base_url=provider.base_url, timeout=10) as client:
        yield client


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ pagination


def test_search_paginates_through_every_match(client: httpx.Client) -> None:
    seen: list[str] = []
    token: str | None = None
    for _ in range(20):
        params = {"page_size": 25}
        if token:
            params["page_token"] = token
        response = client.get("/v1/messages", params=params, headers=auth(ALICE))
        assert response.status_code == 200
        body = response.json()
        seen.extend(m["id"] for m in body["messages"])
        token = body["next_page_token"]
        if token is None:
            break
    assert token is None
    assert len(seen) == len(set(seen)), "pagination returned duplicates"
    assert len(seen) > 50, "fixture mailbox is too small to exercise pagination"


def test_page_size_is_capped(client: httpx.Client) -> None:
    response = client.get("/v1/messages", params={"page_size": 500}, headers=auth(ALICE))
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_argument"


def test_cursor_is_bound_to_its_query(client: httpx.Client) -> None:
    first = client.get("/v1/messages", params={"q": "invoice", "page_size": 2}, headers=auth(ALICE)).json()
    assert first["next_page_token"], "expected more than one page for this query"
    response = client.get(
        "/v1/messages",
        params={"q": "bytefeed", "page_size": 2, "page_token": first["next_page_token"]},
        headers=auth(ALICE),
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_cursor"


# ----------------------------------------------------------------------- auth


def test_scopes_are_enforced(client: httpx.Client) -> None:
    response = client.post(
        "/v1/messages/send",
        json={"to": ["dana.ruiz@acme.test"], "subject": "hi", "body": "hi"},
        headers=auth(ALICE_RO),
    )
    assert response.status_code == 403
    assert response.json()["error"] == "insufficient_scope"
    assert response.json()["required_scope"] == "mail.send"


def test_expired_and_revoked_tokens_are_different_errors(client: httpx.Client) -> None:
    client.post(f"/_control/tokens/{ALICE_RO}/expire")
    expired = client.get("/v1/labels", headers=auth(ALICE_RO))
    assert expired.status_code == 401
    assert expired.json()["error"] == "token_expired"

    client.post(f"/_control/tokens/{BOB}/revoke")
    revoked = client.get("/v1/labels", headers=auth(BOB))
    assert revoked.status_code == 401
    assert revoked.json()["error"] == "invalid_token"


def test_refresh_grant_mints_a_working_token(client: httpx.Client) -> None:
    client.post(f"/_control/tokens/{ALICE}/expire")
    assert client.get("/v1/labels", headers=auth(ALICE)).status_code == 401

    minted = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "rt_alice"})
    assert minted.status_code == 200
    token = minted.json()["access_token"]
    assert token != ALICE
    assert client.get("/v1/labels", headers=auth(token)).status_code == 200

    client.post("/_control/tokens/rt_alice/revoke-refresh")
    denied = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "rt_alice"})
    assert denied.status_code == 400
    assert denied.json()["error"] == "invalid_grant"


def test_tenants_cannot_see_each_other(client: httpx.Client) -> None:
    alice = client.get("/v1/messages", params={"q": "CANARY"}, headers=auth(ALICE)).json()
    bob = client.get("/v1/messages", params={"q": "CANARY"}, headers=auth(BOB)).json()
    assert [m["subject"] for m in alice["messages"]] == ["Q3 board deck — confidential"]
    assert [m["subject"] for m in bob["messages"]] == ["Vendor contract renewal — internal only"]

    alice_body = client.get("/v1/messages/msg_00903", params={"peek": "true"}, headers=auth(ALICE))
    assert "CANARY-ALICE-7f3a91" in alice_body.json()["body_text"]
    assert client.get("/v1/messages/msg_00903", headers=auth(BOB)).status_code == 404


# ------------------------------------------------------------------- read side


def test_peek_does_not_mark_as_read(client: httpx.Client) -> None:
    unread = client.get("/v1/messages", params={"label": "UNREAD"}, headers=auth(ALICE)).json()
    message_id = unread["messages"][0]["id"]

    peeked = client.get(f"/v1/messages/{message_id}", params={"peek": "true"}, headers=auth(ALICE))
    assert "UNREAD" in peeked.json()["labels"]

    fetched = client.get(f"/v1/messages/{message_id}", headers=auth(ALICE))
    assert "UNREAD" not in fetched.json()["labels"]


def test_unicode_and_timezone_are_preserved(client: httpx.Client) -> None:
    unicode_message = client.get(
        "/v1/messages/msg_00905", params={"peek": "true"}, headers=auth(ALICE)
    ).json()
    # NB: "cafe" + U+0301 (decomposed), not the precomposed U+00E9.
    assert unicode_message["subject"] == "Rechnung 📎 契約書 — مرحبا — café"

    flight = client.get("/v1/messages/msg_00904", params={"peek": "true"}, headers=auth(ALICE)).json()
    assert flight["received_at"] == "2026-09-29T23:40:00-05:00"


# ------------------------------------------------------- faults and rate limits


def test_injected_status_applies_a_limited_number_of_times(client: httpx.Client) -> None:
    client.post(
        "/_control/faults",
        json={
            "rules": [
                {
                    "id": "flaky-search",
                    "kind": "status",
                    "path_pattern": "^/v1/messages$",
                    "status": 503,
                    "times": 2,
                }
            ]
        },
    )
    assert client.get("/v1/messages", headers=auth(ALICE)).status_code == 503
    assert client.get("/v1/messages", headers=auth(ALICE)).status_code == 503
    assert client.get("/v1/messages", headers=auth(ALICE)).status_code == 200


def test_rate_limit_returns_retry_after(client: httpx.Client) -> None:
    client.post("/_control/rate_limit", json={"capacity": 1, "refill_per_sec": 1})
    assert client.get("/v1/labels", headers=auth(ALICE)).status_code == 200
    limited = client.get("/v1/labels", headers=auth(ALICE))
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1


def test_drift_profile_changes_the_response_shape(client: httpx.Client) -> None:
    client.post("/_control/drift", json={"profile": "rename_subject_to_title"})
    body = client.get("/v1/messages", params={"page_size": 1}, headers=auth(ALICE)).json()
    assert "title" in body["messages"][0]
    assert "subject" not in body["messages"][0]


# ------------------------------------------------------------ audit and oracle


def test_audit_records_every_call_with_its_subject(client: httpx.Client) -> None:
    client.get("/v1/labels", headers=auth(ALICE))
    client.get("/v1/labels", headers=auth(BOB))
    entries = client.get("/_control/audit").json()["entries"]
    subjects = [e["subject"] for e in entries if e["path"] == "/v1/labels"]
    assert subjects == ["alice@acme.test", "bob@globex.test"]
    assert all(e["duration_ms"] >= 0 for e in entries)


def test_state_diff_classifies_mutations(client: httpx.Client) -> None:
    def facts() -> set[str]:
        return set(client.get("/_control/state", params={"account": "alice@acme.test"}).json()["facts"])

    before = facts()
    client.get("/v1/labels", headers=auth(ALICE))
    assert facts() == before, "a read must not change the state"

    client.post("/v1/messages/msg_00901/labels", json={"label": "Team"}, headers=auth(ALICE))
    additive = facts()
    assert additive - before and not before - additive, "adding a label is purely additive"

    client.post("/v1/messages/msg_00901/trash", headers=auth(ALICE))
    destructive = facts()
    assert additive - destructive, "trashing a message must remove facts"


# --------------------------------------------------------------- the fixtures


def test_fixture_file_matches_the_generator() -> None:
    """The committed fixtures must be exactly what the generator produces."""
    root = Path(__file__).resolve().parents[2]
    generator = root / "tools/gen_fixtures.py"
    rendered = str(runpy.run_path(str(generator))["render"]())
    committed = (root / "demo/src/acme_mail_api/fixtures/mailboxes.json").read_text(encoding="utf-8")
    assert rendered == committed, "run `make fixtures` and commit the result"


def test_store_loads_the_context_bomb_expanded() -> None:
    """The 2 MB message is stored folded so the repository stays small."""
    fixtures = json.loads(
        (Path(__file__).resolve().parents[2] / "demo/src/acme_mail_api/fixtures/mailboxes.json").read_text(
            encoding="utf-8"
        )
    )
    folded = next(m for m in fixtures["mailboxes"][0]["messages"] if m["id"] == "msg_00906")
    assert folded["body_repeat"] > 1
    assert len(folded["body_text"]) < 1000

    message = Store().get("alice@acme.test", "msg_00906", peek=True)
    assert len(message.body_text) > 2_000_000


def test_server_startup_is_quick(provider: ProviderServer) -> None:
    started = time.monotonic()
    response = httpx.get(f"{provider.base_url}/v1/labels", headers=auth(ALICE), timeout=5)
    assert response.status_code == 200
    assert time.monotonic() - started < 2
