"""Wire models of the Acme Mail provider API.

These are the *upstream's* shapes, not the connector's. The connector validates
what it receives against its own copies, which is what makes schema drift
(`/_control/drift`) visible instead of silently turning into nulls.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

INBOX = "INBOX"
UNREAD = "UNREAD"
TRASH = "TRASH"
SENT = "SENT"
STARRED = "STARRED"

SYSTEM_LABELS = (INBOX, UNREAD, TRASH, SENT, STARRED)


class Address(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    email: str


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    mime_type: str
    size_bytes: int


class Message(BaseModel):
    """A message as stored by the provider."""

    model_config = ConfigDict(extra="forbid")

    id: str
    thread_id: str
    account: str
    sender: Address = Field(alias="from")
    to: list[Address]
    cc: list[Address] = Field(default_factory=list)
    subject: str
    snippet: str
    body_text: str
    received_at: str
    """RFC 3339 with an explicit UTC offset. The offset is part of the data."""
    labels: list[str]
    attachments: list[Attachment] = Field(default_factory=list)

    @property
    def size_bytes(self) -> int:
        return len(self.body_text.encode("utf-8"))

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "from": self.sender.model_dump(exclude_none=True),
            "subject": self.subject,
            "snippet": self.snippet,
            "received_at": self.received_at,
            "labels": list(self.labels),
            "has_attachments": bool(self.attachments),
        }

    def full(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "to": [a.model_dump(exclude_none=True) for a in self.to],
            "cc": [a.model_dump(exclude_none=True) for a in self.cc],
            "body_text": self.body_text,
            "attachments": [a.model_dump() for a in self.attachments],
            "size_bytes": self.size_bytes,
        }


class FixtureMessage(Message):
    """A message as written in the fixture file.

    `body_repeat` keeps the multi-megabyte message (the one that exists to blow
    up an agent's context window) out of the repository: the body is stored once
    and repeated on load.
    """

    body_repeat: int = 1

    def to_message(self) -> Message:
        data = self.model_dump(by_alias=True, exclude={"body_repeat"})
        data["body_text"] = self.body_text * self.body_repeat
        return Message.model_validate(data)


class Mailbox(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str
    display_name: str
    user_labels: list[str]
    messages: list[FixtureMessage]


class Fixtures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_by: str
    seed: int
    mailboxes: list[Mailbox]
