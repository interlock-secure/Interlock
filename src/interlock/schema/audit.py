"""The audit chain entry.

Every signal, decision, recall and disposition writes one of these. The chain
is append-only and hash-linked, so any edit or deletion after the fact is
detectable by recomputing forward from the entry in question.

This is a compliance artifact, not a debugging convenience. When an examiner
asks why a specific payment was held eight months ago, this chain is the
answer, and an answer that could have been quietly edited is not one.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from interlock.schema.common import EventType, InstitutionId, utc_now

GENESIS_HASH = "0" * 64
"""The previous_hash of the first entry in a chain. A fixed sentinel rather
than null, so that verification has no special case for the first link."""


def digest_payload(payload: dict[str, Any]) -> str:
    """Stable SHA-256 digest of an event payload.

    Keys are sorted and separators are fixed so that two participants
    serialising the same payload produce the same digest. Without that,
    verification would fail on dictionary ordering rather than on tampering,
    and the chain would cry wolf until someone switched it off.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditEntry(BaseModel):
    """One link in the chain.

    Note that the payload itself is not stored here, only its digest. The chain
    proves that a payload has not changed; it is not a copy of the payload, and
    it deliberately holds nothing that would make the chain itself a privacy
    liability if it were exported for examination.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence_number: int = Field(ge=0)
    previous_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    event_type: EventType
    actor_institution_id: InstitutionId | None = None
    """Absent for network-originated events such as NETWORK_DEGRADED, where the
    actor is the hub itself rather than a participant."""

    correlation_id: str = Field(min_length=8, max_length=64)
    """The request_id or recall_id this event belongs to, so a single payment's
    full history can be reconstructed without scanning the whole chain."""

    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    recorded_at: datetime = Field(default_factory=utc_now)

    @field_validator("recorded_at")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("recorded_at must be timezone-aware; use UTC")
        return v

    def compute_hash(self) -> str:
        """Recompute this entry's hash from its own contents.

        Every field that matters is covered. An attacker who changes the event
        type, the actor, the correlation id, the payload digest or the
        timestamp changes this value, and every subsequent entry stops
        verifying.
        """
        material = json.dumps(
            {
                "sequence_number": self.sequence_number,
                "previous_hash": self.previous_hash,
                "event_type": self.event_type.value,
                "actor_institution_id": self.actor_institution_id,
                "correlation_id": self.correlation_id,
                "payload_digest": self.payload_digest,
                "recorded_at": self.recorded_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def is_self_consistent(self) -> bool:
        """True if entry_hash matches this entry's own contents."""
        return self.entry_hash == self.compute_hash()


class ChainVerificationError(BaseModel):
    """A specific, located failure. Not an exception, because verification
    reports every break it finds rather than stopping at the first."""

    model_config = ConfigDict(frozen=True)

    sequence_number: int
    problem: str


def verify_chain(entries: list[AuditEntry]) -> list[ChainVerificationError]:
    """Verify an audit chain end to end.

    Returns every problem found rather than the first, because an operator
    investigating a suspected tamper needs the extent of it, not the earliest
    symptom.

    Detects: a modified entry (its hash no longer matches its contents), a
    broken link (previous_hash does not match the prior entry), a deleted entry
    (sequence gap), and a reordered or duplicated entry.
    """
    problems: list[ChainVerificationError] = []

    if not entries:
        return problems

    ordered = sorted(entries, key=lambda e: e.sequence_number)

    for index, entry in enumerate(ordered):
        if not entry.is_self_consistent():
            problems.append(
                ChainVerificationError(
                    sequence_number=entry.sequence_number,
                    problem="entry_hash does not match the entry's contents - it was modified",
                )
            )

        if index == 0:
            if entry.previous_hash != GENESIS_HASH and entry.sequence_number == 0:
                problems.append(
                    ChainVerificationError(
                        sequence_number=entry.sequence_number,
                        problem="first entry must link to the genesis hash",
                    )
                )
            continue

        prior = ordered[index - 1]

        expected_sequence = prior.sequence_number + 1
        if entry.sequence_number != expected_sequence:
            problems.append(
                ChainVerificationError(
                    sequence_number=entry.sequence_number,
                    problem=(
                        f"sequence gap: expected {expected_sequence}, found "
                        f"{entry.sequence_number} - an entry was removed"
                    ),
                )
            )

        if entry.previous_hash != prior.entry_hash:
            problems.append(
                ChainVerificationError(
                    sequence_number=entry.sequence_number,
                    problem="previous_hash does not match the prior entry - the chain is broken",
                )
            )

    return problems


def append_entry(
    *,
    previous: AuditEntry | None,
    event_type: EventType,
    correlation_id: str,
    payload: dict[str, Any],
    actor_institution_id: str | None = None,
    recorded_at: datetime | None = None,
) -> AuditEntry:
    """Build the next entry in a chain.

    The only supported way to extend a chain. Constructing an AuditEntry by
    hand and computing its own hash is possible but is how chains end up
    self-consistent yet unlinked.
    """
    sequence_number = 0 if previous is None else previous.sequence_number + 1
    previous_hash = GENESIS_HASH if previous is None else previous.entry_hash

    draft = AuditEntry(
        sequence_number=sequence_number,
        previous_hash=previous_hash,
        entry_hash=GENESIS_HASH,  # placeholder, replaced below
        event_type=event_type,
        actor_institution_id=actor_institution_id,
        correlation_id=correlation_id,
        payload_digest=digest_payload(payload),
        recorded_at=recorded_at or utc_now(),
    )
    return draft.model_copy(update={"entry_hash": draft.compute_hash()})
