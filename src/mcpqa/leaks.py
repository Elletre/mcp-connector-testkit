"""Looking for credentials in everything the server said.

Connectors leak tokens in unglamorous ways: an error message that includes the
request it failed on, a debug log that prints headers, a stack trace that made
it into a tool result. None of those are found by a test that only looks at the
field it expected — so this looks at every byte that came back, on every test,
and it looks for the obvious encodings too.
"""

from __future__ import annotations

import base64
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass

MIN_PREFIX = 8
"""A credential's first characters are enough to be a leak; many are prefixed."""


@dataclass(frozen=True)
class Leak:
    secret: str
    encoding: str
    excerpt: str

    def describe(self) -> str:
        preview = f"{self.secret[:4]}…{self.secret[-2:]}" if len(self.secret) > 8 else self.secret
        return f"{preview} ({self.encoding}) in: …{self.excerpt}…"


def _encodings(secret: str) -> list[tuple[str, str]]:
    forms = [("plain", secret)]
    quoted = urllib.parse.quote(secret, safe="")
    if quoted != secret:
        forms.append(("url-encoded", quoted))
    forms.append(("base64", base64.b64encode(secret.encode()).decode().rstrip("=")))
    if len(secret) > MIN_PREFIX:
        forms.append(("prefix", secret[:MIN_PREFIX]))
    return forms


def scan(texts: Iterable[str], secrets: Iterable[str], *, context: int = 40) -> list[Leak]:
    """Every secret found in any of the texts, with a little context around it."""
    found: list[Leak] = []
    corpus = [text for text in texts if text]
    for secret in {s for s in secrets if s}:
        for encoding, needle in _encodings(secret):
            for text in corpus:
                index = text.find(needle)
                if index == -1:
                    continue
                start = max(0, index - context)
                end = min(len(text), index + len(needle) + context)
                found.append(
                    Leak(secret=secret, encoding=encoding, excerpt=text[start:end].replace("\n", " "))
                )
                break  # one report per secret and encoding is enough
    return found
