"""Layer 4, the quieter half: does the data survive the trip?

Nothing here fails loudly. A dropped UTC offset, a normalised unicode subject
or an unbounded body all look like success from the outside, and all three
change what the model believes.
"""

from __future__ import annotations

import json

import pytest

from mcpqa.probes import diff_states
from mcpqa.session import Session
from support import ProviderControl

pytestmark = pytest.mark.layer4

UNICODE_SUBJECT = "Rechnung 📎 契約書 — مرحبا — café"  # decomposed é, on purpose


def text_of(result: dict) -> str:
    return " ".join(
        block.get("text", "") for block in result.get("content", []) if block.get("type") == "text"
    )


def test_a_subject_arrives_exactly_as_it_was_sent(session: Session) -> None:
    """Emoji, CJK, right-to-left text and a combining accent, byte for byte.

    "Helpful" normalisation (NFC, transliteration, stripping what the terminal
    cannot draw) makes search results stop matching what the user typed.
    """
    message = session.structured("get_message", {"message_id": "msg_00905"})
    assert message["subject"] == UNICODE_SUBJECT
    assert "café" not in json.dumps(message, ensure_ascii=False), "the accent was recomposed"


def test_a_timestamp_keeps_its_offset(session: Session) -> None:
    """23:40 in New York is the next day in Berlin, and the offset is the data."""
    message = session.structured("get_message", {"message_id": "msg_00904"})
    assert message["received_at"] == "2026-09-29T23:40:00-05:00"


def test_attachment_metadata_is_not_lost(session: Session) -> None:
    message = session.structured("get_message", {"message_id": "msg_00901"})
    assert message["has_attachments"] is True
    assert message["attachments"] == [
        {
            "filename": "INV-2026-0413.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 91204,
        }
    ]


def test_a_huge_message_is_cut_down_and_says_so(session: Session) -> None:
    """One message must not be able to fill the model's context window."""
    message = session.structured("get_message", {"message_id": "msg_00906"})

    assert message["body_truncated"] is True
    assert len(message["body"]) < 25_000, "the budget did not hold"
    assert "truncated by the connector" in message["body"], "the cut is silent"
    assert "2026-09-10T02:14:07Z gateway" in message["body"], "the kept part is the beginning"


def test_a_hundred_results_still_fit_in_the_output_budget(session: Session) -> None:
    result = session.structured("search_messages", {"max_results": 100})
    size = len(json.dumps(result))
    assert result["returned"] == 100
    assert size < 60_000, f"a single search returned {size} characters"


def test_an_empty_result_is_a_result_not_an_error(session: Session) -> None:
    exchange = session.call_tool("search_messages", {"query": "nothing-matches-this-string"})
    result = exchange.result or {}
    assert not result.get("isError"), "an empty mailbox search is a normal answer"
    assert result["structuredContent"]["returned"] == 0
    assert result["structuredContent"]["messages"] == []


def test_an_unknown_message_id_says_which_one(session: Session) -> None:
    result = session.call_tool("get_message", {"message_id": "msg_does_not_exist"}).result or {}
    assert result.get("isError")
    assert "msg_does_not_exist" in text_of(result), "the model cannot correct what it cannot see"


def test_trashing_a_message_takes_it_out_of_the_default_view(
    session: Session, control: ProviderControl
) -> None:
    before = control.snapshot()
    session.call_tool("trash_message", {"message_id": "msg_00902"})
    diff = diff_states(before, control.snapshot())

    assert diff.destructive, "trashing must remove the message from the inbox"
    inbox = session.structured("search_messages", {"query": "ByteFeed Weekly", "max_results": 20})
    assert all(message["id"] != "msg_00902" for message in inbox["messages"])

    trash = session.structured("search_messages", {"label": "TRASH", "max_results": 20})
    assert any(message["id"] == "msg_00902" for message in trash["messages"]), "it went nowhere"


def test_a_second_trash_call_changes_nothing_further(session: Session, control: ProviderControl) -> None:
    """The idempotency the tool advertises, checked against state."""
    session.call_tool("trash_message", {"message_id": "msg_00902"})
    settled = control.snapshot()
    session.call_tool("trash_message", {"message_id": "msg_00902"})
    assert not diff_states(settled, control.snapshot()).changed


def test_a_label_can_be_added_twice_without_piling_up(session: Session, control: ProviderControl) -> None:
    session.call_tool("add_label", {"message_id": "msg_00901", "label": "Later"})
    settled = control.snapshot()
    session.call_tool("add_label", {"message_id": "msg_00901", "label": "Later"})
    assert not diff_states(settled, control.snapshot()).changed
