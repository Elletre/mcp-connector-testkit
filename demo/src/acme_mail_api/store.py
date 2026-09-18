"""In-memory mailbox store for the Acme Mail provider.

Two things here exist for the test kit rather than for the "product":

* `facts()` returns the mailbox state as a flat set of strings. Layer 2 uses the
  difference between two snapshots as its oracle: an empty diff means the call
  was read-only, added-only facts mean the mutation was additive, and a removed
  fact means it was destructive. That is the whole definition, and it is
  checkable without knowing what any particular tool is supposed to do.
* `get()` marks a message read unless `peek` is set, mirroring IMAP's
  `BODY[]` / `BODY.PEEK[]` distinction — the classic way a "read-only" mail
  tool quietly mutates a mailbox.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import INBOX, SENT, STARRED, TRASH, UNREAD, Address, Fixtures, Message

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "mailboxes.json"


class ApiError(Exception):
    """An error the provider reports to its caller."""

    def __init__(self, status: int, code: str, message: str, **extra: object) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


@dataclass(frozen=True)
class Page:
    messages: list[Message]
    next_page_token: str | None


def _sort_key(message: Message) -> tuple[datetime, str]:
    return (datetime.fromisoformat(message.received_at), message.id)


def _encode_cursor(offset: int, query_fingerprint: str) -> str:
    raw = json.dumps({"o": offset, "q": query_fingerprint}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(token: str, query_fingerprint: str) -> int:
    padded = token + "=" * (-len(token) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        offset = int(data["o"])
        fingerprint = str(data["q"])
    except Exception as exc:
        raise ApiError(400, "invalid_cursor", "page_token is not a valid cursor") from exc
    if fingerprint != query_fingerprint:
        raise ApiError(400, "invalid_cursor", "page_token does not match the current query")
    return offset


class Store:
    """The mailboxes, plus the sequence used to mint ids for sent messages."""

    MAX_PAGE_SIZE = 50
    DEFAULT_PAGE_SIZE = 25

    def __init__(self, fixtures_path: Path = FIXTURES_PATH) -> None:
        self._fixtures_path = fixtures_path
        self._accounts: dict[str, dict[str, Message]] = {}
        self._user_labels: dict[str, list[str]] = {}
        self._display_names: dict[str, str] = {}
        self._sent_counter = 0
        self.reset()

    # ------------------------------------------------------------------ setup
    def reset(self) -> None:
        fixtures = Fixtures.model_validate_json(self._fixtures_path.read_text(encoding="utf-8"))
        self._accounts = {}
        self._user_labels = {}
        self._display_names = {}
        for mailbox in fixtures.mailboxes:
            self._accounts[mailbox.account] = {m.id: m.to_message() for m in mailbox.messages}
            self._user_labels[mailbox.account] = list(mailbox.user_labels)
            self._display_names[mailbox.account] = mailbox.display_name
        self._sent_counter = 0

    def accounts(self) -> list[str]:
        return sorted(self._accounts)

    def _mailbox(self, account: str) -> dict[str, Message]:
        try:
            return self._accounts[account]
        except KeyError:
            raise ApiError(404, "account_not_found", f"No mailbox for {account}") from None

    # ------------------------------------------------------------------ reads
    def search(
        self,
        account: str,
        *,
        query: str | None = None,
        label: str | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> Page:
        size = self.DEFAULT_PAGE_SIZE if page_size is None else page_size
        if size < 1 or size > self.MAX_PAGE_SIZE:
            raise ApiError(
                400,
                "invalid_argument",
                f"page_size must be between 1 and {self.MAX_PAGE_SIZE}",
            )
        fingerprint = f"{account}|{query or ''}|{label or ''}"
        offset = _decode_cursor(page_token, fingerprint) if page_token else 0

        # Every word has to match somewhere in the message, the way mail search
        # usually behaves: "paylane invoice" finds the PayLane invoice.
        terms = (query or "").lower().split()

        def searchable(message: Message) -> str:
            return " ".join(
                (message.subject, message.body_text, message.sender.email, message.sender.name or "")
            ).lower()

        matches = [
            message
            for message in self._mailbox(account).values()
            if (label in message.labels if label else TRASH not in message.labels)
            and all(term in searchable(message) for term in terms)
        ]
        matches.sort(key=_sort_key, reverse=True)
        window = matches[offset : offset + size]
        next_offset = offset + len(window)
        next_token = _encode_cursor(next_offset, fingerprint) if next_offset < len(matches) else None
        return Page(messages=window, next_page_token=next_token)

    def get(self, account: str, message_id: str, *, peek: bool = False) -> Message:
        mailbox = self._mailbox(account)
        try:
            message = mailbox[message_id]
        except KeyError:
            raise ApiError(404, "not_found", f"No message with id {message_id}") from None
        if not peek and UNREAD in message.labels:
            # The IMAP BODY[] behaviour: fetching the body marks it seen.
            message.labels = [label for label in message.labels if label != UNREAD]
        return message

    def labels(self, account: str) -> list[dict[str, object]]:
        mailbox = self._mailbox(account)
        counts: dict[str, int] = {}
        for message in mailbox.values():
            for label in message.labels:
                counts[label] = counts.get(label, 0) + 1
        names = sorted({*counts, INBOX, UNREAD, TRASH, SENT, *self._user_labels.get(account, [])})
        return [{"name": name, "message_count": counts.get(name, 0)} for name in names]

    # -------------------------------------------------------------- mutations
    def send(self, account: str, *, to: list[str], subject: str, body: str) -> Message:
        if not to:
            raise ApiError(400, "invalid_argument", "to must contain at least one recipient")
        self._sent_counter += 1
        message = Message.model_validate(
            {
                "id": f"msg_sent_{self._sent_counter:04d}",
                "thread_id": f"thr_sent_{self._sent_counter:04d}",
                "account": account,
                "from": {"name": self._display_names.get(account), "email": account},
                "to": [Address(email=address).model_dump(exclude_none=True) for address in to],
                "cc": [],
                "subject": subject,
                "snippet": " ".join(body.split())[:120],
                "body_text": body,
                "received_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
                "labels": [SENT],
                "attachments": [],
            }
        )
        self._mailbox(account)[message.id] = message
        return message

    def trash(self, account: str, message_id: str) -> Message:
        message = self.get(account, message_id, peek=True)
        message.labels = [label for label in message.labels if label not in (INBOX, UNREAD)]
        if TRASH not in message.labels:
            message.labels.append(TRASH)
        return message

    def add_label(self, account: str, message_id: str, label: str) -> Message:
        if not label or label in (TRASH, SENT):
            raise ApiError(400, "invalid_argument", f"{label!r} cannot be applied directly")
        message = self.get(account, message_id, peek=True)
        if label not in message.labels:
            message.labels.append(label)
        known = (INBOX, UNREAD, STARRED, TRASH, SENT)
        if label not in self._user_labels.setdefault(account, []) and label not in known:
            self._user_labels[account].append(label)
        return message

    # ----------------------------------------------------------------- oracle
    def facts(self, account: str | None = None) -> set[str]:
        """Mailbox state as a flat set of facts, for side-effect diffing."""
        accounts = [account] if account else list(self._accounts)
        facts: set[str] = set()
        for name in accounts:
            for message in self._accounts.get(name, {}).values():
                facts.add(f"{name}|message:{message.id}")
                for label in message.labels:
                    facts.add(f"{name}|message:{message.id}|label:{label}")
        return facts
