"""Per-account behaviour sampling and the temporal model.

Two problems this module exists to solve, both found by measuring the data
rather than by reading it.

First, homogeneity. An earlier version gave every account of a type the same
behavioural constants, so every classic mule swept its funds within an
eighteen-minute band while a small business ranged across ten days. A single
threshold on sweep delay separated them perfectly. Real institutions do not
share a parameter file: two mule operators run different playbooks, and two
small businesses bank differently. Each account now draws its own parameters
around the archetype, so the archetype is a centre of mass rather than a
template.

Second, mules came in exactly two flavours. A detector facing two discrete
patterns learns two discrete patterns. Mule behaviour here is now governed by
a continuous sophistication score, so tenure, pacing and sweep delay vary
along a spectrum with no clean boundary anywhere on it. The classic and
evasive labels survive only for reporting, derived from the score after the
fact - the data itself has no such split.

Third, time was uniform: flat across the week, flat across the day, payments
at four in the morning as likely as at noon. Timestamps now follow a weekly
cycle, a per-account daily rhythm, and a payday effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from interlock.generator.config import BEHAVIOUR, AccountType

# ---------------------------------------------------------------------------
# Per-account behaviour
# ---------------------------------------------------------------------------

BEHAVIOUR_VARIATION_CV = 0.45
"""[ASSUMPTION] Coefficient of variation applied to per-account rates.

Wide on purpose. At a low value the population still looks like a template
with noise; at this level two accounts of the same archetype can differ by
severalfold, which is what a real book looks like.
"""


@dataclass(frozen=True, slots=True)
class AccountBehaviour:
    """One account's own parameters, drawn around its archetype."""

    outbound_per_month: float
    inbound_weight: float
    distinct_payees: int
    first_time_payee_rate: float

    sweeps_inbound: bool
    sweep_delay_hours: float
    sweep_share: float

    peak_hour: int
    """Hour of day this account transacts around. A night-shift worker and a
    retiree do not share one."""

    hour_spread: float
    """How tightly activity clusters around peak_hour."""

    weekend_activity: float
    """Multiplier on weekend traffic. A business drops to near zero; a
    personal account barely changes."""

    burstiness: float
    """0 spreads payments evenly across the window; 1 clusters them into a
    few active stretches with quiet gaps between."""

    sophistication: float = 0.0
    """Mules only. 0 is a crude operator burning a fresh account in days; 1 is
    a patient one using an aged account and moving funds slowly."""


def _gamma_around(
    rng: np.random.Generator, mean: float, cv: float = BEHAVIOUR_VARIATION_CV
) -> float:
    """Draw a positive value with the given mean and spread.

    Gamma rather than normal because these are rates: they cannot go negative,
    and their real distributions are right-skewed - a few accounts are far
    busier than typical, none are busier than zero in the other direction.
    """
    if mean <= 0:
        return 0.0
    shape = 1.0 / (cv**2)
    return float(rng.gamma(shape, mean / shape))


def _beta_around(rng: np.random.Generator, mean: float, concentration: float = 8.0) -> float:
    """Draw a proportion centred on mean, bounded to [0, 1]."""
    mean = min(max(mean, 0.01), 0.99)
    return float(rng.beta(mean * concentration, (1 - mean) * concentration))


def interpolate_log(low: float, high: float, position: float) -> float:
    """Interpolate on a log scale.

    Used for sweep delay and tenure, which span orders of magnitude. Linear
    interpolation between three minutes and four days would put almost every
    draw in the upper range.
    """
    return float(np.exp(np.log(low) + (np.log(high) - np.log(low)) * position))


MULE_SWEEP_DELAY_HOURS = (0.05, 96.0)
"""[ASSUMPTION] Sweep delay at sophistication 0 and 1: three minutes to four
days. The lower end is an operator emptying an account before anyone can
react; the upper is one who has noticed that instant sweeps are the easiest
thing in the world to alert on."""

MULE_TENURE_DAYS = (2, 900)
"""[ASSUMPTION] Account age at sophistication 0 and 1. A continuum rather
than the two discrete bands an earlier version used, so no tenure threshold
sits at a natural boundary."""

AUTO_SWEEP_SHARE_OF_BUSINESSES = 0.18
"""[ASSUMPTION] Share of business accounts on an automatic sweep to a
concentration account.

A standard treasury product: funds arriving in an operating account are moved
to a central account the same hour, sometimes within minutes. It is also
indistinguishable from laundering by timing alone.

Without this the fastest legitimate sweeper in the population held funds for
eight hours, so any mule moving faster was uniquely identifiable and a single
threshold on sweep delay scored an F1 of 0.92. Businesses that sweep on a
timer are the reason that threshold does not work in production.
"""

MULE_CAMPAIGN_DAYS = (2.0, 45.0)
"""[ASSUMPTION] How long an operator runs one account. Crude operators burn
through quickly; patient ones spread activity thin enough to stay under a
velocity alert."""


def sample_sophistication(rng: np.random.Generator) -> float:
    """Draw a mule's sophistication.

    Beta(2, 2) is unimodal and bounded: most operators are middling, a few
    are at either extreme, and crucially there is no gap in the middle. A
    bimodal draw would recreate the two-archetype problem with extra steps.
    """
    return float(rng.beta(2.0, 2.0))


def sample_behaviour(
    rng: np.random.Generator,
    account_type: AccountType,
    *,
    sophistication: float | None = None,
) -> AccountBehaviour:
    """Draw one account's parameters."""
    archetype = BEHAVIOUR[account_type]

    if account_type.is_mule:
        assert sophistication is not None, "A mule needs a sophistication score"
        sweep_delay = interpolate_log(*MULE_SWEEP_DELAY_HOURS, sophistication)
        # Jitter on top, so two operators at the same sophistication still
        # differ and the mapping from delay back to score is not invertible.
        sweep_delay *= float(rng.lognormal(0.0, 0.35))
        sweep_share = _beta_around(rng, 0.95 - 0.25 * sophistication)
        outbound = _gamma_around(rng, archetype.outbound_per_month)
    else:
        sweep_delay = archetype.sweep_delay.total_seconds() / 3600.0
        if sweep_delay:
            if (
                account_type is AccountType.SMALL_BUSINESS
                and rng.random() < AUTO_SWEEP_SHARE_OF_BUSINESSES
            ):
                # On a timer: minutes to a couple of hours, the same band a
                # crude mule operates in.
                sweep_delay = float(rng.lognormal(np.log(0.5), 0.9))
            else:
                # Log-normal rather than gamma, because gamma's left tail was
                # too thin to produce any fast legitimate sweeper at all.
                sweep_delay = float(rng.lognormal(np.log(sweep_delay), 0.85))
        else:
            sweep_delay = 0.0
        sweep_share = _beta_around(rng, archetype.sweep_share) if archetype.sweep_share else 0.0
        outbound = _gamma_around(rng, archetype.outbound_per_month)

    business_like = account_type in (AccountType.SMALL_BUSINESS,)

    return AccountBehaviour(
        outbound_per_month=max(0.2, outbound),
        inbound_weight=max(0.1, _gamma_around(rng, archetype.inbound_per_month)),
        distinct_payees=max(1, int(_gamma_around(rng, float(archetype.distinct_payees)))),
        first_time_payee_rate=_beta_around(rng, archetype.first_time_payee_rate),
        sweeps_inbound=archetype.sweeps_inbound,
        sweep_delay_hours=max(0.01, sweep_delay),
        sweep_share=sweep_share,
        peak_hour=int(rng.integers(8, 22)) if not business_like else int(rng.integers(9, 18)),
        hour_spread=float(rng.uniform(1.5, 5.0)),
        weekend_activity=(
            float(rng.uniform(0.05, 0.35)) if business_like else float(rng.uniform(0.6, 1.1))
        ),
        burstiness=float(rng.beta(2.0, 5.0)),
        sophistication=sophistication or 0.0,
    )


# ---------------------------------------------------------------------------
# Temporal model
# ---------------------------------------------------------------------------

WEEKDAY_WEIGHTS = (1.0, 1.0, 1.0, 1.0, 1.05, 0.55, 0.45)
"""[ASSUMPTION] Relative payment volume by weekday, Monday first.

Weekends run lighter than weekdays for consumer payments, and far lighter for
business ones. Flat weights - which an earlier version had - make a weekday
feature useless and let a detector ignore a real seasonal signal.
"""

PAYDAY_DAYS = (1, 15, 16, 30, 31)
PAYDAY_MULTIPLIER = 1.6
"""[ASSUMPTION] Semi-monthly payday spike. Money moves when people are paid,
which makes an amount-and-timing model behave differently on the 1st than on
the 9th."""


def day_weights(window_start: datetime, window_days: int) -> np.ndarray:
    """Relative payment volume for each day in the window."""
    weights = np.empty(window_days, dtype=float)
    for offset in range(window_days):
        day = window_start + timedelta(days=offset)
        weight = WEEKDAY_WEIGHTS[day.weekday()]
        if day.day in PAYDAY_DAYS:
            weight *= PAYDAY_MULTIPLIER
        weights[offset] = weight
    return weights / weights.sum()


def sample_timestamps(
    rng: np.random.Generator,
    *,
    count: int,
    window_start: datetime,
    window_days: int,
    behaviour: AccountBehaviour,
    base_day_weights: np.ndarray,
) -> list[datetime]:
    """Draw timestamps for one account's payments.

    Three effects compose: the network-wide weekly and payday cycle, this
    account's own weekend habit, and its own daily rhythm. Burstiness then
    concentrates the draws into a few active stretches rather than spreading
    them evenly, because real accounts go quiet for weeks and then transact
    five times in a day.
    """
    if count <= 0:
        return []

    weights = base_day_weights.copy()

    # This account's own weekend behaviour, on top of the network cycle.
    for offset in range(window_days):
        if (window_start + timedelta(days=offset)).weekday() >= 5:
            weights[offset] *= behaviour.weekend_activity

    # Burstiness: raise a few random stretches and suppress the rest. At
    # burstiness 0 this is a no-op and activity stays evenly spread.
    if behaviour.burstiness > 0.05:
        n_bursts = max(1, int(window_days / 30))
        envelope = np.ones(window_days)
        for _ in range(n_bursts):
            centre = rng.integers(0, window_days)
            width = max(2.0, window_days * 0.04)
            distance = np.arange(window_days) - centre
            envelope += 3.0 * np.exp(-0.5 * (distance / width) ** 2)
        weights = weights * (1 - behaviour.burstiness) + weights * envelope * behaviour.burstiness

    weights /= weights.sum()

    days = rng.choice(window_days, size=count, p=weights)

    # Hour of day, wrapped so an account peaking at 23:00 also transacts at
    # 01:00 rather than having its distribution clipped at midnight.
    hours = (
        rng.normal(behaviour.peak_hour, behaviour.hour_spread, size=count).round().astype(int) % 24
    )
    minutes = rng.integers(0, 60, size=count)
    seconds = rng.integers(0, 60, size=count)

    return [
        window_start
        + timedelta(
            days=int(days[i]),
            hours=int(hours[i]),
            minutes=int(minutes[i]),
            seconds=int(seconds[i]),
        )
        for i in range(count)
    ]
