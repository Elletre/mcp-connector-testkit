"""Running the cases: fresh state, a fresh connection and a clean slate every time.

Each trial resets the world behind the server, applies the case's faults,
opens a new session, runs one episode and diffs the state afterwards. That is
slower than reusing a connection and it is the point — a trial that inherits
a warm cache or a half-trashed mailbox from the previous one is not measuring
what it claims to.

A run is also long, and the things it depends on are not always there: a local
model server restarts, a hosted API has a bad minute. A trial whose model or
connector could not be reached is retried once, then recorded as an *error* —
excluded from the pass rate rather than counted as a failure of the connector —
and the run carries on. Several errors in a row mean the model is gone; the run
stops, keeps everything it has, and can be resumed.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from ..probes import diff_states
from ..session import Session
from .adapters.base import ModelAdapter
from .agent import run_episode, tool_specs
from .dataset import Case, Fault
from .scoring import grade

RETRY_PAUSE_S = 5.0
GIVE_UP_AFTER_CONSECUTIVE_ERRORS = 4


class EvalEnvironment(Protocol):
    """What a connector's owner provides so its agent-level behaviour can be measured."""

    label: str

    def reset(self) -> None: ...

    def snapshot(self) -> frozenset[str]: ...

    def apply_faults(self, faults: tuple[Fault, ...]) -> None: ...

    def open_session(self) -> Session: ...

    def close(self) -> None: ...


@dataclass
class Trial:
    case_id: str
    category: str
    repeat: int
    passed: bool
    failures: list[str]
    protocol_failure: bool
    unsafe: bool
    tools_called: list[str]
    arguments: list[dict[str, Any]]
    answer: str
    input_tokens: int
    output_tokens: int
    requests: int
    seconds: float
    largest_tool_result: int
    malformed_turns: int
    error: str | None = None
    """Set when the trial could not be run at all (model or connector unreachable)."""
    retried_after: str | None = None
    """Set when a first attempt failed and this result comes from the second."""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def key(self) -> tuple[str, int]:
        return (self.case_id, self.repeat)


@dataclass
class RunResult:
    model: str
    environment: str
    repeats: int
    started_at: str
    trials: list[Trial] = field(default_factory=list)
    duration_s: float = 0.0
    stopped_early: str | None = None

    @property
    def graded(self) -> list[Trial]:
        """Trials that ran; errored ones say nothing about the connector."""
        return [trial for trial in self.trials if trial.error is None]


def _errored(case: Case, repeat: int, error: BaseException) -> Trial:
    return Trial(
        case_id=case.id,
        category=case.category,
        repeat=repeat,
        passed=False,
        failures=[],
        protocol_failure=False,
        unsafe=False,
        tools_called=[],
        arguments=[],
        answer="",
        input_tokens=0,
        output_tokens=0,
        requests=0,
        seconds=0.0,
        largest_tool_result=0,
        malformed_turns=0,
        error=f"{type(error).__name__}: {error}"[:300],
    )


def _run_one(
    case: Case,
    repeat: int,
    environment: EvalEnvironment,
    make_adapter: Callable[[int], ModelAdapter],
    max_steps: int,
) -> Trial:
    environment.reset()
    environment.apply_faults(case.faults)
    before = environment.snapshot()
    session = environment.open_session()
    try:
        adapter = make_adapter(repeat)
        episode = run_episode(session, adapter, case.prompt, tools=tool_specs(session), max_steps=max_steps)
    finally:
        session.close()
        environment.apply_faults(())
    verdict = grade(case, episode, diff_states(before, environment.snapshot()))
    return Trial(
        case_id=case.id,
        category=case.category,
        repeat=repeat,
        passed=verdict.passed,
        failures=verdict.failures,
        protocol_failure=verdict.protocol_failure,
        unsafe=verdict.unsafe,
        tools_called=episode.tools_called,
        arguments=[step.call.arguments for step in episode.steps],
        answer=episode.answer,
        input_tokens=episode.usage.input_tokens,
        output_tokens=episode.usage.output_tokens,
        requests=episode.usage.requests,
        seconds=round(episode.elapsed_s, 2),
        largest_tool_result=episode.largest_tool_result,
        malformed_turns=episode.malformed_turns,
    )


def run(
    cases: Iterable[Case],
    environment: EvalEnvironment,
    make_adapter: Callable[[int], ModelAdapter],
    *,
    repeats: int = 3,
    max_steps: int = 6,
    on_trial: Callable[[Trial], None] | None = None,
    prior: Iterable[Trial] = (),
    retry_pause_s: float = RETRY_PAUSE_S,
) -> RunResult:
    """Run every case `repeats` times, skipping any (case, repeat) already in `prior`."""
    selected = list(cases)
    result = RunResult(
        model=make_adapter(1).name,
        environment=environment.label,
        repeats=repeats,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    done = {trial.key: trial for trial in prior if trial.error is None}
    result.trials.extend(done.values())
    consecutive_errors = 0
    started = time.monotonic()
    try:
        for repeat in range(1, repeats + 1):
            for case in selected:
                if (case.id, repeat) in done:
                    continue
                trial: Trial | None = None
                first_failure: str | None = None
                for attempt in (1, 2):
                    try:
                        trial = _run_one(case, repeat, environment, make_adapter, max_steps)
                        trial.retried_after = first_failure
                        break
                    except Exception as error:
                        if attempt == 2:
                            trial = _errored(case, repeat, error)
                        else:
                            first_failure = f"{type(error).__name__}: {error}"[:300]
                            time.sleep(retry_pause_s)
                assert trial is not None
                result.trials.append(trial)
                if on_trial:
                    on_trial(trial)
                consecutive_errors = consecutive_errors + 1 if trial.error else 0
                if consecutive_errors >= GIVE_UP_AFTER_CONSECUTIVE_ERRORS:
                    result.stopped_early = (
                        f"{consecutive_errors} trials in a row could not run; last error: {trial.error}"
                    )
                    return result
    finally:
        result.duration_s = time.monotonic() - started
        environment.close()
    return result
