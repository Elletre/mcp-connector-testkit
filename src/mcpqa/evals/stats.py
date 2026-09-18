"""The statistics the report needs, and no more.

Two principles. Intervals rather than bare percentages, because thirty cases
is a small sample and a report that says "87%" without saying "somewhere
between 70 and 95" is overstating what it knows. And the case, not the trial,
as the unit of evidence — for intervals and for comparisons alike — because
three repeats of the same case are not three independent observations.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Interval:
    estimate: float
    low: float
    high: float

    def describe(self) -> str:
        return f"{self.estimate:.0%} [{self.low:.0%}–{self.high:.0%}]"


def wilson(successes: float, total: int, z: float = 1.96) -> Interval:
    """Wilson score interval: honest at small n and near 0 or 1, unlike the normal approximation."""
    if total == 0:
        return Interval(0.0, 0.0, 1.0)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return Interval(p, max(0.0, centre - margin), min(1.0, centre + margin))


def pass_hat_k(results: Mapping[str, Sequence[bool]]) -> float:
    """Share of cases that passed on *every* repeat.

    From τ-bench (Yao et al., 2024): a measure of reliability rather than
    capability. An agent that gets a case right two times out of three is not
    an agent you can hand that case to.
    """
    if not results:
        return 0.0
    return sum(1 for outcomes in results.values() if outcomes and all(outcomes)) / len(results)


def by_case(trials: Iterable[tuple[str, bool]]) -> dict[str, list[bool]]:
    grouped: dict[str, list[bool]] = defaultdict(list)
    for case_id, passed in trials:
        grouped[case_id].append(passed)
    return dict(grouped)


def case_rates(results: Mapping[str, Sequence[bool]]) -> dict[str, float]:
    return {case_id: sum(outcomes) / len(outcomes) for case_id, outcomes in results.items() if outcomes}


def case_level(results: Mapping[str, Sequence[bool]], z: float = 1.96) -> Interval:
    """A rate whose interval counts cases, not trials.

    Repeats of one case are not independent — at temperature 0 they are usually
    identical — so a Wilson interval over trials claims a precision the run does
    not have. This one averages each case's own rate and gives the interval the
    number of cases as its sample size. A case's rate varies at most as much as
    a single yes/no answer would, so the interval has the right width when
    every case passes all of its repeats or none, and is wider than it needs to
    be when repeats disagree.
    """
    rates = case_rates(results)
    if not rates:
        return Interval(0.0, 0.0, 1.0)
    return wilson(sum(rates.values()), len(rates), z)


def repeat_agreement(results: Mapping[str, Sequence[bool]]) -> float:
    """Share of cases whose repeats all came out the same way."""
    if not results:
        return 0.0
    return sum(1 for outcomes in results.values() if len(set(outcomes)) <= 1) / len(results)


def bootstrap_delta(
    baseline: Mapping[str, Sequence[bool]],
    candidate: Mapping[str, Sequence[bool]],
    *,
    iterations: int = 4000,
    seed: int = 20260917,
    confidence: float = 0.95,
) -> Interval:
    """Candidate minus baseline pass rate, resampling cases rather than trials.

    Only cases present in both runs are compared, so the delta is about the
    change under test and not about a difference in what was asked.
    """
    shared = sorted(set(baseline) & set(candidate))
    if not shared:
        return Interval(0.0, 0.0, 0.0)
    base = case_rates(baseline)
    cand = case_rates(candidate)
    deltas = [cand[case] - base[case] for case in shared]
    observed = sum(deltas) / len(deltas)

    rng = random.Random(seed)
    samples = sorted(
        sum(deltas[rng.randrange(len(deltas))] for _ in deltas) / len(deltas) for _ in range(iterations)
    )
    tail = (1 - confidence) / 2
    low = samples[int(tail * iterations)]
    high = samples[min(iterations - 1, int((1 - tail) * iterations))]
    return Interval(observed, low, high)
