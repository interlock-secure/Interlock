"""Turning fraudulent payments into the recall cases an operator works.

A recall case is not a payment. It is what happens when a victim notices, tells
their institution, and that institution asks the receiving one for the money
back. Three separate delays sit between settlement and the request landing, and
they are the whole game:

    settlement -> victim notices -> institution raises it -> we receive it

The sum of those is the elapsed time that decides whether anything is left.
Modelling them separately matters because they have different causes and
different distributions - a victim can take four days to notice an invoice
redirection and four minutes to notice a drained current account - and
collapsing them into one delay would make the case population far too uniform.

Labels
------
Each case carries whether funds were recovered and how much. Those come from
:mod:`interlock.generator.recovery`, which decays with elapsed time but with
enough real variation that no single threshold separates the classes. The
separability test in the M5 suite enforces that.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from interlock.generator.counterparties import (
    Counterparty,
    build_counterparties,
    response_delay_hours,
)
from interlock.generator.payments import Payment
from interlock.generator.recovery import (
    RecoveryOutcome,
    sample_operator_speed,
    sample_outcome,
    sample_responsiveness,
)
from interlock.schema.case import Channel, Direction, RecallReason
from interlock.schema.common import Rail

# ---------------------------------------------------------------------------
# The three delays
# ---------------------------------------------------------------------------

NOTICE_DELAY_HOURS_BY_SCAM: dict[str, tuple[float, float]] = {
    "investment": (3.2, 1.3),
    "romance": (4.1, 1.4),
    "impersonation": (1.1, 1.2),
    "purchase": (3.0, 1.1),
    "invoice": (3.8, 1.2),
    "advance_fee": (2.6, 1.3),
    "job_offer": (3.5, 1.3),
    "other": (2.5, 1.4),
}
"""[ASSUMPTION] Log-normal (mu, sigma) for hours until the victim notices, by scam type.

The spread across categories is the important part, not the exact values. An
impersonation scam - a caller pretending to be the bank - unravels within a
couple of hours, because the victim calls the real bank. An invoice
redirection surfaces when someone chases an unpaid invoice, which can be weeks.
No published distribution of victim notice times was found; these are shaped
from the categories' own mechanics and are labelled as assumptions.
"""

INSTITUTION_RAISE_DELAY_HOURS = (0.4, 0.9)
"""[ASSUMPTION] Log-normal (mu, sigma) for hours from the victim reporting to
the request being raised. Median around 1.5 hours - a queue, a triage, a form."""

REPORTED_SHARE = 0.77
"""[ASSUMPTION] Share of fraud victims who report to their institution at all.

From PYMNTS Intelligence survey reporting, November 2025, n=15,110: 77% of
scam victims reported to their financial institution. Survey self-report, not
transaction data, so it is a labelled assumption rather than a measurement.
Unreported fraud produces no recall case, which is why this is here: a
generator that turned every fraudulent payment into a case would overstate the
queue by about a third.
"""

_SCAM_TO_REASON = {
    "invoice": RecallReason.FRAUD_SCAM,
    "impersonation": RecallReason.FRAUD_SCAM,
    "investment": RecallReason.FRAUD_SCAM,
    "romance": RecallReason.FRAUD_SCAM,
    "purchase": RecallReason.FRAUD_SCAM,
    "advance_fee": RecallReason.FRAUD_SCAM,
    "job_offer": RecallReason.FRAUD_SCAM,
    "other": RecallReason.FRAUD_SCAM,
}

_RAIL_NAMES = {"fednow": Rail.FEDNOW, "rtp": Rail.RTP, "ach": Rail.ACH, "wire": Rail.FEDWIRE}


@dataclass(frozen=True, slots=True)
class GeneratedCase:
    """One recall case with its outcome label.

    Flat and primitive so it writes straight to CSV and reads back without a
    schema migration. The canonical :class:`~interlock.schema.case.RecallCase`
    is built from this at the boundary, not stored as it.
    """

    case_id: str
    rail: str
    direction: str
    channel: str
    reason: str

    amount_cents: int
    requesting_institution_id: str
    responding_institution_id: str
    original_payment_reference: str

    original_settled_at: datetime
    victim_reported_at: datetime
    received_at: datetime

    minutes_since_settlement: float
    """Precomputed because it is the feature everything turns on, and deriving
    it in three places invites three subtly different answers."""

    counterparty_median_response_hours: float
    counterparty_return_willingness: float
    counterparty_answers_within_window: float
    arrived_structured: bool
    scam_category: str

    responded_after_hours: float
    answered_within_window: bool

    recovered: bool
    recovered_cents: int
    share_remaining: float

    split: str
    """train or test, assigned temporally."""


def _case_id(payment_id: str) -> str:
    digest = hashlib.sha256(payment_id.encode()).hexdigest()[:10].upper()
    return f"ILK-{digest}"


def _channel_for(rail: Rail, structured: bool) -> Channel:
    if not structured:
        return Channel.EMAIL
    if rail is Rail.ACH:
        return Channel.ACH_R06_REQUEST
    return Channel.CAMT_056


def generate_cases(
    payments: list[Payment],
    *,
    rng: np.random.Generator,
    window_start: datetime,
    window_days: int,
    train_fraction: float = 0.7,
    counterparties: list[Counterparty] | None = None,
) -> tuple[list[GeneratedCase], list[Counterparty]]:
    """Build recall cases from the fraudulent payments in a ledger.

    Only cross-institution fraud produces a case: an internal transfer is
    resolved inside one bank and never becomes an interbank request, which is
    exactly the population this product does not serve.

    The split is temporal, on the date the case was received. A random split
    would let a model see an institution's later behaviour while predicting
    its earlier cases, which is the classic leak in any dataset with entities
    that recur.
    """
    counterparties = counterparties or build_counterparties(rng)
    by_id = {c.institution_id: c for c in counterparties}

    # Each mule account gets one operator speed, drawn once. Two requests
    # against the same account face the same operator, which is a real
    # correlation the model should be able to find.
    operator_speed: dict[str, float] = {}
    responsiveness: dict[str, float] = {}

    split_boundary = window_start + timedelta(days=int(window_days * train_fraction))
    cases: list[GeneratedCase] = []

    for payment in payments:
        if not payment.is_fraud or not payment.is_cross_institution:
            continue
        if rng.random() > REPORTED_SHARE:
            continue

        scam = payment.scam_category or "other"
        mu, sigma = NOTICE_DELAY_HOURS_BY_SCAM.get(scam, NOTICE_DELAY_HOURS_BY_SCAM["other"])
        notice_hours = float(rng.lognormal(mu, sigma))
        raise_hours = float(rng.lognormal(*INSTITUTION_RAISE_DELAY_HOURS))

        victim_reported_at = payment.timestamp + timedelta(hours=notice_hours)
        received_at = victim_reported_at + timedelta(hours=raise_hours)

        # Cases arriving after the window closes belong to the next period.
        if received_at >= window_start + timedelta(days=window_days):
            continue

        counterparty = counterparties[int(rng.integers(0, len(counterparties)))]

        if payment.receiver_account_id not in operator_speed:
            # Sophistication is not carried on the Payment, so it is drawn per
            # receiving account here and cached. The correlation that matters -
            # same account, same operator - is preserved either way.
            operator_speed[payment.receiver_account_id] = sample_operator_speed(
                rng, sophistication=float(rng.beta(2.0, 2.0))
            )
            responsiveness[payment.receiver_account_id] = sample_responsiveness(rng)

        minutes = (received_at - payment.timestamp).total_seconds() / 60.0

        outcome: RecoveryOutcome = sample_outcome(
            rng,
            amount_cents=payment.amount_cents,
            minutes_elapsed=minutes,
            operator_speed=operator_speed[payment.receiver_account_id],
            institution_responsiveness=(
                responsiveness[payment.receiver_account_id] * 0.5
                + counterparty.responsiveness * 0.5
            ),
            return_willingness=counterparty.return_willingness,
        )

        responded_hours = response_delay_hours(rng, counterparty)
        answered_in_window = bool(rng.random() < counterparty.answers_within_window)

        rail = _RAIL_NAMES.get(payment.rail, Rail.FEDNOW)
        structured = bool(rng.random() < (0.85 if counterparty.prefers_structured else 0.25))

        cases.append(
            GeneratedCase(
                case_id=_case_id(payment.payment_id),
                rail=rail.value,
                direction=Direction.INBOUND.value,
                channel=_channel_for(rail, structured).value,
                reason=_SCAM_TO_REASON.get(scam, RecallReason.FRAUD_SCAM).value,
                amount_cents=payment.amount_cents,
                requesting_institution_id=counterparty.institution_id,
                responding_institution_id=payment.receiver_institution_id,
                original_payment_reference=payment.payment_id,
                original_settled_at=payment.timestamp,
                victim_reported_at=victim_reported_at,
                received_at=received_at,
                minutes_since_settlement=minutes,
                counterparty_median_response_hours=counterparty.median_response_hours,
                counterparty_return_willingness=counterparty.return_willingness,
                counterparty_answers_within_window=counterparty.answers_within_window,
                arrived_structured=structured,
                scam_category=scam,
                responded_after_hours=responded_hours,
                answered_within_window=answered_in_window,
                recovered=outcome.recovered,
                recovered_cents=outcome.recovered_cents,
                share_remaining=outcome.share_remaining,
                split="train" if received_at < split_boundary else "test",
            )
        )

    _ = by_id  # kept for callers that resolve a case back to its counterparty
    return cases, counterparties


def case_statistics(cases: list[GeneratedCase]) -> dict[str, object]:
    """Figures for the manifest and the calibration report."""
    if not cases:
        return {"total": 0}

    recovered = [c for c in cases if c.recovered]
    total_value = sum(c.amount_cents for c in cases)
    recovered_value = sum(c.recovered_cents for c in cases)

    return {
        "total": len(cases),
        "recovered": len(recovered),
        "recovery_rate": len(recovered) / len(cases),
        "value_recovery_rate": (recovered_value / total_value) if total_value else 0.0,
        "median_minutes_to_request": float(np.median([c.minutes_since_settlement for c in cases])),
        "train": sum(1 for c in cases if c.split == "train"),
        "test": sum(1 for c in cases if c.split == "test"),
        "structured_share": sum(1 for c in cases if c.arrived_structured) / len(cases),
        "answered_within_window_share": sum(1 for c in cases if c.answered_within_window)
        / len(cases),
        "by_rail": {
            rail: sum(1 for c in cases if c.rail == rail)
            for rail in sorted({c.rail for c in cases})
        },
    }
