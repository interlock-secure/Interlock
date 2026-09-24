"""Ordering the queue, and measuring whether it was worth doing.

The claim this module has to support is the product's central one: ranking by
expected recoverable value beats working cases in the order they arrived.

How it is measured
------------------
Not by accuracy. By **recovered dollars per analyst-hour under a fixed
capacity constraint**, which is the only figure that corresponds to a decision
anyone makes.

The evaluation gives each policy the same queue and the same capacity, lets it
choose, and adds up what it recovered. Four policies:

    arrival      - what institutions do today. The baseline that matters.
    amount       - largest first. The obvious heuristic, and a real competitor:
                   it needs no model and it is not stupid.
    model        - expected value, probability times amount.
    oracle       - perfect knowledge. The ceiling, so the gap between model
                   and oracle is visible rather than implied.

Capacity is swept rather than chosen. The whole value argument rests on
scarcity, so a single flattering capacity would be cherry-picking; the sweep
shows where ranking helps and where it stops mattering because there is enough
capacity to work everything.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from interlock.generator.recall_cases import GeneratedCase

DEFAULT_CAPACITY_SWEEP: tuple[int, ...] = (5, 10, 20, 40, 60, 100)
"""Cases an analyst can work per hour, swept.

The low end is where ranking should matter most and the high end is where it
should stop mattering at all. A policy that only wins at one capacity has not
demonstrated anything.
"""


@dataclass(frozen=True, slots=True)
class PolicyResult:
    """What one policy achieved at one capacity."""

    policy: str
    capacity: int
    cases_worked: int
    recovered_cents: int
    recovered_count: int

    @property
    def recovered_dollars(self) -> float:
        return self.recovered_cents / 100.0

    @property
    def hit_rate(self) -> float:
        return self.recovered_count / self.cases_worked if self.cases_worked else 0.0


def _score_arrival(cases: Sequence[GeneratedCase], _p: np.ndarray) -> np.ndarray:
    """Earliest arrival first. Negated so a higher score still sorts first."""
    return np.array([-c.received_at.timestamp() for c in cases])


def _score_amount(cases: Sequence[GeneratedCase], _p: np.ndarray) -> np.ndarray:
    return np.array([float(c.amount_cents) for c in cases])


def _score_model(cases: Sequence[GeneratedCase], probabilities: np.ndarray) -> np.ndarray:
    """Expected recoverable value.

    Probability times amount, which is why calibration matters: an
    overconfident model on a large case outranks a certain one on a small
    case, and gets an analyst sent to the wrong desk.
    """
    return probabilities * np.array([float(c.amount_cents) for c in cases])


def _score_oracle(cases: Sequence[GeneratedCase], _p: np.ndarray) -> np.ndarray:
    """Perfect knowledge of what will actually be recovered. Not achievable -
    it is the ceiling, and the gap to it is the honest measure of headroom."""
    return np.array([float(c.recovered_cents) for c in cases])


POLICIES: dict[str, Callable[[Sequence[GeneratedCase], np.ndarray], np.ndarray]] = {
    "arrival": _score_arrival,
    "amount": _score_amount,
    "model": _score_model,
    "oracle": _score_oracle,
}


def run_policy(
    cases: Sequence[GeneratedCase],
    probabilities: np.ndarray,
    *,
    policy: str,
    capacity: int,
) -> PolicyResult:
    """Work ``capacity`` cases under one policy and total what came back.

    The simplification worth stating: this treats the queue as static - every
    case present, the analyst picks the best ``capacity`` of them. A real
    queue arrives over time and a case not worked in hour one is still there,
    older and worth less, in hour two. Modelling that would strengthen the
    result rather than weaken it, since ranking compounds, so the static form
    is the conservative choice.
    """
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}; expected one of {sorted(POLICIES)}")

    scores = POLICIES[policy](cases, probabilities)
    # Stable sort on descending score, ties broken by arrival - so two cases
    # with identical scores behave predictably rather than by list order.
    order = np.lexsort((np.array([c.received_at.timestamp() for c in cases]), -scores))
    chosen = [cases[i] for i in order[:capacity]]

    return PolicyResult(
        policy=policy,
        capacity=capacity,
        cases_worked=len(chosen),
        recovered_cents=sum(c.recovered_cents for c in chosen),
        recovered_count=sum(1 for c in chosen if c.recovered),
    )


def sweep(
    cases: Sequence[GeneratedCase],
    probabilities: np.ndarray,
    *,
    capacities: Sequence[int] = DEFAULT_CAPACITY_SWEEP,
) -> list[PolicyResult]:
    """Every policy at every capacity."""
    return [
        run_policy(cases, probabilities, policy=policy, capacity=capacity)
        for capacity in capacities
        for policy in POLICIES
    ]


def uplift_against_arrival(results: Sequence[PolicyResult]) -> dict[int, dict[str, float]]:
    """How much better each policy did than arrival order, by capacity.

    Expressed as a multiple rather than a percentage. At low capacity the
    multiples are large and the absolute figures small; both matter, and a
    multiple makes the shape of the curve legible across capacities that
    differ twentyfold.
    """
    by_capacity: dict[int, dict[str, float]] = {}

    for capacity in sorted({r.capacity for r in results}):
        at_capacity = {r.policy: r for r in results if r.capacity == capacity}
        baseline = at_capacity.get("arrival")
        if baseline is None or baseline.recovered_cents == 0:
            # Arrival order recovering nothing is a real outcome at small
            # capacities. A ratio would be infinite, so report the absolute
            # dollars instead and let the reader see why.
            by_capacity[capacity] = {
                policy: result.recovered_dollars for policy, result in at_capacity.items()
            }
            continue
        by_capacity[capacity] = {
            policy: result.recovered_cents / baseline.recovered_cents
            for policy, result in at_capacity.items()
        }

    return by_capacity
