"""Layer 4: what the connector does on the provider's bad days.

Rate limits, outages, timeouts and a schema that changed overnight are not edge
cases for a connector — they are the job. Every test here works by making the
upstream misbehave on purpose and then reading the upstream's own log to see
what the connector did about it.
"""

from __future__ import annotations

import time

import pytest

from acme_mail_api.server import ProviderServer
from mcpqa.probes import diff_states
from mcpqa.session import Session
from support import ProviderControl, stdio_target

pytestmark = pytest.mark.layer4


def text_of(result: dict) -> str:
    return " ".join(
        block.get("text", "") for block in result.get("content", []) if block.get("type") == "text"
    )


# ------------------------------------------------------------------ pagination


def test_a_large_request_is_filled_across_pages(session: Session) -> None:
    """The provider caps a page at 50; asking for 100 must still return 100.

    Truncating here is the quietest bug a connector can have: the model gets a
    shorter list, believes it is complete, and answers confidently.
    """
    result = session.structured("search_messages", {"max_results": 100})
    ids = [message["id"] for message in result["messages"]]

    assert result["returned"] == 100, f"asked for 100, got {result['returned']}"
    assert len(set(ids)) == len(ids), "the same message came back on two pages"
    assert result["next_page_token"], "there are more messages, so a cursor is expected"


def test_the_cursor_continues_where_the_page_ended(session: Session) -> None:
    first = session.structured("search_messages", {"max_results": 50})
    second = session.structured(
        "search_messages", {"max_results": 50, "page_token": first["next_page_token"]}
    )
    first_ids = {message["id"] for message in first["messages"]}
    second_ids = {message["id"] for message in second["messages"]}

    assert second_ids, "following the cursor returned nothing"
    assert not (first_ids & second_ids), "the second page repeats the first"


# ----------------------------------------------------------------- rate limits


def test_a_rate_limit_is_waited_out_rather_than_hammered(session: Session, control: ProviderControl) -> None:
    control.fail(
        path="^/v1/labels$",
        status=429,
        times=1,
        body={"error": "rate_limited", "message": "slow down"},
        headers={"Retry-After": "1"},
    )
    before = len(control.calls())

    result = session.call_tool("list_labels").result or {}

    assert not result.get("isError"), f"the connector gave up on a retryable 429: {text_of(result)}"
    attempts = [call for call in control.calls(since=before) if call.path == "/v1/labels"]
    assert len(attempts) == 2, f"expected one retry, saw {len(attempts)} attempts"
    waited = attempts[1].started_at - attempts[0].at
    assert waited >= 0.9, f"retried after {waited:.2f}s, while the provider asked for 1s"


def test_a_persistent_rate_limit_ends_in_an_answer_not_a_hang(
    session: Session, control: ProviderControl
) -> None:
    control.fail(
        path="^/v1/labels$",
        status=429,
        body={"error": "rate_limited", "message": "slow down"},
        headers={"Retry-After": "1"},
    )
    before = len(control.calls())

    result = session.call_tool("list_labels").result or {}

    assert result.get("isError")
    assert "rate limit" in text_of(result).lower()
    attempts = [call for call in control.calls(since=before) if call.path == "/v1/labels"]
    assert len(attempts) <= 3, f"the connector kept retrying: {len(attempts)} attempts"


# --------------------------------------------------------------------- outages


def test_a_transient_outage_is_retried(session: Session, control: ProviderControl) -> None:
    control.fail(path="^/v1/messages$", status=503, times=2)
    before = len(control.calls())

    result = session.call_tool("search_messages", {"query": "invoice", "max_results": 3}).result or {}

    assert not result.get("isError"), f"two 503s should be survivable: {text_of(result)}"
    attempts = [call for call in control.calls(since=before) if call.path == "/v1/messages"]
    assert len(attempts) == 3, f"expected three attempts, saw {len(attempts)}"


def test_a_lasting_outage_is_reported_without_a_retry_storm(
    session: Session, control: ProviderControl
) -> None:
    control.fail(path="^/v1/messages$", status=503)
    before = len(control.calls())

    result = session.call_tool("search_messages", {"query": "invoice"}).result or {}

    assert result.get("isError")
    assert "unavailable" in text_of(result).lower()
    attempts = [call for call in control.calls(since=before) if call.path == "/v1/messages"]
    assert len(attempts) <= 3, f"{len(attempts)} attempts against a server that is already down"


def test_a_non_json_response_is_reported_as_a_provider_problem(
    session: Session, control: ProviderControl
) -> None:
    """A proxy's HTML error page is a classic, and json.loads() is not a plan.

    Every attempt gets the HTML: one bad response is retried and survived, which
    is correct behaviour and would hide the case this test is about.
    """
    control.garbage(path="^/v1/labels$", times=None)

    result = session.call_tool("list_labels").result or {}

    assert result.get("isError")
    assert "acme mail" in text_of(result).lower()


# -------------------------------------------------------------------- timeouts


def test_a_hanging_provider_does_not_hang_the_tool(
    provider: ProviderServer, control: ProviderControl
) -> None:
    target = stdio_target(provider, timeout_s=1.0)
    control.delay(path="^/v1/labels$", ms=3000, times=None)

    with Session(target=target, protocol_version="2026-07-28") as session:
        result = session.call_tool("list_labels").result or {}
        assert result.get("isError"), "a timeout must surface as an error"
        assert "did not respond" in text_of(result).lower()

        # And the connection is still usable afterwards.
        after = session.structured("search_messages", {"query": "invoice", "max_results": 2})
        assert after["returned"] >= 1


def test_a_send_that_times_out_is_not_sent_twice(provider: ProviderServer, control: ProviderControl) -> None:
    """The one retry that must never happen.

    The provider delivers the mail and then takes three seconds to say so; the
    connector gives up after one. Retrying here is indistinguishable from
    working, right up until someone receives the same message twice.
    """
    target = stdio_target(provider, timeout_s=1.0)
    control.delay_response(path="^/v1/messages/send$", ms=3000, times=1)
    before_state = control.snapshot()
    before_calls = len(control.calls())

    with Session(target=target, protocol_version="2026-07-28") as session:
        result = (
            session.call_tool(
                "send_message",
                {"to": ["dana.ruiz@acme.test"], "subject_line": "status", "body": "on my way"},
            ).result
            or {}
        )

    assert result.get("isError"), "the caller must be told the send did not complete"

    # The provider is still working on the request the connector walked away
    # from; wait for it to land before counting what exists.
    time.sleep(2.5)
    sends = [call for call in control.calls(since=before_calls) if call.path == "/v1/messages/send"]
    assert len(sends) == 1, f"the connector sent the message {len(sends)} times"
    created = {
        fact.split("|")[1]
        for fact in diff_states(before_state, control.snapshot()).added
        if "|message:" in fact
    }
    assert len(created) <= 1, f"more than one message was created: {sorted(created)}"


# ------------------------------------------------------------------ schema drift


@pytest.mark.parametrize("profile_name", ["rename_subject_to_title", "drop_snippet"])
def test_a_changed_upstream_schema_is_an_error_not_an_empty_field(
    session: Session, control: ProviderControl, profile_name: str
) -> None:
    """The provider ships a v2 field name overnight.

    The tempting implementation — `payload.get("subject", "")` — turns that into
    a mailbox where every message is untitled, and nothing anywhere reports a
    problem.
    """
    control.drift(profile_name)

    result = session.call_tool("search_messages", {"query": "invoice", "max_results": 3}).result or {}

    assert result.get("isError"), f"{profile_name} was absorbed silently: {result}"
    message = text_of(result).lower()
    assert "does not understand" in message or "unexpected" in message
    assert "api" in message, "the message should point at the provider, not at the user"
