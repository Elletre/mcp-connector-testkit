"""Running the checks and collecting what happened.

Sessions are shared by default, because spawning a server per assertion is how
a fast suite becomes a slow one; checks that need a connection to themselves say
so with `isolation="fresh"`.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from ..protocol import LATEST_HANDSHAKE, LATEST_STATELESS, Era
from ..session import Session, probe_era
from ..target import Target
from .model import Check, Ctx, Outcome, Profile, Severity, Status, selected, transport_of


@dataclass(frozen=True)
class CheckRun:
    check: Check
    era: Era
    transport: str
    outcome: Outcome
    elapsed_s: float

    @property
    def status(self) -> Status:
        return self.outcome.status

    @property
    def severity(self) -> Severity:
        return self.check.severity

    @property
    def counts_as_failure(self) -> bool:
        """A failed check at MUST severity. Warnings and notes are reported, not failed."""
        return self.status == "failed" and self.severity == "error"

    def label(self) -> str:
        return f"{self.check.id}[{self.era}/{self.transport}]"


@dataclass
class Report:
    target: str
    runs: list[CheckRun] = field(default_factory=list)

    def by_status(self, status: Status) -> list[CheckRun]:
        return [run for run in self.runs if run.status == status]

    def failures(self, severity: Severity) -> list[CheckRun]:
        return [run for run in self.runs if run.status == "failed" and run.severity == severity]

    @property
    def errors(self) -> list[CheckRun]:
        return self.failures("error")

    @property
    def warnings(self) -> list[CheckRun]:
        return self.failures("warning")

    @property
    def notes(self) -> list[CheckRun]:
        return self.failures("info")

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (
            f"{len(self.by_status('passed'))} passed · {len(self.errors)} failed · "
            f"{len(self.warnings)} warnings · {len(self.notes)} notes · "
            f"{len(self.by_status('skipped'))} skipped"
        )


def supported_eras(target: Target) -> tuple[Era, ...]:
    """Which eras this server answers, established the way a client would."""
    detected = probe_era(target)
    eras: list[Era] = [detected]
    other: Era = "handshake" if detected == "stateless" else "stateless"
    version = LATEST_HANDSHAKE if other == "handshake" else LATEST_STATELESS
    session = Session(target=target, protocol_version=version)
    try:
        session.wire.start()
        if other == "handshake":
            works = session.initialize().result is not None
        else:
            works = session.call("tools/list").result is not None
        if works:
            eras.append(other)
    except Exception:
        pass
    finally:
        session.close()
    return tuple(eras)


def run_checks(
    target: Target,
    profile: Profile | None = None,
    *,
    eras: Iterable[Era] = ("stateless",),
    ids: tuple[str, ...] | None = None,
    layers: tuple[int, ...] | None = None,
    on_result: Callable[[CheckRun], None] | None = None,
    read_timeout_s: float = 15.0,
) -> Report:
    profile = profile or Profile()
    transport = transport_of(target)
    report = Report(target=target.describe())
    checks = selected(ids=ids, layers=layers)

    for era in eras:
        applicable = [check for check in checks if check.applies_to(era, transport)]
        if not applicable:
            continue
        version = LATEST_STATELESS if era == "stateless" else LATEST_HANDSHAKE
        shared: Session | None = None
        try:
            for check in applicable:
                missing = check.missing(profile)
                if missing is not None:
                    run = CheckRun(
                        check=check,
                        era=era,
                        transport=transport,
                        outcome=Outcome(status="skipped", detail=missing),
                        elapsed_s=0.0,
                    )
                    report.runs.append(run)
                    if on_result:
                        on_result(run)
                    continue

                try:
                    if check.isolation == "fresh":
                        session = Session(
                            target=target, protocol_version=version, read_timeout_s=read_timeout_s
                        ).open()
                    else:
                        if shared is not None and not shared.alive:
                            # The server died under an earlier check (which will
                            # have failed for it); later checks get a fresh one.
                            shared.close()
                            shared = None
                        if shared is None:
                            shared = Session(
                                target=target,
                                protocol_version=version,
                                read_timeout_s=read_timeout_s,
                            ).open()
                        session = shared
                except Exception as error:
                    run = CheckRun(
                        check=check,
                        era=era,
                        transport=transport,
                        outcome=Outcome(
                            status="failed",
                            detail=(f"could not open a {era} session: {type(error).__name__}: {error}"),
                        ),
                        elapsed_s=0.0,
                    )
                    report.runs.append(run)
                    if on_result:
                        on_result(run)
                    continue

                ctx = Ctx(
                    session=session,
                    target=target,
                    profile=profile,
                    era=era,
                    transport=transport,
                )
                started = time.monotonic()
                try:
                    outcome = check.run(ctx)
                except Exception as error:
                    outcome = Outcome(
                        status="failed",
                        detail=f"the check itself raised {type(error).__name__}: {error}",
                        evidence=traceback.format_exc(limit=4),
                    )
                elapsed = time.monotonic() - started
                if check.isolation == "fresh":
                    session.close()

                run = CheckRun(
                    check=check,
                    era=era,
                    transport=transport,
                    outcome=outcome,
                    elapsed_s=elapsed,
                )
                report.runs.append(run)
                if on_result:
                    on_result(run)
        finally:
            if shared is not None:
                shared.close()

    return report
