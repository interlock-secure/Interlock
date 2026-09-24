"""Shared types and enumerations for the Interlock wire protocol.

Everything in this package crosses an institutional boundary, which drives two
rules that are enforced here rather than left to reviewer discipline:

1. Money is integer cents, strictly typed. A float amount is rejected, not
   silently coerced, because a rounding error in a fraud hold is a customer
   complaint and a regulatory question.
2. Account identifiers are opaque hashes. The raw identifier never leaves the
   institution that holds it, so the type system refuses to carry one.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

# --------------------------------------------------------------------------
# Scalar types
# --------------------------------------------------------------------------

# Strict: a float is a validation error, not a value to round. Pydantic would
# happily coerce 4200.0 -> 4200 without strict=True, and would coerce 4200.7
# into 4200 while discarding the seven cents.
AmountCents = Annotated[int, Field(strict=True, ge=0, le=10_000_00_000)]
"""A monetary amount in integer cents. Upper bound is the $10 million
transaction cap both FedNow and RTP raised to in 2025."""

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

AccountHash = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$", strip_whitespace=True),
]
"""A salted SHA-256 digest of an account identifier, lowercase hex.

The pattern is load-bearing. A raw account number, an IBAN, or an email address
cannot satisfy it, so a developer who accidentally passes the real identifier
gets a validation error at the boundary instead of a privacy incident.
"""

InstitutionId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$", strip_whitespace=True),
]
"""A network-assigned participant identifier. Not a routing number: routing
numbers are public but they identify a real institution to anyone who sees a
captured message, and the network assigns its own opaque handles instead."""

CaseReference = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, strip_whitespace=True),
]
"""The sending institution's own case number. Opaque to the network - we never
parse it, we only carry it so both sides can reconcile against their own
systems."""


def utc_now() -> datetime:
    """Timezone-aware UTC now.

    Every timestamp on the wire is UTC and explicitly aware. A naive datetime
    in a network that spans institutions is a defect waiting for a daylight
    saving transition.
    """
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# Account hashing
# --------------------------------------------------------------------------

_ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

_RAW_IDENTIFIER_PATTERNS = (
    # Unanchored on purpose. The interesting case is not a field whose entire
    # value is an account number - the AccountHash type already refuses those -
    # but a number pasted mid-sentence into a free text field by a human.
    re.compile(r"\b\d{7,17}\b"),
    re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b", re.IGNORECASE),  # IBAN
    re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+"),  # email
)


def hash_account(raw_identifier: str, *, network_salt: str) -> str:
    """Hash a raw account identifier for transmission.

    HMAC-SHA256 with a network-wide salt, so the same account produces the same
    hash at every participant - which is the whole point, since the prior-flags
    dimension depends on two institutions independently arriving at the same
    value for the same account.

    The salt is a network parameter, not a per-institution secret. It exists to
    stop an observer with a list of candidate account numbers from confirming
    membership by brute force, which an unsalted SHA-256 over a 10-digit space
    would permit in seconds.

    The standard and the rotation policy are an open question in the
    specification (PRD Section 14). This is a working reference implementation,
    not a ratified scheme, and the network cannot go to production until
    participants agree on one - rotating the salt re-keys every stored hash
    across every participant simultaneously.
    """
    if not raw_identifier or not raw_identifier.strip():
        raise ValueError("Cannot hash an empty account identifier")
    if not network_salt:
        raise ValueError("A network salt is required; unsalted hashes are brute-forceable")

    return hmac.new(
        network_salt.encode("utf-8"),
        raw_identifier.strip().encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def looks_like_raw_identifier(value: str) -> bool:
    """True if a string looks like an un-hashed account identifier.

    Used by the boundary guard in interlock.schema.pii.

    Two deliberate limits, stated rather than discovered later:

    ISO timestamps are skipped. A microsecond component is six digits with
    non-word characters either side, which is indistinguishable from a short
    account number by pattern alone.

    The digit threshold is seven, not six, for the same reason. US account
    numbers run to roughly eight to twelve digits so this covers the realistic
    range, but a six-digit identifier would pass unnoticed. The alternative -
    flagging every timestamp - produces a guard nobody trusts, and a guard
    nobody trusts gets switched off.
    """
    candidate = value.strip()
    if _ISO_TIMESTAMP.match(candidate):
        return False
    return any(p.search(candidate) for p in _RAW_IDENTIFIER_PATTERNS)


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class Rail(StrEnum):
    """The US payment rails a return-of-funds request can arrive on.

    ACH is here despite not being an instant rail, and it is not an
    afterthought: it carries the only mandatory response obligation of the four
    (Nacha, ten banking days, since April 2025) and the weakest format, which is
    exactly the asymmetry Interlock exists to absorb. A system that handled only
    the instant rails would miss the one rail where the obligation is real.

    FEDWIRE is declared but has no adapter in M3. It shares camt.056 and
    camt.029 with FedNow, so adding it later is cheap; declaring it now keeps
    the capability matrix honest about what exists rather than about what we
    have got round to.
    """

    FEDNOW = "fednow"
    RTP = "rtp"
    ACH = "ach"
    FEDWIRE = "fedwire"

    @property
    def is_instant(self) -> bool:
        """True for rails that settle in seconds.

        Drives nothing about deadlines - the deadline comes from the capability
        matrix in interlock.schema.rails, never from this flag - but does drive
        how urgently a case is worth working, since the funds decay faster.
        """
        return self in {Rail.FEDNOW, Rail.RTP}


class RiskBand(StrEnum):
    """The receiving institution's assessment of a destination account.

    Bands rather than a numeric score, deliberately. A score invites the
    sending institution to treat another institution's model output as
    comparable to its own, which it is not - the two are trained on different
    populations with different labels. Bands carry the decision-relevant
    information without implying a false precision.
    """

    LOW = "low"
    ELEVATED = "elevated"
    HIGH = "high"

    NO_SIGNAL = "no_signal"
    """We reached the receiving institution and it has nothing on this account.

    Distinct from LOW. A genuinely new or dormant account produces no signal,
    and reporting that as low risk would present absence of evidence as
    evidence of absence - the exact failure this network exists to remove.
    """

    UNAVAILABLE = "unavailable"
    """We could not get an answer: timeout, unreachable participant, or budget
    exceeded. Distinct from NO_SIGNAL, which is a real answer."""

    @property
    def is_actionable(self) -> bool:
        """True if this band reflects an assessment rather than its absence."""
        return self in {RiskBand.LOW, RiskBand.ELEVATED, RiskBand.HIGH}


class SignalDimension(StrEnum):
    """What the receiving institution contributed to its assessment.

    A category rather than a required field, so an institution contributes what
    its own engine actually produces instead of being forced into a model shape
    it does not have. Specification Section 5.
    """

    ACCOUNT_TENURE = "account_tenure"
    INBOUND_VELOCITY_ANOMALY = "inbound_velocity_anomaly"
    ONWARD_MOVEMENT_PATTERN = "onward_movement_pattern"
    PRIOR_NETWORK_FLAGS = "prior_network_flags"
    SENDING_SIDE_CONTEXT = "sending_side_context"


class SenderContextFlag(StrEnum):
    """What the sending institution observed about the payment.

    This is the reverse direction of the exchange: it lets the receiving
    institution learn why an inbound payment is suspect, which it cannot derive
    from its own data alone.
    """

    FIRST_TIME_PAYEE = "first_time_payee"
    AMOUNT_ANOMALOUS_FOR_CUSTOMER = "amount_anomalous_for_customer"
    PAYEE_ADDED_THIS_SESSION = "payee_added_this_session"
    CUSTOMER_OVERRODE_WARNING = "customer_overrode_warning"
    ACCOUNT_RECENTLY_ACCESSED_FROM_NEW_DEVICE = "account_recently_accessed_from_new_device"


class ScamCategory(StrEnum):
    """Claimed scam type on a recall request.

    Categories follow the breakdown both UK Finance and the FTC report against,
    so benchmark results can be compared to published loss statistics rather
    than to a taxonomy we invented.
    """

    INVESTMENT = "investment"
    ROMANCE = "romance"
    IMPERSONATION = "impersonation"
    PURCHASE = "purchase"
    INVOICE = "invoice"
    ADVANCE_FEE = "advance_fee"
    JOB_OFFER = "job_offer"
    OTHER = "other"


class EventType(StrEnum):
    """Every event that writes to the audit chain.

    All Section 9 metrics are computed from these events alone, so that both
    institutions and the network operator read identical numbers from the same
    source rather than reconciling separate counters.
    """

    SIGNAL_REQUESTED = "signal_requested"
    SIGNAL_RETURNED = "signal_returned"
    SIGNAL_UNAVAILABLE = "signal_unavailable"
    DECISION_RECORDED = "decision_recorded"
    PAYMENT_HELD = "payment_held"
    PAYMENT_RELEASED = "payment_released"
    NETWORK_DEGRADED = "network_degraded"
    RECALL_RAISED = "recall_raised"
    RECALL_ACKNOWLEDGED = "recall_acknowledged"
    RECALL_DISPOSED = "recall_disposed"
    SLA_BREACHED = "sla_breached"
    BENCHMARK_RUN_RECORDED = "benchmark_run_recorded"


class PaymentDecision(StrEnum):
    """What the sending institution did with the payment.

    Interlock never makes this decision. It records it, because the audit chain
    has to be able to answer an examiner asking why a specific payment was
    held, and the answer requires both the signal and the action taken on it.
    """

    PASSED = "passed"
    WARNED = "warned"
    HELD = "held"
