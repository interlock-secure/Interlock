"""Pre-settlement signal exchange.

The first of Interlock's two moments: between payment initiation and
settlement, inside a hard 300ms budget, the sending institution asks the
network what the receiving institution knows about the destination account.

The response carries a band and the dimensions behind it. It never carries a
recommendation, because the decision belongs to the sending institution and its
own threshold. Specification Section 5.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from interlock.schema.common import (
    AccountHash,
    AmountCents,
    CaseReference,
    Confidence,
    InstitutionId,
    PaymentDecision,
    Rail,
    RiskBand,
    SenderContextFlag,
    SignalDimension,
    utc_now,
)

WIRE_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    str_strip_whitespace=True,
    use_enum_values=False,
)
"""Shared configuration for every model that crosses an institutional boundary.

``extra="forbid"`` is the important one. A participant that adds an undeclared
field gets a validation error rather than having it silently dropped or,
worse, silently carried - which is how PII leaks into a protocol that was
designed not to carry any.

``frozen=True`` because a wire message is a record of what was sent. Mutating
one after the fact desynchronises it from the audit entry that hashed it.
"""


class SignalRequest(BaseModel):
    """A sending institution asking about a destination account.

    Note what is absent: no customer name, no sender account, no device
    fingerprint, no free text. The receiving institution learns that someone on
    the network is about to send a given amount to one of its accounts, and
    what the sending side found unusual. It does not learn who is sending.
    """

    model_config = WIRE_CONFIG

    protocol_version: str
    request_id: str = Field(min_length=8, max_length=64)

    requesting_institution_id: InstitutionId
    destination_institution_id: InstitutionId
    destination_account_hash: AccountHash

    amount_cents: AmountCents
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    rail: Rail

    sender_context_flags: frozenset[SenderContextFlag] = Field(default_factory=frozenset)
    """What the sending side observed. Empty is valid and means "nothing
    unusual", which is itself information."""

    requested_at: datetime = Field(default_factory=utc_now)

    @field_validator("requested_at")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("requested_at must be timezone-aware; use UTC")
        return v

    @field_serializer("sender_context_flags")
    def _sort_flags(self, flags: frozenset[SenderContextFlag]) -> list[str]:
        """Serialise deterministically.

        A set has no order, and Python's string hash randomisation means the
        same set can serialise differently between two processes. That would be
        cosmetic if not for the audit chain: the payload digest is computed
        over the serialised message, so two participants recording the same
        event would produce different hashes and the chain would report
        tampering that never happened.
        """
        return sorted(f.value for f in flags)

    @model_validator(mode="after")
    def _institutions_must_differ(self) -> SignalRequest:
        if self.requesting_institution_id == self.destination_institution_id:
            raise ValueError(
                "An institution cannot query itself through the network; "
                "on-us payments are screened by its own engine directly"
            )
        return self


class SignalResponse(BaseModel):
    """The receiving institution's answer, or an explicit non-answer.

    ``explanation_strings`` is not decoration. A band with no reason is
    unusable by an analyst working a queue, and unusable by a regulator asking
    why a payment was held six months later. The invariant below enforces it.
    """

    model_config = WIRE_CONFIG

    protocol_version: str
    request_id: str = Field(min_length=8, max_length=64)

    risk_band: RiskBand
    contributing_dimensions: frozenset[SignalDimension] = Field(default_factory=frozenset)
    confidence: Confidence | None = None
    explanation_strings: tuple[str, ...] = ()

    responding_institution_id: InstitutionId | None = None
    """Absent when the band is UNAVAILABLE - there was no responder."""

    valid_until: datetime | None = None
    responded_at: datetime = Field(default_factory=utc_now)

    @field_serializer("contributing_dimensions")
    def _sort_dimensions(self, dimensions: frozenset[SignalDimension]) -> list[str]:
        """Deterministic order, for the same reason as sender_context_flags:
        the audit chain digests the serialised message."""
        return sorted(d.value for d in dimensions)

    @model_validator(mode="after")
    def _actionable_bands_must_explain_themselves(self) -> SignalResponse:
        """An actionable band needs a dimension and a reason; a non-answer must
        not pretend to have one.

        This is the schema-level expression of the rule that the network never
        reports "we don't know" as "low risk". A LOW band with no contributing
        dimension is indistinguishable from a silent failure, so it is invalid.
        """
        if self.risk_band.is_actionable:
            if not self.contributing_dimensions:
                raise ValueError(
                    f"risk_band {self.risk_band.value!r} is an assessment and must name at "
                    "least one contributing dimension; a band with no dimension behind it "
                    "cannot be distinguished from a failure that defaulted to it"
                )
            if not self.explanation_strings:
                raise ValueError(
                    f"risk_band {self.risk_band.value!r} must carry at least one explanation "
                    "string; an analyst cannot action a bare band and a regulator cannot "
                    "reconstruct one"
                )
            if self.responding_institution_id is None:
                raise ValueError(
                    "An actionable band must identify the institution that produced it"
                )
        else:
            if self.contributing_dimensions:
                raise ValueError(
                    f"risk_band {self.risk_band.value!r} is a non-answer and must not carry "
                    "contributing dimensions"
                )
            if self.confidence is not None:
                raise ValueError(
                    f"risk_band {self.risk_band.value!r} is a non-answer; a confidence value "
                    "on it would imply an assessment that was never made"
                )
        return self

    @model_validator(mode="after")
    def _validity_window_must_be_future(self) -> SignalResponse:
        if self.valid_until is not None and self.valid_until <= self.responded_at:
            raise ValueError("valid_until must be after responded_at")
        return self

    def is_expired(self, *, at: datetime | None = None) -> bool:
        """True if this response is past its validity window.

        A stale signal is worse than none: mule account behaviour changes over
        hours, so a cached band from this morning is a statement about a
        different account than the one receiving money now.
        """
        if self.valid_until is None:
            return False
        return (at or utc_now()) >= self.valid_until

    @classmethod
    def unavailable(cls, *, request_id: str, protocol_version: str, reason: str) -> SignalResponse:
        """Construct the explicit non-answer returned on timeout or outage.

        Exists as a constructor so that the degraded path is as easy to write
        correctly as the happy path. Every caller that would otherwise be
        tempted to return a default LOW band has this to reach for instead.
        """
        return cls(
            protocol_version=protocol_version,
            request_id=request_id,
            risk_band=RiskBand.UNAVAILABLE,
            explanation_strings=(reason,),
        )


class DecisionRecord(BaseModel):
    """What the sending institution did, and on what basis.

    Recorded rather than transmitted: this does not go to the receiving
    institution, it goes to the audit chain. It is what lets the network answer
    "why was this payment held" with the signal, the threshold and the action
    all in one place.
    """

    model_config = WIRE_CONFIG

    protocol_version: str
    request_id: str = Field(min_length=8, max_length=64)
    case_reference: CaseReference | None = None

    decision: PaymentDecision
    acted_on_band: RiskBand
    institution_threshold: str = Field(min_length=1, max_length=128)
    """The sending institution's own configured policy at decision time, as a
    human-readable label. Institutions set thresholds independently, so the
    same band produces different decisions at different participants and the
    record has to capture which policy applied."""

    decided_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _degraded_decisions_must_be_visible(self) -> DecisionRecord:
        """A payment must never be blocked because the network was unreachable.

        Specification FR-7: when the hub is unreachable or over budget, the
        payment proceeds on the institution's own score. A HELD decision on an
        UNAVAILABLE band means a network outage became a payment outage, which
        is the fastest way to lose a participant permanently.
        """
        if self.acted_on_band is RiskBand.UNAVAILABLE and self.decision is PaymentDecision.HELD:
            raise ValueError(
                "Cannot hold a payment on an UNAVAILABLE band: a network outage must not "
                "become a payment outage (FR-7). Fall back to the institution's own score "
                "and record the degraded state."
            )
        return self


DEFAULT_SIGNAL_VALIDITY = timedelta(minutes=5)
"""How long a signal response stays usable.

Short by design. The dimensions behind a band - inbound velocity, onward
movement - are computed over windows measured in minutes, so a longer validity
would serve a cached answer about an account that has since changed behaviour.
"""
