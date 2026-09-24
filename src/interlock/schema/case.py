"""The canonical recall case: one shape every rail normalises into.

A bank receiving "please send that money back" gets it four different ways - a
camt.056 over FedNow or RTP, an R06 request over ACH, a Fed Exception Resolution
Service case, or an email from someone who found a phone number. Same business
event, four queues, often four teams.

This module is the shape all four become. Everything downstream - the SLA clock,
the triage ranking, the disposition ledger, the console - codes against
:class:`RecallCase` and never against a rail message.

Two rules that shape the design
-------------------------------
**Nothing is lost on the way in.** An adapter that cannot round-trip its input
is not finished. Every rail-specific value that has no canonical home is kept in
``native`` so the emitter can reproduce the original byte for byte. The
alternative - dropping what does not fit - means the first real integration
finds out we corrupted a counterparty's case reference.

**The canonical reason is a lossy view, and says so.** ``reason`` is what
Interlock reasons about; ``native.reason_code`` is what the rail actually said.
Where they disagree, the native code is the fact and the canonical one is our
interpretation.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from interlock.schema.common import (
    AmountCents,
    CaseReference,
    InstitutionId,
    Rail,
    utc_now,
)
from interlock.schema.signal import WIRE_CONFIG

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Direction(StrEnum):
    """Which way the request runs, from this institution's point of view.

    The distinction drives almost everything. An inbound request starts a clock
    we must answer; an outbound one starts a clock we are waiting on. Same
    shape, opposite obligations.
    """

    INBOUND = "inbound"
    """Another institution is asking us to return funds."""

    OUTBOUND = "outbound"
    """We are asking another institution, usually because our customer was
    scammed."""


class Channel(StrEnum):
    """How the request physically arrived.

    Worth recording separately from the rail, because they come apart in both
    directions: a FedNow return request can arrive as an email when the
    counterparty's operations team gives up on the message, and a structured
    camt.056 can carry an ACH-originated dispute.

    The unstructured channels are not a legacy nuisance to be designed out. They
    are the operational majority today - a bank operations VP described
    counterparty registries as listing "call-tree-hell" numbers, and reported
    speaking to 18 representatives at one institution over four hours to resolve
    a single case.
    """

    CAMT_056 = "camt_056"
    CAMT_029 = "camt_029"
    ACH_R06_REQUEST = "ach_r06_request"
    ACH_RETURN_ENTRY = "ach_return_entry"
    FED_ERS = "fed_ers"
    NACHA_PORTAL = "nacha_portal"
    EMAIL = "email"
    PHONE_NOTE = "phone_note"
    SECURE_MESSAGE = "secure_message"

    @property
    def is_structured(self) -> bool:
        """True where the request arrived as a machine-readable message."""
        return self in {
            Channel.CAMT_056,
            Channel.CAMT_029,
            Channel.ACH_R06_REQUEST,
            Channel.ACH_RETURN_ENTRY,
        }


class RecallReason(StrEnum):
    """Why the funds are being asked for, canonically.

    A deliberately small set. The rails between them define several dozen codes,
    most of which describe operational errors Interlock does not handle. What
    matters downstream is a handful of distinctions that change how a case is
    worked, and inventing finer categories than that produces a taxonomy nobody
    populates correctly.

    The one distinction that genuinely matters is FRAUD_SCAM against
    FRAUD_UNAUTHORISED, and it is not a nicety:

    - FRAUD_SCAM - the customer authorised the payment, having been deceived.
      Nacha calls this False Pretenses. Under UCC Article 4A a wire in this
      category is an effective payment order and the customer bears the loss.
      Reportedly exempt from RTP's response window.
    - FRAUD_UNAUTHORISED - the customer never authorised it. Different legal
      footing entirely, and on ACH a different return code with a different
      clock.

    A system that collapsed the two would apply the wrong deadline and the wrong
    liability model to half its cases.
    """

    FRAUD_SCAM = "fraud_scam"
    FRAUD_UNAUTHORISED = "fraud_unauthorised"
    DUPLICATE = "duplicate"
    TECHNICAL_ERROR = "technical_error"
    WRONG_AMOUNT = "wrong_amount"
    WRONG_BENEFICIARY = "wrong_beneficiary"
    CUSTOMER_REQUEST = "customer_request"
    UNKNOWN = "unknown"
    """The rail said something we do not have a canonical mapping for.

    Not an error. It means the native code is the only trustworthy statement of
    why, and anything reasoning about this case should read ``native.reason_code``
    rather than guessing from this field.
    """

    @property
    def is_fraud(self) -> bool:
        return self in {RecallReason.FRAUD_SCAM, RecallReason.FRAUD_UNAUTHORISED}


class CaseState(StrEnum):
    """Where a case has got to.

    M3 only ever produces RECEIVED. The state machine, its transitions and the
    guarantee that nothing reaches a closed state without a disposition all
    belong to :mod:`interlock.recall.state` in M4. This enum exists now so the
    canonical shape does not change under M4's feet.
    """

    RECEIVED = "received"
    ACKNOWLEDGED = "acknowledged"
    INVESTIGATING = "investigating"
    DISPOSED = "disposed"

    @property
    def is_terminal(self) -> bool:
        return self is CaseState.DISPOSED


# ---------------------------------------------------------------------------
# Native passthrough
# ---------------------------------------------------------------------------


class NativeEnvelope(BaseModel):
    """Everything the rail said that the canonical model has no field for.

    This is what makes lossless round-tripping possible. It is not a junk
    drawer: an adapter puts here exactly what it needs to rebuild its own
    message, and a round-trip test fails loudly if that turns out to be
    incomplete.
    """

    model_config = WIRE_CONFIG

    message_id: str = Field(min_length=1, max_length=64)
    """The rail's own identifier for this message. Never regenerated on emit -
    a counterparty reconciles against it."""

    reason_code: str = Field(min_length=1, max_length=32)
    """The rail's reason code verbatim: FRAD, DUPL, R06, and so on. The fact of
    record where the canonical reason and this disagree."""

    creation_time: datetime | None = None
    """The message's own creation timestamp, distinct from when we received it.
    Preserved because emitting a different one makes a re-sent message look like
    a new one."""

    extra: dict[str, str] = Field(default_factory=dict)
    """Rail-specific fields with no canonical home. String values only: this is
    passthrough, not a place to grow a second schema."""

    @field_validator("creation_time")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("Timestamps must be timezone-aware; use UTC")
        return v


# ---------------------------------------------------------------------------
# The case
# ---------------------------------------------------------------------------


class RecallCase(BaseModel):
    """One return-of-funds request, normalised.

    Immutable, like everything else on the wire. M4 produces a new case rather
    than mutating this one, so the audit chain records a sequence of states
    instead of a single object whose history has been overwritten.
    """

    model_config = WIRE_CONFIG

    case_id: str = Field(min_length=8, max_length=64)
    """Interlock's own identifier, stable across the case's life."""

    rail: Rail
    direction: Direction
    channel: Channel

    original_payment_reference: CaseReference
    """The rail's identifier for the payment being clawed back. Opaque to us -
    we carry it so both sides can reconcile against their own ledgers."""

    amount_cents: AmountCents
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")

    reason: RecallReason
    requesting_institution_id: InstitutionId
    responding_institution_id: InstitutionId

    original_settled_at: datetime | None = None
    """When the payment being recalled actually settled.

    The single most important field for triage. Everything about whether funds
    are still there is a function of elapsed time since this instant, and
    published UK data puts roughly 28% of value out of a mule account within
    fifteen minutes of arrival. A case without it can still be worked, but it
    cannot be ranked.
    """

    victim_reported_at: datetime | None = None
    """When the customer said something, not when the request was raised. The
    gap between the two is how much time the sending institution's own process
    cost."""

    received_at: datetime = Field(default_factory=utc_now)

    native: NativeEnvelope

    # -- validation --------------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_state(cls, data: object) -> object:
        """Accept case JSON written while this model still had a ``state`` field.

        The field was removed because it was never updated: a case's state
        lives on :class:`~interlock.recall.state.CaseFile`, and a second copy
        here read ``received`` forever, which a review flagged as a trap. Rows
        stored before the removal carry ``"state": "received"``, so that exact
        value is dropped; any other value means something wrote state onto the
        wire model, and is refused rather than silently discarded.
        """
        if isinstance(data, dict) and "state" in data:
            if data["state"] != CaseState.RECEIVED.value:
                raise ValueError(
                    "RecallCase no longer carries state; it lives on the case file. "
                    f"Refusing a payload that sets it to {data['state']!r}."
                )
            data = {k: v for k, v in data.items() if k != "state"}
        return data

    @field_validator("original_settled_at", "victim_reported_at", "received_at")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("Timestamps must be timezone-aware; use UTC")
        return v

    @model_validator(mode="after")
    def _institutions_must_differ(self) -> RecallCase:
        if self.requesting_institution_id == self.responding_institution_id:
            raise ValueError(
                "An institution does not recall from itself; requesting and responding "
                "institution must differ"
            )
        return self

    @model_validator(mode="after")
    def _settlement_precedes_report(self) -> RecallCase:
        """Funds cannot be reported stolen before they moved.

        Cheap check, and it catches the commonest real integration bug in this
        domain: a counterparty sending local time in a field documented as UTC,
        which shows up as a report timestamp hours before settlement.
        """
        if (
            self.original_settled_at is not None
            and self.victim_reported_at is not None
            and self.victim_reported_at < self.original_settled_at
        ):
            raise ValueError(
                "victim_reported_at precedes original_settled_at, which is impossible; "
                "suspect a timezone-naive timestamp from the counterparty"
            )
        return self

    # -- derived -----------------------------------------------------------

    @property
    def is_fraud_claim(self) -> bool:
        """Whether this case is a fraud claim, which changes which rules apply.

        Read this rather than comparing reasons at the call site: RTP's window
        reportedly exempts fraud, so this predicate decides whether a rail
        deadline exists at all.
        """
        return self.reason.is_fraud

    def minutes_since_settlement(self, *, at: datetime | None = None) -> float | None:
        """Elapsed minutes since the payment settled, or None if unknown.

        The core triage feature. Returns None rather than a sentinel, so a
        missing value cannot be silently treated as "just now" - which would
        rank an unknown case as the most urgent thing in the queue.
        """
        if self.original_settled_at is None:
            return None
        return ((at or utc_now()) - self.original_settled_at).total_seconds() / 60.0

    def amount_decimal(self) -> Decimal:
        """The amount as a decimal, for rendering into a rail message.

        Decimal, never float. ``123456 / 100`` is 1234.56 in float only by
        accident of this particular value, and the accident does not hold for
        every amount.

        Quantized to two places so a round number keeps its cents: without it,
        482000 cents renders as ``4820`` rather than ``4820.00``, and a
        counterparty's schema validator is entitled to reject that.
        """
        return (Decimal(self.amount_cents) / Decimal(100)).quantize(Decimal("0.01"))

    def semantic_key(self) -> dict[str, object]:
        """The case's content, excluding when we happened to receive it.

        Two parses of the same message five milliseconds apart are the same
        case, but they differ in ``received_at``. Comparing them directly is a
        test that fails for a reason nobody cares about, so semantic comparison
        goes through here.
        """
        return self.model_dump(exclude={"received_at"})


def cents_from_decimal(value: Decimal | str) -> int:
    """Parse a rail's decimal amount into integer cents, exactly.

    Raises rather than rounds. A third decimal place in a USD amount means the
    counterparty sent something we do not understand, and quietly dropping it
    would put a discrepancy into a recovery claim.
    """
    amount = Decimal(value) if isinstance(value, str) else value
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError(
            f"Amount {amount} does not resolve to whole cents; refusing to round a figure "
            "that will be quoted in a recovery claim"
        )
    return int(cents)
