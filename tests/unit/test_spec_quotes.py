"""Every citation is the specification's own words, and every severity is earned.

Two failure modes this guards against, both easy to fall into while writing a
conformance tool: a "quote" that is really a paraphrase — which then drifts
into something the specification never said — and a check that is marked as
an error while the sentence behind it only says SHOULD.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from mcpqa.checks import Check, all_checks

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "tests/spec_sources"
SCHEMAS = ROOT / "src/mcpqa/schemas"

MUST = {"MUST", "MUST NOT", "REQUIRED"}
SHOULD = {"SHOULD", "SHOULD NOT", "RECOMMENDED"}


def normalize(text: str) -> str:
    """Reduce markdown to the words a reader sees."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # [text](url)
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)  # [text][ref]
    text = text.replace("**", "").replace("`", "").replace("\\_", "_")
    text = re.sub(r"(?m)^\s*[-*]\s+", "", text)  # bullet markers
    return re.sub(r"\s+", " ", text).strip()


def _descriptions(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("description"), str):
            found.append(node["description"])
        for value in node.values():
            found.extend(_descriptions(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_descriptions(value))
    return found


def source_for(check: Check) -> str:
    ref = check.ref
    if ref.section == "schema":
        schema = json.loads((SCHEMAS / f"{ref.version}.json").read_text(encoding="utf-8"))
        return normalize("\n".join(_descriptions(schema)))
    base = SOURCES / ref.version
    for candidate in (base / f"{ref.section}.mdx", base / ref.section / "index.mdx", base / "index.mdx"):
        if ref.section == "" and candidate.name != "index.mdx":
            continue
        if candidate.exists() and candidate.is_file():
            return normalize(candidate.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"no vendored page for {ref.version}/{ref.section}")


QUOTED = [check for check in all_checks() if check.ref.quote is not None]


@pytest.mark.parametrize("check", QUOTED, ids=[check.id for check in QUOTED])
def test_the_quote_is_word_for_word(check: Check) -> None:
    source = source_for(check)
    position = 0
    assert check.ref.quote is not None
    for fragment in check.ref.quote.split("[...]"):
        wanted = normalize(fragment)
        if not wanted:
            continue
        found = source.find(wanted, position)
        assert found != -1, (
            f"{check.id} quotes {wanted!r}, which does not appear (in order) in "
            f"{check.ref.version}/{check.ref.section or 'index'}"
        )
        position = found + len(wanted)


@pytest.mark.parametrize("check", all_checks(), ids=[check.id for check in all_checks()])
def test_severity_follows_the_keyword_unless_a_reason_is_given(check: Check) -> None:
    keyword = check.ref.keyword
    if keyword in MUST:
        allowed = {"error"}
    elif keyword in SHOULD:
        allowed = {"warning"}
    else:
        allowed = {"info"}
    if check.severity in allowed:
        return
    assert check.escalation, (
        f"{check.id} is a {check.severity} but its citation says {keyword or 'nothing normative'}; "
        "either cite the sentence that makes it one, or write down why it is stricter"
    )
    rank = {"info": 0, "warning": 1, "error": 2}
    assert rank[check.severity] > max(rank[level] for level in allowed), (
        f"{check.id} is weaker than its own citation ({check.severity} for a {keyword}); "
        "downgrading a MUST needs no escalation, it needs a different citation"
    )


def test_a_check_without_a_quote_explains_itself() -> None:
    for check in all_checks():
        if check.ref.quote is None:
            assert len(check.escalation) > 40, f"{check.id} has neither a quote nor a reason"
            assert check.severity != "error", f"{check.id}: practice alone does not make an error"
