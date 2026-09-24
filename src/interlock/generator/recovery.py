"""How much of a payment is still there when someone asks for it back.

The single most consequential model in the project, and the one resting on the
weakest evidence. Everything about triage depends on funds decaying with time,
and the only published curve found is British.

The curve, and where it comes from
----------------------------------
RUSI, *Following the Fraud: The Role of Money Mules*, July 2025, using
anonymised Lloyds Banking Group transaction data from June to August 2024:

    ~28% of value left mule accounts within 15 minutes of arrival
    ~53% had gone within one hour
    under 15% remained after 24 hours

That is UK rails, one bank, a two-month window. It is used here because no US
equivalent was found to exist, and inventing a US curve would be worse than
importing a real one and labelling it. **Every parameter below carries
[UK-DERIVED ASSUMPTION], and so does every report built on them.**

What makes this hard rather than easy
-------------------------------------
A generator where recoverability is a clean function of elapsed time produces
a dataset where one threshold on elapsed time scores near-perfectly, and a
model trained on it learns nothing except the generator's own formula. That
was the mistake caught four times in M2, and the fix is the same each time:
add real sources of variation that make the clean rule wrong.

So recoverability here depends on elapsed time *and* on which operator holds
the account, how large the payment was relative to what else arrived, whether
the receiving institution acts quickly, and luck. The decay curve is the
population average, not any individual case's fate.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# ---------------------------------------------------------------------------
# The published anchors
# ---------------------------------------------------------------------------

RUSI_ANCHORS: tuple[tuple[float, float], ...] = (
    (15.0, 0.72),
    (60.0, 0.47),
    (1440.0, 0.15),
)
"""[UK-DERIVED ASSUMPTION] (minutes elapsed, share of value remaining).

The complement of the published figures: 28% gone at 15 minutes leaves 72%.
The calibration test in the M5 suite checks the generated population against
these three points, so a change to the model that breaks the curve fails
loudly rather than drifting.
"""

FAST_SHARE = 0.570
FAST_TAU_MINUTES = 21.0
SLOW_TAU_MINUTES = 960.0
"""[UK-DERIVED ASSUMPTION] A two-phase decay, fitted to the three anchors above.

An earlier version of this module used a single exponential and argued that a
second phase was "not justified by three observations". That was wrong, and
the calibration test caught it: no single exponential passes within tolerance
of all three anchors at once. It overshoots at fifteen minutes and undershoots
badly at twenty-four hours, because the real curve is steep early and flat
late.

Two components fit to an RMSE of 0.0003:

    57% of value on a 21-minute time constant
    43% on a 16-hour one

That split is not a curve-fitting artifact looking for a story - it matches
the operator behaviour the M2 generator already models. A crude operator
empties an account within minutes of funds landing; a patient one holds,
precisely because instant sweeps are the easiest thing in the world to alert
on. The fast and slow components are those two populations.
"""

RESIDUAL_FLOOR = 0.06
"""[UK-DERIVED ASSUMPTION] Share that stays put indefinitely.

Some funds never move: the account is frozen first, the operator abandons it,
the sweep fails. Without a floor the model says every case older than a few
days is exactly zero, which would make a large and realistic part of the queue
perfectly predictable. Fitted alongside the two time constants rather than
chosen.
"""


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """What actually happened to one recall request."""

    recovered: bool
    recovered_cents: int
    share_remaining: float
    """The fraction still present when the request was worked. The label the
    model predicts is ``recovered``; this is kept for calibration reporting."""


def expected_share_remaining(minutes_elapsed: float) -> float:
    """Population-average share of value still present.

    The curve itself, with no per-case variation. Used by the calibration
    report and by anything that needs the anchor behaviour rather than a
    sample.
    """
    if minutes_elapsed <= 0:
        return 1.0
    decayed = FAST_SHARE * float(np.exp(-minutes_elapsed / FAST_TAU_MINUTES)) + (
        1.0 - FAST_SHARE
    ) * float(np.exp(-minutes_elapsed / SLOW_TAU_MINUTES))
    return RESIDUAL_FLOOR + (1.0 - RESIDUAL_FLOOR) * decayed


# ---------------------------------------------------------------------------
# Per-case variation
# ---------------------------------------------------------------------------

OPERATOR_SPEED_LOG_SD = 0.85
"""[ASSUMPTION] Spread in how fast different mule operators move funds.

Wide on purpose, and it is what stops elapsed time being a sufficient
statistic. Two requests at the same elapsed minute can face a patient operator
who has not moved anything and a crude one who emptied the account in three
minutes. A detector that keys on elapsed time alone is therefore wrong a lot,
which is the honest situation.
"""

INSTITUTION_RESPONSIVENESS_BETA = (2.0, 2.0)
"""[ASSUMPTION] Beta parameters for how effectively an institution freezes.

Unimodal with no gap in the middle. A bimodal draw would recreate the
two-archetype problem M2 spent four rounds removing.
"""

LARGE_PAYMENT_SCRUTINY_CENTS = 1_000_000
"""[ASSUMPTION] Above $10,000, recovery odds improve slightly.

Larger inbound credits attract manual review at the receiving institution, and
a held payment is a recoverable one. Small, and it works against elapsed time
rather than with it, which is the point - it gives the model something to
learn that a single threshold cannot capture.
"""


MATERIAL_RECOVERY_SHARE = 0.10
"""[ASSUMPTION] Below a tenth of the payment, a recovery is not a recovery.

Recovering forty dollars of a four-thousand-dollar scam does not close a
customer's complaint and is not what anyone means by success. Without this
floor the label is almost always true, because the residual share means some
money is nearly always present - a defect this generator had on its first run
and the separability test caught.
"""


def sample_outcome(
    rng: np.random.Generator,
    *,
    amount_cents: int,
    minutes_elapsed: float,
    operator_speed: float,
    institution_responsiveness: float,
    return_willingness: float,
) -> RecoveryOutcome:
    """Draw what happened to one request.

    Two independent things have to go right, and keeping them separate is the
    point:

    1. **The money has to still be there.** That is the decay curve, modified
       by how fast this particular operator moves.
    2. **The institution has to hand it back.** Returning is discretionary on
       three of the four rails. An institution can hold the funds and decline,
       and a generator that ignored this would model a world where the only
       question is speed - which is exactly the world this product argues does
       not exist.

    Args:
        operator_speed: multiplier on the decay rate for this account's
            operator. Above 1 moves funds faster than average.
        institution_responsiveness: 0 to 1, how effectively the receiving
            institution freezes what is left once asked.
        return_willingness: 0 to 1, how readily it hands funds back at all.
    """
    effective_minutes = max(0.0, minutes_elapsed) * operator_speed
    share = expected_share_remaining(effective_minutes)

    # What the institution manages to hold of what is still there.
    held = share * (0.35 + 0.65 * institution_responsiveness)

    if amount_cents >= LARGE_PAYMENT_SCRUTINY_CENTS:
        held = min(1.0, held * 1.25)

    # Noise, so identical inputs do not produce identical outcomes. Real
    # recoveries turn on whether a particular person picked up a particular
    # phone.
    held = float(np.clip(held * rng.lognormal(0.0, 0.3), 0.0, 1.0))

    # The decision. Weighted by how much is left - an institution sitting on
    # the full amount is far likelier to return it than one holding scraps -
    # but never certain either way.
    returns_it = bool(rng.random() < return_willingness * (0.25 + 0.75 * held))

    recovered_cents = round(amount_cents * held) if returns_it else 0
    recovered = returns_it and held >= MATERIAL_RECOVERY_SHARE

    return RecoveryOutcome(
        recovered=recovered,
        recovered_cents=recovered_cents if recovered else 0,
        share_remaining=held,
    )


def sample_operator_speed(rng: np.random.Generator, *, sophistication: float) -> float:
    """How fast this account's operator moves funds, relative to average.

    Anti-correlated with sophistication, and deliberately only loosely. A
    patient operator using an aged account also tends to move money slowly, but
    the relationship is noisy enough that sophistication is not a proxy for
    speed - otherwise the two features would be one feature wearing a hat.
    """
    centre = -0.45 * (sophistication - 0.5)
    return float(np.exp(rng.normal(centre, OPERATOR_SPEED_LOG_SD)))


def sample_responsiveness(rng: np.random.Generator) -> float:
    """How effectively one institution freezes funds once asked."""
    return float(rng.beta(*INSTITUTION_RESPONSIVENESS_BETA))
