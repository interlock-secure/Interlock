"""Reason code translation between each rail's vocabulary and ours.

Kept in one module rather than inside each adapter, because the interesting
property is a relationship *between* rails: FRAD on RTP, "False Pretenses" on
ACH and a scam narrative in an email are the same business fact, and a reader
should be able to see that in one place.

On the direction of truth
-------------------------
Mapping in is lossy and mapping out is not a reversal of it. Several native
codes collapse to one canonical reason, so emitting cannot recover the original
from the canonical value alone - it reads ``native.reason_code`` instead. That
is why :class:`~interlock.schema.case.NativeEnvelope` exists, and why these
tables are deliberately not written as a single bidirectional dict.
"""

from __future__ import annotations

from interlock.schema.case import RecallReason

# ---------------------------------------------------------------------------
# ISO 20022 - camt.056 cancellation reasons
# ---------------------------------------------------------------------------

CAMT_REASON_TO_CANONICAL: dict[str, RecallReason] = {
    "FRAD": RecallReason.FRAUD_SCAM,
    "DUPL": RecallReason.DUPLICATE,
    "TECH": RecallReason.TECHNICAL_ERROR,
    "AM09": RecallReason.WRONG_AMOUNT,
    "AGNT": RecallReason.WRONG_BENEFICIARY,
    "CUST": RecallReason.CUSTOMER_REQUEST,
    "CURR": RecallReason.TECHNICAL_ERROR,
    "COVR": RecallReason.TECHNICAL_ERROR,
    "CUTA": RecallReason.TECHNICAL_ERROR,
    "UPAY": RecallReason.FRAUD_SCAM,
    "NARR": RecallReason.UNKNOWN,
}
"""camt.056 ``CxlRsnInf/Rsn/Cd`` values.

Two mappings deserve their reasoning stated, because both are judgement calls a
reviewer should be able to challenge:

``FRAD`` maps to FRAUD_SCAM rather than FRAUD_UNAUTHORISED. ISO's "fraudulent
origin" does not distinguish a deceived customer from a compromised account, and
on the instant rails the overwhelming majority of post-settlement recalls are
the former. Where the distinction matters legally, the sending institution's
narrative is the evidence, not this code.

``NARR`` maps to UNKNOWN deliberately. It means the reason is in a free-text
field, so any canonical value would be a guess dressed up as a fact. UNKNOWN
routes the case to something that reads the narrative.
"""

CANONICAL_TO_CAMT_REASON: dict[RecallReason, str] = {
    RecallReason.FRAUD_SCAM: "FRAD",
    RecallReason.FRAUD_UNAUTHORISED: "FRAD",
    RecallReason.DUPLICATE: "DUPL",
    RecallReason.TECHNICAL_ERROR: "TECH",
    RecallReason.WRONG_AMOUNT: "AM09",
    RecallReason.WRONG_BENEFICIARY: "AGNT",
    RecallReason.CUSTOMER_REQUEST: "CUST",
    RecallReason.UNKNOWN: "NARR",
}
"""Canonical reason to camt.056 code, for cases we originate.

Only used when Interlock composes a request from scratch - an outbound case
raised by our own customer's scam report. When re-emitting a case that arrived
as camt.056, the adapter uses ``native.reason_code`` instead, because this table
cannot distinguish the two fraud reasons and would silently rewrite one.
"""

# ---------------------------------------------------------------------------
# ISO 20022 - camt.029 investigation status
# ---------------------------------------------------------------------------

CAMT029_STATUS_CODES: frozenset[str] = frozenset({"CNCL", "PDCR", "RJCR", "PTNA"})
"""Valid ``CxlDtls/CxlStsRsnInf`` outcomes.

CNCL cancelled, PDCR pending, RJCR rejected, PTNA passed to next agent.

PDCR is the one to watch. It is a non-answer that satisfies a response
obligation, and a counterparty that returns PDCR has stopped its own clock
without deciding anything. M4 must not treat it as a terminal disposition, which
is the whole reason ``RecallDispositionCode`` has no PENDING member.
"""

# ---------------------------------------------------------------------------
# ACH - Nacha return reason codes
# ---------------------------------------------------------------------------

ACH_CODE_TO_CANONICAL: dict[str, RecallReason] = {
    "R06": RecallReason.UNKNOWN,
    "R10": RecallReason.FRAUD_UNAUTHORISED,
    "R11": RecallReason.WRONG_AMOUNT,
    "R07": RecallReason.CUSTOMER_REQUEST,
    "R17": RecallReason.FRAUD_UNAUTHORISED,
    "R05": RecallReason.FRAUD_UNAUTHORISED,
    "R51": RecallReason.TECHNICAL_ERROR,
}
"""Nacha codes to canonical reasons.

``R06`` - "Return per ODFI's Request" - maps to UNKNOWN on purpose, and this is
the most important entry in the module. R06 says only that the originating
institution asked; it carries no reason at all. Since October 2024 an ODFI may
request a return "for any reason", which is exactly what makes scam cases
possible on ACH and exactly what makes the code uninformative. The reason lives
in the accompanying letter of indemnity, which Nacha distributes as a *PDF*.

So on the rail with the strongest response obligation in US payments, the
machine-readable artifact cannot say why the money is wanted back. That is not a
gap in this mapping table. That is the gap the product exists to fill.

``R11`` is narrower than it looks: the authorisation exists but the debit does
not match its terms. Wrong amount is the commonest case and the closest
canonical fit, but a case arriving as R11 deserves a human reading the detail.

``R05`` and ``R51`` are carried at lower confidence - their exact titles and
windows were not verified against nacha.org, only against general knowledge.
Neither drives a deadline, so the risk is confined to labelling.
"""

CANONICAL_TO_ACH_CODE: dict[RecallReason, str] = {
    RecallReason.FRAUD_SCAM: "R06",
    RecallReason.FRAUD_UNAUTHORISED: "R10",
    RecallReason.DUPLICATE: "R06",
    RecallReason.TECHNICAL_ERROR: "R06",
    RecallReason.WRONG_AMOUNT: "R11",
    RecallReason.WRONG_BENEFICIARY: "R06",
    RecallReason.CUSTOMER_REQUEST: "R07",
    RecallReason.UNKNOWN: "R06",
}
"""Canonical reason to the ACH code we would originate under.

Note how much collapses onto R06. That is faithful to the rail rather than lazy:
a scam claim, a duplicate and a misdirected payment are all simply "the ODFI
asked", and the distinction travels in the indemnity letter instead.
"""

FALSE_PRETENSES_CODES: frozenset[str] = frozenset({"R06"})
"""ACH codes that can carry a False Pretenses claim.

Nacha defined False Pretenses into the Rules effective 1 October 2024 as the
inducement of a payment by misrepresenting identity, authority to act, or
ownership of an account to be credited. It covers business email compromise,
vendor impersonation and payroll fraud, and explicitly excludes disputes about
the quality of goods or services.

It is a definition, not a return code - which is why a scam claim on ACH has to
travel as R06 plus a narrative.
"""


def canonical_reason_from_camt(code: str) -> RecallReason:
    """Map a camt.056 reason code, defaulting to UNKNOWN for anything unlisted.

    Unknown codes are not an error. The ISO external code sets are gated, this
    table was built from the subset the research could verify, and a code we
    have not seen is far more likely to be a real code we could not read than a
    corrupt message. Refusing the case would lose it; UNKNOWN keeps it and flags
    that the native code is the only reliable statement of why.
    """
    return CAMT_REASON_TO_CANONICAL.get(code.strip().upper(), RecallReason.UNKNOWN)


def canonical_reason_from_ach(code: str) -> RecallReason:
    """Map a Nacha return or request code, defaulting to UNKNOWN."""
    return ACH_CODE_TO_CANONICAL.get(code.strip().upper(), RecallReason.UNKNOWN)
