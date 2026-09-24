"""The seeded demo dataset.

An interviewer opens the URL and has to see a working queue within seconds.
That means the instance seeds itself, deterministically, so the demo is the
same every time and can be talked through from notes.

The cases come from the M5 generator rather than from a hand-written fixture,
so the demo shows the real population - decay curve, heterogeneous
counterparties, mixed channels - rather than a tableau arranged to flatter the
ranking.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from interlock.config import Settings
from interlock.generator.config import INFLATED_ARM
from interlock.generator.recall_cases import GeneratedCase, generate_cases
from interlock.generator.testbed import WINDOW_START, build_arm
from interlock.recall.state import CaseFile, Transition
from interlock.recall.store import SqliteCaseRepository
from interlock.schema.case import (
    Channel,
    Direction,
    NativeEnvelope,
    RecallCase,
    RecallReason,
)
from interlock.schema.common import Rail

DEMO_CASE_COUNT = 40
"""Enough to make a ranked queue worth looking at, few enough to scan."""

CLOSED_SHARE = 0.35
"""Share of demo cases already disposed, so the evidence export has something
to attest to and the queue is not suspiciously all-open."""

_REASON_CODES = {
    Rail.FEDNOW: "FRAD",
    Rail.RTP: "FRAD",
    Rail.ACH: "R06",
    Rail.FEDWIRE: "FRAD",
}


def _to_canonical(
    generated: GeneratedCase, *, institution_id: str, offset: timedelta
) -> RecallCase:
    """Turn a generated row into the canonical case the system stores.

    One offset is applied to the whole population rather than a per-case
    shift, so the relative spacing the generator produced survives. That
    spacing is the point: it is what makes some cases urgent and others
    already lost, and jittering each case independently would flatten it.
    """
    rail = Rail(generated.rail)

    return RecallCase(
        case_id=generated.case_id,
        rail=rail,
        direction=Direction.INBOUND,
        channel=Channel(generated.channel),
        original_payment_reference=generated.original_payment_reference[:64],
        amount_cents=generated.amount_cents,
        reason=RecallReason(generated.reason),
        requesting_institution_id=generated.requesting_institution_id,
        responding_institution_id=institution_id,
        original_settled_at=generated.original_settled_at + offset,
        victim_reported_at=generated.victim_reported_at + offset,
        received_at=generated.received_at + offset,
        native=NativeEnvelope(
            message_id=f"DEMO-{generated.case_id}",
            reason_code=_REASON_CODES.get(rail, "FRAD"),
            creation_time=generated.received_at + offset,
            extra={"scam_category": generated.scam_category, "source": "demo_seed"},
        ),
    )


def seed(repo: SqliteCaseRepository, config: Settings, *, now: datetime | None = None) -> int:
    """Populate an empty repository with the demo queue.

    Returns the number of cases created. Does nothing if the repository
    already holds cases, so a restart against a persistent database does not
    duplicate the demo.
    """
    if repo.all_cases():
        return 0

    now = now or datetime.now(UTC)
    rng = np.random.default_rng(config.demo_seed)

    _, payments, _ = build_arm(INFLATED_ARM)
    generated, _ = generate_cases(
        payments,
        rng=rng,
        window_start=WINDOW_START,
        window_days=INFLATED_ARM.window_days,
    )

    # Take the most recent cases, so the demo queue is the tail of the
    # population rather than an arbitrary slice.
    recent = sorted(generated, key=lambda c: c.received_at)[-DEMO_CASE_COUNT:]
    if not recent:  # pragma: no cover - the generator always produces cases
        return 0

    # Land the newest case a few minutes ago rather than exactly now, so the
    # freshest row shows a real elapsed time instead of zero.
    offset = (now - timedelta(minutes=9)) - recent[-1].received_at

    created = 0
    for index, row in enumerate(recent):
        case = _to_canonical(row, institution_id=config.institution_id, offset=offset)
        try:
            case_file: CaseFile = repo.open_new(case)
        except ValueError as exc:
            # Only the duplicate-case-id case is expected here. Swallowing
            # every ValueError also hid Pydantic validation failures, so a
            # broken generator produced a quietly short demo queue instead of
            # an error.
            if "already exists" not in str(exc):
                raise
            continue
        created += 1

        # Close a share of them, so the evidence export has history and the
        # queue shows a realistic mix.
        if index % int(1 / CLOSED_SHARE) == 0:
            acknowledged = case_file.apply(
                Transition.ACKNOWLEDGE, actor="demo-operator", at=case.received_at
            )
            repo.save_transition(case_file, acknowledged)

            disposition = (
                Transition.DISPOSE_FUNDS_RETURNED
                if row.recovered
                else Transition.DISPOSE_INSUFFICIENT_FUNDS
            )
            repo.save_transition(
                acknowledged,
                acknowledged.apply(
                    disposition,
                    actor="demo-operator",
                    reason=(
                        None
                        if row.recovered
                        else "Beneficiary account emptied before the request arrived"
                    ),
                    at=case.received_at + timedelta(hours=float(row.responded_after_hours)),
                ),
            )

    return created
