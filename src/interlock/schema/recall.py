"""Post-settlement freeze and recall.

The second of Interlock's two moments, and the part no existing network has
standardised. On a rail that clears in seconds, most fraud is discovered after
settlement, and today recovery is a phone call to a counterparty chosen from
more than 1,800 FedNow and roughly 1,200 RTP participants.

The protocol's job is not to compel the receiving institution - it cannot, and
a design that tried would never clear anyone's counsel. Its job is to remove
ambiguity about what was asked, when, on what basis, and what was decided.
Specification Section 5.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from interlock.schema.common import (
    AccountHash,
    AmountCents,
    CaseReference,
    EventType,  # noqa: F401  (re-exported for callers)
    InstitutionId,
    ScamCategory,
    utc_now,
)
from interlock.schema.rails import house_policy_window
from interlock.schema.signal import WIRE_CONFIG


def default_recall_sla() -> timedelta:
    """The fallback window where no rail rule applies.

    Was a module constant until the rail capability matrix landed in M3. It is a
    function now because the number is not ours to invent twice: it comes from
    :func:`interlock.schema.rails.house_policy_window`, which carries the
    reasoning and the provenance, and which the console renders so an operator
    can see that this deadline is Interlock's own rather than a rail
    requirement.

    Never use this where a rail rule exists. Call
    :func:`interlock.schema.rails.require_verified_window` first and fall back
    here only when it declines - visibly, at the call site.
    """
    return house_policy_window().as_timedelta()


class RecallDispositionCode(StrEnum):
    """How a recall request ended.

    Every member of this enum is terminal. There is deliberately no PENDING and
    no NULL: a request that goes quiet is the failure mode this protocol
    exists to remove, so the type system does not offer a way to express one.
    """

    FUNDS_FROZEN = "funds_frozen"
    FUNDS_RETURNED = "funds_returned"
    PARTIAL_RETURN = "partial_return"

    INSUFFICIENT_FUNDS = "insufficient_funds"
    """The funds had already left. Not a refusal - the onward destination
    becomes a new signal input, so the chain continues rather than ending."""

    ACCOUNT_HOLDER_DISPUTES = "account_holder_disputes"
    """The receiving customer contests the claim. A legitimate outcome, not a
    failure to comply: the account holder may be an account-takeover victim
    rather than a criminal, and a protocol with no way to say so would force
    institutions to mislabel their own customers."""

    DECLINED_WITH_REASON = "declined_with_reason"
    SLA_EXPIRED_ACKNOWLEDGED = "sla_expired_acknowledged"
    """The clock ran out and the receiving institution has acknowledged that it
    did. This is a recorded disposition, not the absence of one - the
    distinction is the entire point of the SLA design."""

    @property
    def returns_funds(self) -> bool:
        return self in {
            RecallDispositionCode.FUNDS_RETURNED,
            RecallDispositionCode.PARTIAL_RETURN,
        }

    @property
    def requires_reason(self) -> bool:
        """Dispositions that are unusable to the sending institution without a
        stated basis, because they will be quoted to a customer or a regulator."""
        return self in {
            RecallDispositionCode.DECLINED_WITH_REASON,
            RecallDispositionCode.ACCOUNT_HOLDER_DISPUTES,
            RecallDispositionCode.INSUFFICIENT_FUNDS,
        }


class RecallRequest(BaseModel):
    """A sending institution asking for funds to be frozen or returned.

    ``evidence_summary`` is structured rather than free text on purpose. Free
    text invites customer narrative, which is PII, and cannot be reconciled
    across institutions. The sending institution asserts facts it can stand
    behind; the receiving institution decides what they are worth.
    """

    model_config = WIRE_CONFIG

    protocol_version: str
    recall_id: str = Field(min_length=8, max_length=64)

    originating_case_reference: CaseReference
    original_request_id: str | None = Field(default=None, min_length=8, max_length=64)
    """The pre-settlement signal request, when there was one. Absent if the
    payment was never screened - which is itself worth knowing, since it means
    the network had no chance to intercept."""

    requesting_institution_id: InstitutionId
    destination_institution_id: InstitutionId
    destination_account_hash: AccountHash

    amount_cents: AmountCents
    claimed_scam_category: ScamCategory

    victim_reported_at: datetime | None = None
    """When the customer reported it, not when we raised this request. The gap
    between the two is the single most useful predictor of whether funds are
    still recoverable."""

    requested_at: datetime = Field(default_factory=utc_now)
    sla_expires_at: datetime

    @field_validator("requested_at", "sla_expires_at")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("Timestamps must be timezone-aware; use UTC")
        return v

    @model_validator(mode="after")
    def _sla_must_be_in_the_future(self) -> RecallRequest:
        if self.sla_expires_at <= self.requested_at:
            raise ValueError("sla_expires_at must be after requested_at")
        return self

    @model_validator(mode="after")
    def _institutions_must_differ(self) -> RecallRequest:
        if self.requesting_institution_id == self.destination_institution_id:
            raise ValueError("An institution does not recall from itself through the network")
        return self

    def is_breached(self, *, at: datetime | None = None) -> bool:
        """True if the SLA window has closed."""
        return (at or utc_now()) >= self.sla_expires_at


class RecallDisposition(BaseModel):
    """The receiving institution's terminal answer.

    The receiving institution always retains the decision. What this model
    removes is the ability to make one silently, or to make one that cannot be
    explained later.
    """

    model_config = WIRE_CONFIG

    protocol_version: str
    recall_id: str = Field(min_length=8, max_length=64)

    disposition: RecallDispositionCode
    responding_institution_id: InstitutionId

    returned_amount_cents: AmountCents | None = None
    disposition_reason: str | None = Field(default=None, min_length=1, max_length=512)

    resolved_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _returned_amount_matches_disposition(self) -> RecallDisposition:
        if self.disposition.returns_funds:
            if self.returned_amount_cents is None:
                raise ValueError(
                    f"{self.disposition.value!r} must state returned_amount_cents; the sending "
                    "institution has to reconcile the return against its own ledger"
                )
            if self.returned_amount_cents <= 0:
                raise ValueError("A returning disposition must return a positive amount")
        elif self.returned_amount_cents is not None:
            raise ValueError(
                f"{self.disposition.value!r} does not return funds, so returned_amount_cents "
                "must be absent rather than zero - an explicit zero reads as a partial return "
                "that failed"
            )
        return self

    @model_validator(mode="after")
    def _reasons_where_a_reason_is_owed(self) -> RecallDisposition:
        if self.disposition.requires_reason and not self.disposition_reason:
            raise ValueError(
                f"{self.disposition.value!r} requires disposition_reason; this outcome gets "
                "quoted to a customer or a regulator and is unusable without a stated basis"
            )
        return self


class RecallAcknowledgement(BaseModel):
    """Receipt confirmation, sent before the disposition.

    Separates "we have this" from "we have decided", so the sending
    institution's analyst can tell a waiting customer the difference between a
    counterparty working the case and a counterparty that never saw it.
    """

    model_config = WIRE_CONFIG

    protocol_version: str
    recall_id: str = Field(min_length=8, max_length=64)
    acknowledging_institution_id: InstitutionId
    acknowledged_at: datetime = Field(default_factory=utc_now)
