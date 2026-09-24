"""Writing case history into the hash chain.

The state machine in :mod:`interlock.recall.state` decides what may happen.
This module records that it did, in a form nobody can quietly edit afterwards.

Why the two are separate
------------------------
The state machine is pure: no clock it does not take as an argument, no
storage, no side effects. That is what lets it be exhaustively walked over
66,430 transition sequences in a few seconds. Bolting chain writes into
``apply()`` would make every one of those sequences perform I/O and the
property test would stop being runnable.

So the ledger observes transitions instead. The cost is that a caller could
apply a transition and forget to record it, and :func:`record_transition` is
therefore the only sanctioned path - the store in
:mod:`interlock.recall.store` calls it for every save, so the two cannot drift
apart in practice.
"""

from __future__ import annotations

from dataclasses import dataclass

from interlock.recall.state import DISPOSITION_OF, CaseFile, HistoryStep, Transition
from interlock.schema.audit import AuditEntry, append_entry, verify_chain
from interlock.schema.common import EventType

EVENT_OF: dict[Transition, EventType] = {
    Transition.ACKNOWLEDGE: EventType.RECALL_ACKNOWLEDGED,
    Transition.BEGIN_INVESTIGATION: EventType.RECALL_ACKNOWLEDGED,
    Transition.ACKNOWLEDGE_SLA_EXPIRY: EventType.SLA_BREACHED,
}
"""Transition to audit event type.

Anything not listed is a disposition and records as RECALL_DISPOSED. Note that
acknowledging an expiry records as SLA_BREACHED rather than RECALL_DISPOSED:
both facts matter to an examiner, and the breach is the one they will search
for. The disposition itself is in the payload.
"""


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """An audit entry together with the payload it commits to.

    The chain stores only a digest, deliberately - it holds nothing that would
    make the chain itself a privacy liability if exported. But a regulator
    needs to see what the digest covers, so the payload travels alongside and
    the export re-derives the digest to prove the two match.
    """

    entry: AuditEntry
    payload: dict[str, str | int | None]


def _payload_for(case_file: CaseFile, step: HistoryStep) -> dict[str, str | int | None]:
    """What this event asserts, in a form that can be re-digested exactly.

    Flat and JSON-primitive on purpose. A nested structure invites a future
    change to its shape, and a changed shape means every historical digest
    stops verifying.
    """
    disposition = DISPOSITION_OF.get(step.transition)
    payload: dict[str, str | int | None] = {
        "case_id": case_file.case_id,
        "rail": case_file.case.rail.value,
        "direction": case_file.case.direction.value,
        "channel": case_file.case.channel.value,
        "amount_cents": case_file.case.amount_cents,
        "transition": step.transition.value,
        "actor": step.actor,
        "at": step.at.isoformat(),
        "reason": step.reason,
        "disposition": disposition.value if disposition else None,
        "deadline_due_at": case_file.deadline.due_at.isoformat(),
        "deadline_authority": case_file.deadline.authority.value,
        "breached_at_transition": str(case_file.is_breached(at=step.at)),
    }
    # Added only when present, so entries written before the AI layer existed
    # keep exactly the payload - and digest - they were written with.
    if step.ai_suggested is not None:
        payload["ai_suggested"] = step.ai_suggested.value
        payload["followed_ai"] = str(step.ai_suggested is step.transition)
    return payload


def record_transition(
    case_file: CaseFile,
    step: HistoryStep,
    *,
    previous: AuditEntry | None,
) -> LedgerEntry:
    """Append one applied transition to the chain."""
    payload = _payload_for(case_file, step)
    entry = append_entry(
        previous=previous,
        event_type=EVENT_OF.get(step.transition, EventType.RECALL_DISPOSED),
        correlation_id=case_file.case_id,
        payload=payload,
        actor_institution_id=case_file.case.responding_institution_id,
        recorded_at=step.at,
    )
    return LedgerEntry(entry=entry, payload=payload)


def record_opening(case_file: CaseFile, *, previous: AuditEntry | None) -> LedgerEntry:
    """Append the arrival of a case.

    Separate from :func:`record_transition` because arrival is not a
    transition - the case does not move between states, it comes into
    existence. An examiner asking when we first knew about a claim is asking
    about this entry, not about the first thing an operator did.
    """
    payload: dict[str, str | int | None] = {
        "case_id": case_file.case_id,
        "rail": case_file.case.rail.value,
        "direction": case_file.case.direction.value,
        "channel": case_file.case.channel.value,
        "amount_cents": case_file.case.amount_cents,
        "reason": case_file.case.reason.value,
        "native_reason_code": case_file.case.native.reason_code,
        "requesting_institution": case_file.case.requesting_institution_id,
        "responding_institution": case_file.case.responding_institution_id,
        "received_at": case_file.case.received_at.isoformat(),
        "original_settled_at": (
            case_file.case.original_settled_at.isoformat()
            if case_file.case.original_settled_at
            else None
        ),
        "deadline_due_at": case_file.deadline.due_at.isoformat(),
        "deadline_authority": case_file.deadline.authority.value,
        "deadline_reason": case_file.deadline.reason,
    }
    # For a case filed from free text: what the extractor proposed and who
    # confirmed it, so the evidence shows machine proposal against human
    # filing. Added only when present, so other openings keep their digests.
    extra = case_file.case.native.extra or {}
    for key in sorted(extra):
        if key.startswith("extracted_") or key in {"confirmed_by", "intake_channel"}:
            payload[f"intake_{key}" if not key.startswith("intake_") else key] = str(extra[key])
    entry = append_entry(
        previous=previous,
        event_type=EventType.RECALL_RAISED,
        correlation_id=case_file.case_id,
        payload=payload,
        actor_institution_id=case_file.case.responding_institution_id,
        recorded_at=case_file.case.received_at,
    )
    return LedgerEntry(entry=entry, payload=payload)


def rebuild_chain(entries: list[LedgerEntry]) -> list[str]:
    """Verify a chain and confirm each payload matches the digest it committed to.

    Two distinct failures, reported together:

    - The chain is broken - an entry was modified, removed or reordered. This
      is :func:`~interlock.schema.audit.verify_chain`'s job.
    - The chain is intact but a *payload* was altered. The entry still links
      correctly, so chain verification alone passes, and only re-digesting the
      payload catches it. An export that skipped this check could hand an
      examiner a cryptographically sound chain describing events that did not
      happen.
    """
    from interlock.schema.audit import digest_payload

    problems = [
        f"#{p.sequence_number}: {p.problem}" for p in verify_chain([item.entry for item in entries])
    ]

    for item in entries:
        recomputed = digest_payload(dict(item.payload))
        if recomputed != item.entry.payload_digest:
            problems.append(
                f"#{item.entry.sequence_number}: payload does not match its committed "
                f"digest - the chain links correctly but the event body was altered"
            )

    return problems
