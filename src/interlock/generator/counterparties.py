"""The institutions on the other end, and how differently they behave.

A generator where every counterparty answers alike produces a dataset where
counterparty identity carries no information, and a triage model trained on it
would rank a request to a bank that answers in twenty minutes the same as one
to a bank that takes nine days. Real books are not like that, and the
difference is one of the few things an analyst can actually act on.

This is the same lesson as M2's ``inbound_per_month`` defect: a feature that is
declared but not varied is a feature that teaches nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

COUNTERPARTY_COUNT = 40
"""[ASSUMPTION] How many institutions the testbed models.

Enough for per-institution statistics to mean something - roughly 25 cases
each at the volumes the generator produces - without making the fixtures
unwieldy. Below about 20 the per-counterparty medians are noise; above about
100 nothing new appears.
"""

RESPONSE_HOURS_LOGNORMAL = (2.4, 1.1)
"""[ASSUMPTION] Median response around 11 hours, with a long tail to several days.

Log-normal because response times cannot be negative and the tail is what
matters: the institutions that take nine days are the reason a deadline exists
at all. No published distribution of interbank response times was found, which
is itself a finding - the Federal Reserve's 2025 request for information asked
supervised institutions about exactly this and reported only complaints.
"""

RETURN_WILLINGNESS_BETA = (2.2, 3.0)
"""[ASSUMPTION] Share of requests an institution returns funds on.

Centred near 0.42 and skewed low. Returning is discretionary on three of four
rails, and an institution that returns on most requests is unusual.
"""


@dataclass(frozen=True, slots=True)
class Counterparty:
    """One institution, with its own habits."""

    institution_id: str
    display_name: str

    median_response_hours: float
    response_spread: float

    return_willingness: float
    """Probability it returns funds when it can."""

    responsiveness: float
    """How effectively it freezes what remains. Distinct from willingness: an
    institution can be eager to help and slow to act, or the reverse."""

    prefers_structured: bool
    """Whether it sends a rail message or an email. Smaller institutions often
    have the rail connection and not the operations team behind it."""

    answers_within_window: float
    """Share of requests it answers before the deadline, whatever the outcome.
    The Nacha obligation is to answer, not to comply, so this is a separate
    behaviour from return_willingness and the two do not move together."""


_NAME_STEMS = (
    "Northbay",
    "Cedar",
    "Harbor",
    "Meridian",
    "Lakeshore",
    "Pinnacle",
    "Granite",
    "Cascade",
    "Ironwood",
    "Bluffton",
    "Fairmont",
    "Sandpiper",
    "Redstone",
    "Waverly",
    "Kestrel",
    "Thornbury",
    "Alderwood",
    "Brightwater",
    "Copperfield",
    "Dunmore",
)
_NAME_SUFFIXES = ("Credit Union", "National Bank", "Savings", "Trust", "Community Bank")


def build_counterparties(
    rng: np.random.Generator, *, count: int = COUNTERPARTY_COUNT
) -> list[Counterparty]:
    """Draw a population of counterparty institutions.

    Each draws its own parameters. Two institutions of the same size do not
    share a playbook, and the point of this module is that the model can learn
    that without being told which is which.
    """
    counterparties: list[Counterparty] = []

    for index in range(count):
        stem = _NAME_STEMS[index % len(_NAME_STEMS)]
        suffix = _NAME_SUFFIXES[(index // len(_NAME_STEMS)) % len(_NAME_SUFFIXES)]

        median_hours = float(rng.lognormal(*RESPONSE_HOURS_LOGNORMAL))

        counterparties.append(
            Counterparty(
                institution_id=f"inst-{stem.lower()}-{index:02d}",
                display_name=f"{stem} {suffix}",
                median_response_hours=median_hours,
                response_spread=float(rng.uniform(0.4, 1.2)),
                return_willingness=float(rng.beta(*RETURN_WILLINGNESS_BETA)),
                responsiveness=float(rng.beta(2.0, 2.0)),
                prefers_structured=bool(rng.random() < 0.62),
                # Loosely tied to speed: a fast institution usually answers in
                # time. Loosely, because a slow one with a good queue still
                # makes its deadline, and that combination has to exist in the
                # data or the model will treat speed as destiny.
                answers_within_window=float(
                    np.clip(rng.beta(6.0, 1.6) - 0.10 * np.log1p(median_hours / 24.0), 0.05, 1.0)
                ),
            )
        )

    return counterparties


def response_delay_hours(rng: np.random.Generator, counterparty: Counterparty) -> float:
    """How long this institution takes to answer one particular request."""
    return float(
        rng.lognormal(
            np.log(max(counterparty.median_response_hours, 0.1)), counterparty.response_spread
        )
    )
