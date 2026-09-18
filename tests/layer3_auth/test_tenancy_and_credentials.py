"""Layer 3: whose data is this, and where did the credential go?

The failures here are the expensive ones. A pagination bug costs a support
ticket; one tenant seeing another tenant's mailbox costs the customer. Both are
invisible to a test suite that only ever runs as one user, which is how most
connector suites are written.
"""

from __future__ import annotations

import pytest

from acme_mail_api.server import ProviderServer
from mcpqa.leaks import scan
from mcpqa.probes import diff_states
from mcpqa.session import Session
from mcpqa.target import HttpTarget
from support import ALICE_UPSTREAM, CANARY_ALICE, CANARY_BOB, ProviderControl, stdio_target

pytestmark = pytest.mark.layer3


def test_a_tenant_never_sees_the_other_tenants_mail(
    alice_session: Session, bob_session: Session, control: ProviderControl
) -> None:
    alice = alice_session.structured("search_messages", {"query": "CANARY"})
    bob = bob_session.structured("search_messages", {"query": "CANARY"})

    assert [message["subject"] for message in alice["messages"]] == ["Q3 board deck — confidential"]
    assert [message["subject"] for message in bob["messages"]] == ["Vendor contract renewal — internal only"]

    crossed = scan(bob_session.transcript.texts(), [CANARY_ALICE])
    assert not crossed, f"Alice's data reached Bob: {[leak.describe() for leak in crossed]}"
    crossed_back = scan(alice_session.transcript.texts(), [CANARY_BOB])
    assert not crossed_back


def test_the_search_cache_is_not_shared_between_tenants(alice_session: Session, bob_session: Session) -> None:
    """The same query, from two people, one after the other.

    A cache keyed on the query alone looks perfect in single-user testing and
    serves one customer's mail to another the moment a second user arrives.
    """
    arguments = {"query": "CANARY", "max_results": 5}
    alice_session.structured("search_messages", arguments)
    bob = bob_session.structured("search_messages", arguments)

    assert bob["messages"], "Bob got an empty result where he has a matching message"
    assert all("board deck" not in message["subject"] for message in bob["messages"])
    assert not scan(bob_session.transcript.texts(), [CANARY_ALICE])


def test_an_expired_upstream_token_is_renewed_without_the_caller_noticing(
    session: Session, control: ProviderControl
) -> None:
    assert session.structured("list_labels")["labels"], "the first call should work"
    control.expire_token(ALICE_UPSTREAM)
    before = len(control.calls())

    result = session.call_tool("list_labels").result or {}

    assert not result.get("isError"), f"the call failed instead of refreshing: {result}"
    refreshes = [call for call in control.calls(since=before) if call.path == "/oauth/token"]
    assert len(refreshes) == 1, f"expected exactly one refresh, saw {len(refreshes)}"


def test_an_unrenewable_credential_produces_an_answer_a_human_can_act_on(
    session: Session, control: ProviderControl
) -> None:
    control.expire_token(ALICE_UPSTREAM)
    control.revoke_refresh("rt_alice")

    result = session.call_tool("list_labels").result or {}
    text = " ".join(
        block.get("text", "") for block in result.get("content", []) if block.get("type") == "text"
    )

    assert result.get("isError"), "a dead credential must be reported, not swallowed"
    assert "reconnect" in text.lower(), f"the message does not say what to do: {text!r}"
    # The session fixture scans the whole transcript for credentials on teardown.


def test_a_tool_the_credential_may_not_use_is_refused_and_changes_nothing(
    provider: ProviderServer, control: ProviderControl
) -> None:
    read_only = stdio_target(provider, access_token="at_alice_ro", refresh_token="")
    before = control.snapshot()
    with Session(target=read_only, protocol_version="2026-07-28") as session:
        result = (
            session.call_tool(
                "send_message",
                {"to": ["dana.ruiz@acme.test"], "subject_line": "nope", "body": "nope"},
            ).result
            or {}
        )
        text = " ".join(
            block.get("text", "") for block in result.get("content", []) if block.get("type") == "text"
        )
        leaks = scan(session.transcript.texts(), ["at_alice_ro"])

    assert result.get("isError"), "a call outside the granted scopes must fail"
    assert "not permitted" in text.lower() or "mail.send" in text
    assert not diff_states(before, control.snapshot()).changed, "a refused call still sent mail"
    assert not leaks, f"the refusal leaked the credential: {[leak.describe() for leak in leaks]}"


def test_an_upstream_credential_is_not_a_ticket_into_the_mcp_server(
    http_target: HttpTarget,
) -> None:
    """Token passthrough, tested from the outside.

    The connector holds an Acme Mail token for each user. If presenting that
    token to the MCP endpoint works, then anything that can read it — a log, a
    proxy, another service — can act as that user here.
    """
    stolen = HttpTarget(url=http_target.url, bearer=ALICE_UPSTREAM, name="stolen-upstream-token")
    session = Session(target=stolen, protocol_version="2026-07-28")
    session.wire.start()
    try:
        exchange = session.call("tools/list")
    finally:
        session.close()

    assert exchange.status == 401, f"an upstream credential was accepted: HTTP {exchange.status}"
