"""The shape of a check, and the registry of them.

Three rules hold for every check in this kit:

1. It cites the specification verbatim, with a link. A conformance tool that
   cannot show you the sentence it is enforcing is just an opinion with a
   red X next to it.
2. Its severity comes from the specification's own keyword — MUST is an error,
   SHOULD is a warning, guidance without a keyword is informational. That is
   why a clean run can still print warnings, and why a warning is not a bug
   report.
3. It is itself tested, against a fixture server that violates it and one that
   does not. A check that never fires is worse than no check at all.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from ..protocol import Era
from ..session import Session
from ..target import HttpTarget, StdioTarget, Target

Severity = Literal["error", "warning", "info"]
Status = Literal["passed", "failed", "skipped"]
Transport = Literal["stdio", "http"]
Isolation = Literal["shared", "fresh"]

SPEC_BASE = "https://modelcontextprotocol.io/specification"


@dataclass(frozen=True)
class SpecRef:
    """Where a requirement comes from, quoted rather than paraphrased.

    `quote` is the specification's own wording, with `[...]` marking an elision.
    `tests/unit/test_spec_quotes.py` checks every quote against a vendored copy
    of the page it points to, so a citation cannot drift into a paraphrase.
    `quote` is `None` only for checks that encode engineering practice rather
    than a sentence of the specification; those say why in `Check.escalation`.
    """

    version: str
    section: str
    quote: str | None
    anchor: str | None = None

    @property
    def url(self) -> str:
        url = f"{SPEC_BASE}/{self.version}"
        if self.section:
            url = f"{url}/{self.section}"
        return f"{url}#{self.anchor}" if self.anchor else url

    @property
    def keyword(self) -> str | None:
        """The strongest RFC 2119 keyword in the quote, if any."""
        if self.quote is None:
            return None
        for keyword in ("MUST NOT", "MUST", "REQUIRED", "SHOULD NOT", "SHOULD", "RECOMMENDED"):
            if keyword in self.quote:
                return keyword
        return None

    def describe(self) -> str:
        if self.quote is None:
            return f"{self.version} {self.section}: (engineering practice, no normative sentence)"
        return f'{self.version} {self.section}: "{self.quote}"'


@dataclass(frozen=True)
class Outcome:
    status: Status
    detail: str = ""
    evidence: str = ""


def passed(detail: str = "") -> Outcome:
    return Outcome(status="passed", detail=detail)


def failed(detail: str, evidence: str = "") -> Outcome:
    return Outcome(status="failed", detail=detail, evidence=evidence)


def skipped(reason: str) -> Outcome:
    return Outcome(status="skipped", detail=reason)


@dataclass
class Profile:
    """What the kit knows about this server beyond what the wire tells it."""

    samples: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    """Tool name -> argument sets that are meant to succeed."""
    read_only_samples: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    """Argument sets safe to call repeatedly; defaults to `samples` when unset."""
    secrets: tuple[str, ...] = ()
    """Credentials that must never appear in anything the server says."""
    oracle: Any = None
    """A `SideEffectOracle`, when the caller can see the state behind the server."""
    faults: Any = None
    """A `FaultControl`, when the caller can make the upstream misbehave."""
    expects_auth: bool = False
    """The endpoint is expected to require a token (enables the 401 checks)."""
    foreign_tokens: tuple[str, ...] = ()
    """Credentials issued for something other than this server; it must refuse them."""
    allow_mutations: bool = False
    """Permission to call tools that change state. Off by default: pointing the kit at
    a production mailbox should not send mail."""

    def arguments_for(self, tool: str) -> list[dict[str, Any]]:
        return self.samples.get(tool, [])

    def safe_arguments_for(self, tool: str) -> list[dict[str, Any]]:
        return self.read_only_samples.get(tool) or self.samples.get(tool, [])


@dataclass
class Ctx:
    """Everything a check is allowed to touch."""

    session: Session
    target: Target
    profile: Profile
    era: Era
    transport: Transport
    _tools: list[dict[str, Any]] | None = field(default=None, init=False, repr=False)

    def tools(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        if self._tools is None or refresh:
            self._tools = self.session.list_tools()
        return self._tools

    def tool(self, name: str) -> dict[str, Any] | None:
        return next((tool for tool in self.tools() if tool.get("name") == name), None)

    def fresh_session(self, **overrides: Any) -> Session:
        """A second, independent connection to the same server."""
        session = Session(
            target=overrides.pop("target", self.target),
            protocol_version=overrides.pop("protocol_version", self.session.protocol_version),
        )
        return session.open(**overrides)

    def evidence(self, count: int = 2) -> str:
        return self.session.transcript.tail(count)


CheckFn = Callable[[Ctx], Outcome]


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    layer: int
    severity: Severity
    ref: SpecRef
    run: CheckFn
    eras: tuple[Era, ...] = ("handshake", "stateless")
    transports: tuple[Transport, ...] = ("stdio", "http")
    needs: tuple[str, ...] = ()
    """Capabilities the check requires: "oracle", "faults", "samples", "auth"."""
    isolation: Isolation = "shared"
    escalation: str = ""
    """Why this check is stricter than the specification's own keyword, if it is.

    Severity normally follows the quote: MUST is an error, SHOULD a warning,
    anything else a note. A check may go further only by saying why, here, where
    the report and the documentation will show it."""

    def applies_to(self, era: Era, transport: Transport) -> bool:
        return era in self.eras and transport in self.transports

    def missing(self, profile: Profile) -> str | None:
        if "oracle" in self.needs and profile.oracle is None:
            return "no side-effect oracle was provided"
        if "faults" in self.needs and profile.faults is None:
            return "no fault control was provided"
        if "samples" in self.needs and not profile.samples:
            return "no sample arguments were provided"
        if "auth" in self.needs and not profile.expects_auth:
            return "the endpoint is not expected to require authorization"
        if "foreign_tokens" in self.needs and not profile.foreign_tokens:
            return "no foreign credentials were provided to test audience validation with"
        if "mutations" in self.needs and not profile.allow_mutations:
            return "mutating calls are not allowed (pass --allow-mutations against a test account)"
        return None


REGISTRY: dict[str, Check] = {}


def register(
    *,
    id: str,
    title: str,
    layer: int,
    severity: Severity,
    ref: SpecRef | None = None,
    eras: tuple[Era, ...] = ("handshake", "stateless"),
    transports: tuple[Transport, ...] = ("stdio", "http"),
    needs: tuple[str, ...] = (),
    isolation: Isolation = "shared",
    escalation: str = "",
) -> Callable[[CheckFn], CheckFn]:
    from .citations import CITATIONS, ESCALATIONS  # the table imports this module

    citation = ref if ref is not None else CITATIONS[id]
    reason = escalation or ESCALATIONS.get(id, "")

    def decorator(fn: CheckFn) -> CheckFn:
        if id in REGISTRY:
            raise ValueError(f"duplicate check id: {id}")
        REGISTRY[id] = Check(
            id=id,
            title=title,
            layer=layer,
            severity=severity,
            ref=citation,
            run=fn,
            eras=eras,
            transports=transports,
            needs=needs,
            isolation=isolation,
            escalation=reason,
        )
        return fn

    return decorator


def all_checks() -> list[Check]:
    return sorted(REGISTRY.values(), key=lambda check: check.id)


def selected(ids: tuple[str, ...] | None = None, layers: tuple[int, ...] | None = None) -> list[Check]:
    checks = all_checks()
    if layers:
        checks = [check for check in checks if check.layer in layers]
    if ids:
        wanted = set(ids)
        checks = [
            check
            for check in checks
            if check.id in wanted or any(check.id.startswith(prefix.rstrip("*")) for prefix in wanted)
        ]
    return checks


def transport_of(target: Target) -> Transport:
    if isinstance(target, StdioTarget):
        return "stdio"
    if isinstance(target, HttpTarget):
        return "http"
    raise TypeError(f"unknown target type: {type(target)!r}")  # pragma: no cover


def iter_registry() -> Iterator[Check]:
    yield from all_checks()
