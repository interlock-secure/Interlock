"""The rail capability matrix: what each US rail obliges, and how well we know it.

This module is the single source of truth for every rail rule in Interlock.
Nothing anywhere else may hardcode a deadline, a window, or an obligation. A
test enforces that (``test_rails.py::TestNoRuleIsHardcodedElsewhere``).

Why a module and not a document
-------------------------------
The research behind this matrix turned up an asymmetry that is the product's
whole reason to exist:

    ACH has a mandatory response obligation and no machine-readable format.
    The instant rails have the format and no enforceable obligation.

If those rules live in prose, they get paraphrased into code slightly wrong and
nobody notices for a year. Here they are executable, and each one carries the
URL it came from.

Why provenance is a first-class field
-------------------------------------
Several of these rules could not be verified against a primary source, because
the FedNow Operating Procedures, the RTP Operating Rules and the Nacha Rules
Book are variously gated, robots-disallowed, or paywalled. That is a real
limitation of this project and pretending otherwise would be the worse
engineering choice.

So each rule carries a :class:`SourceConfidence`. Code that reads a rule which
is not ``CONFIRMED`` must go through :func:`require_verified_window`, which
raises :class:`UnverifiedRuleError` rather than quietly returning a number
somebody guessed. If you find yourself catching that exception to get past it,
you are about to ship a compliance bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from interlock.schema.common import Rail

# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class SourceConfidence(StrEnum):
    """How well established a rule is.

    The distinction that matters is between CONFIRMED and everything else.
    LIKELY is not "probably fine"; it means one credible source quoted the rule
    and we could not see the rule itself.
    """

    CONFIRMED = "confirmed"
    """Quoted from the primary source - the operating circular, the rule page,
    the Fed's own FAQ."""

    LIKELY = "likely"
    """A credible secondary source quoted the rule with a citation, but the
    primary text was not retrievable. Specific and internally consistent, but
    not seen."""

    UNVERIFIED = "unverified"
    """We could not establish this at all. Present in the matrix so the gap is
    visible rather than invisible. Never rely on it."""

    @property
    def is_safe_to_rely_on(self) -> bool:
        return self is SourceConfidence.CONFIRMED


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a rule came from and how far to trust it."""

    claim: str
    """The rule in one sentence, as close to the source's wording as possible."""

    source_url: str
    confidence: SourceConfidence
    retrieved: date

    note: str | None = None
    """Anything a reader needs in order not to misuse this. Usually the reason a
    confirmed-looking rule is not confirmed."""


class ResponseObligation(StrEnum):
    """Whether the receiving institution must answer a return request."""

    MANDATORY = "mandatory"
    """A rule requires an answer and silence is a violation. Only ACH."""

    ADVISORY = "advisory"
    """The rule says "should". FedNow's Operating Circular 8 wording."""

    NONE = "none"
    """No obligation is stated at all."""


class DispositionFormat(StrEnum):
    """Whether the rail has a machine-readable way to carry the answer."""

    STRUCTURED = "structured"
    """An ISO 20022 message - camt.029 on the instant rails and Fedwire."""

    UNSTRUCTURED = "unstructured"
    """No format. Nacha states the method is flexible: portal, phone, etc. The
    only structured artifact on ACH is the return entry itself, which can say
    "returned" and nothing else - not "funds gone", not "account closed", not
    "our customer disputes this"."""


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


class WindowUnit(StrEnum):
    """Banking days and wall-clock hours are not interchangeable.

    Ten banking days spans two weekends and any federal holidays in between.
    Conflating the two is the classic implementation bug in this domain, so the
    unit is carried explicitly and :mod:`interlock.sla.calendar` (M4) is the
    only thing allowed to resolve a banking-day window into a real instant.
    """

    BANKING_DAYS = "banking_days"
    HOURS = "hours"


@dataclass(frozen=True, slots=True)
class ResponseWindow:
    """How long the receiving institution has to answer."""

    amount: int
    unit: WindowUnit
    provenance: Provenance

    fraud_exempt: bool = False
    """True if fraud claims are carved out of this window.

    Reportedly the case on RTP, which would be a sharp irony: the one scenario
    where funds decay fastest is the one with no deadline. Flagged rather than
    relied upon - see the RTP entry below.
    """

    def as_timedelta(self) -> timedelta:
        """Only valid for wall-clock windows.

        Banking-day windows deliberately cannot be resolved here. They need a
        holiday calendar and a start instant, which is M4's job.
        """
        if self.unit is not WindowUnit.HOURS:
            raise ValueError(
                f"A {self.unit.value} window cannot become a timedelta without a banking "
                "calendar; use interlock.sla.calendar instead of guessing"
            )
        return timedelta(hours=self.amount)


class UnverifiedRuleError(RuntimeError):
    """Raised when code tries to depend on a rule we could not verify.

    Deliberately not catchable-by-accident: it is a RuntimeError rather than a
    ValueError so that a broad ``except ValueError`` around parsing does not
    swallow it.
    """


# ---------------------------------------------------------------------------
# House policy
# ---------------------------------------------------------------------------

HOUSE_POLICY_WINDOW_HOURS = 24
"""[ASSUMPTION] Our own deadline for rails that set none.

FedNow and Fedwire both say participants "should" respond and neither states a
window we could verify. Rather than treat that as "no deadline", Interlock
applies its own and labels it as ours.

Twenty-four hours, justified by the decay curve rather than by any rule: UK data
on mule accounts (RUSI, July 2025, Lloyds transaction data) found under 15% of
value remaining after 24 hours, so a deadline beyond that is answering a
question about money that has already gone.

This is our number. It has a rationale, not an authority. Anything that reports
it to a user must say so - see :attr:`RailProfile.window_is_house_policy`.
"""

_HOUSE_POLICY_PROVENANCE = Provenance(
    claim=(
        "No rail-established response window; Interlock applies a 24-hour house policy "
        "derived from the published mule-account decay curve."
    ),
    source_url="https://static.rusi.org/following-the-fraud-the-role-of-money-mules.pdf",
    confidence=SourceConfidence.CONFIRMED,
    retrieved=date(2026, 9, 18),
    note=(
        "CONFIRMED refers to the decay curve, which is published and UK-derived. The choice "
        "of 24 hours is ours. Do not present this as a rail requirement."
    ),
)


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RailProfile:
    """Everything Interlock knows about one rail's return-of-funds rules."""

    rail: Rail
    disposition_format: DispositionFormat
    obligation: ResponseObligation
    window: ResponseWindow
    window_is_house_policy: bool
    request_message: str
    """What the request is called on this rail, in the rail's own vocabulary."""

    response_message: str | None
    """What the answer is called, or None where the rail has no message for it."""

    rail_authority_url: str = ""
    """The rail's own governing document.

    Distinct from ``window.provenance.source_url``, and the distinction is not
    pedantic. Where a rail sets no window, the window's provenance points at
    the research behind our house policy - a UK mule-account paper - and a
    reader shown that as "the FedNow source" would reasonably conclude the
    Federal Reserve's rules come from a British research PDF. They do not, and
    this field is where the operating circular goes.
    """

    notes: tuple[str, ...] = ()

    @property
    def has_enforceable_deadline(self) -> bool:
        """True only where a rule obliges an answer within a stated window.

        Note that ``fraud_exempt`` defeats this: a window that exempts the fraud
        case is not an enforceable deadline for anything Interlock handles.
        """
        return (
            self.obligation is ResponseObligation.MANDATORY
            and not self.window_is_house_policy
            and not self.window.fraud_exempt
        )


RAIL_PROFILES: dict[Rail, RailProfile] = {
    # -----------------------------------------------------------------------
    Rail.ACH: RailProfile(
        rail=Rail.ACH,
        disposition_format=DispositionFormat.UNSTRUCTURED,
        obligation=ResponseObligation.MANDATORY,
        window=ResponseWindow(
            amount=10,
            unit=WindowUnit.BANKING_DAYS,
            provenance=Provenance(
                claim=(
                    "Regardless of whether the RDFI complies with the ODFI's request to return "
                    "the Entry, the RDFI must advise the ODFI of its decision or the status of "
                    "the request within ten (10) banking days of receipt."
                ),
                source_url="https://www.nacha.org/rules/risk-management-topic-april-1-2025",
                confidence=SourceConfidence.CONFIRMED,
                retrieved=date(2026, 9, 18),
            ),
        ),
        window_is_house_policy=False,
        request_message="R06 Request for Return",
        rail_authority_url="https://www.nacha.org/rules/risk-management-topic-april-1-2025",
        response_message=None,
        notes=(
            "The strongest response obligation of any US rail, and the only one where silence "
            "is itself a violation.",
            "Nacha does not prescribe a format: the method is flexible - portal, phone, etc. "
            "This is the gap Interlock fills on this rail.",
            "Since 1 October 2024 an ODFI may request a return for any reason, which brought "
            "scam cases formally into scope, and 'False Pretenses' entered the Rules as a "
            "defined term.",
        ),
    ),
    # -----------------------------------------------------------------------
    Rail.RTP: RailProfile(
        rail=Rail.RTP,
        disposition_format=DispositionFormat.STRUCTURED,
        obligation=ResponseObligation.MANDATORY,
        window=ResponseWindow(
            amount=10,
            unit=WindowUnit.BANKING_DAYS,
            fraud_exempt=True,
            provenance=Provenance(
                claim=(
                    "A Receiving Participant must send its Response to Request for Return of "
                    "Funds within ten banking days, except for requests sent due to claimed "
                    "fraud ('FRAD') or breach of a Request for Payment warranty ('UPAY'), for "
                    "which it may take longer while investigating."
                ),
                source_url="https://www.cuanswers.com/wp-content/uploads/RTP-Participant-Self-Audit-Guidebook.pdf",
                confidence=SourceConfidence.LIKELY,
                retrieved=date(2026, 9, 18),
                note=(
                    "Quoted as RTP Operating Rule VII.C.2 by a credit union vendor's self-audit "
                    "guidebook. The Clearing House's own Operating Rules PDF is "
                    "robots-disallowed and could not be read. The quote is rule-numbered and "
                    "internally consistent, which is why this is LIKELY rather than UNVERIFIED "
                    "- but it has not been seen at source. Verify before any production use."
                ),
            ),
        ),
        window_is_house_policy=False,
        request_message="camt.056 Request for Return of Funds",
        rail_authority_url="https://www.theclearinghouse.org/payment-systems/rtp/technical-documentation",
        response_message="camt.029 Response to Request for Return of Funds",
        notes=(
            "The fraud carve-out is the sharpest finding in the research and the least "
            "verified. Treat with suspicion: it means the fastest-decaying case is the one "
            "with no deadline.",
            "Because fraud is exempt, Interlock applies its house policy to FRAD cases on RTP "
            "and reports the rail window only for non-fraud requests.",
        ),
    ),
    # -----------------------------------------------------------------------
    Rail.FEDNOW: RailProfile(
        rail=Rail.FEDNOW,
        disposition_format=DispositionFormat.STRUCTURED,
        obligation=ResponseObligation.ADVISORY,
        window=ResponseWindow(
            amount=HOUSE_POLICY_WINDOW_HOURS,
            unit=WindowUnit.HOURS,
            provenance=_HOUSE_POLICY_PROVENANCE,
        ),
        window_is_house_policy=True,
        request_message="camt.056 Return Request",
        rail_authority_url="https://www.frbservices.org/binaries/content/assets/crsocms/resources/rules-regulations/062425-operating-circular-8.pdf",
        response_message="camt.029 Return Request Response",
        notes=(
            "Operating Circular 8 section 9.8.3: participants 'should' respond to Nonvalue "
            "Messages. Should, not shall. Section 9.6 makes returning discretionary, and "
            "9.8.6 disclaims any Reserve Bank obligation to act on a request.",
            "The only binding duty is section 9.8.4: coordinate and use reasonable efforts to "
            "aid the investigation. Unmeasurable, and with no stated penalty.",
            "The rail's own response timeframe lives in FedNow Operating Procedures section "
            "15.2, which could not be retrieved. Interlock therefore runs house policy here "
            "and says so, rather than inventing a rail deadline.",
            "FedNow does not use the ISO canonical names. It calls these Return Request and "
            "Return Request Response, never FIToFIPaymentCancellationRequest or "
            "ResolutionOfInvestigation. Expect the mismatch in any mapping documentation.",
            "camt.029 is overloaded on FedNow - it also answers camt.055 and camt.026. A "
            "parser must disambiguate by the underlying case, never by message type alone.",
        ),
    ),
    # -----------------------------------------------------------------------
    Rail.FEDWIRE: RailProfile(
        rail=Rail.FEDWIRE,
        disposition_format=DispositionFormat.STRUCTURED,
        obligation=ResponseObligation.ADVISORY,
        window=ResponseWindow(
            amount=HOUSE_POLICY_WINDOW_HOURS,
            unit=WindowUnit.HOURS,
            provenance=_HOUSE_POLICY_PROVENANCE,
        ),
        window_is_house_policy=True,
        request_message="camt.056",
        rail_authority_url="https://www.frbservices.org/resources/financial-services/wires/faq/iso-20022/format",
        response_message="camt.029",
        notes=(
            "Fedwire migrated to ISO 20022 on 14 July 2025 and gained camt.056 and camt.029. "
            "The Fed's wording is 'should send' and 'should respond'; no window is stated.",
            "Implementation warning from the Fed: using camt.110 to request a return 'may "
            "cause a rejection by a Fedwire receiver'. camt.110 is a cross-border construct "
            "and is the wrong tool here.",
            "No adapter in M3. Declared so the matrix is honest about the rail landscape.",
            "UCC Article 4A is why wire scam recovery is hard: a customer tricked into "
            "authorising has made an effective payment order under 4A-202(2) and bears the "
            "loss. Interlock records the request; it cannot change that allocation.",
        ),
    ),
}


# ---------------------------------------------------------------------------
# Accessors - the only sanctioned way to read a rule
# ---------------------------------------------------------------------------


def profile_for(rail: Rail) -> RailProfile:
    """The capability profile for a rail."""
    try:
        return RAIL_PROFILES[rail]
    except KeyError:  # pragma: no cover - unreachable while Rail and the matrix agree
        raise ValueError(f"No capability profile for rail {rail!r}") from None


def require_verified_window(rail: Rail, *, is_fraud_claim: bool) -> ResponseWindow:
    """The response window, refusing to answer where we do not actually know.

    Call this instead of reading ``profile.window`` whenever the answer will
    drive a deadline, an escalation, or anything a customer or examiner sees.

    Raises:
        UnverifiedRuleError: if the rule behind this window was not confirmed at
            a primary source. Do not catch this to get a number - either verify
            the rule or fall back to house policy explicitly, so that the
            fallback is visible in the code rather than hidden in an exception
            handler.
    """
    profile = profile_for(rail)
    window = profile.window

    if profile.window_is_house_policy:
        # There is no rail rule here to return. The window on the profile is
        # our own fallback, and handing it back from a function named
        # "require_verified" would let a caller present our preference to a
        # counterparty as their obligation - the precise confusion this whole
        # module exists to prevent.
        raise UnverifiedRuleError(
            f"{rail.value}: no rail response window could be established, so there is no "
            f"verified window to return. {window.provenance.note or ''} "
            f"Use house_policy_window() and label it as our policy."
        )

    if is_fraud_claim and window.fraud_exempt:
        raise UnverifiedRuleError(
            f"{rail.value}: fraud claims are reportedly carved out of the {window.amount} "
            f"{window.unit.value} window, so no rail deadline applies to this case. Use "
            f"house_policy_window() and label it as our policy. "
            f"Source: {window.provenance.source_url}"
        )

    if not window.provenance.confidence.is_safe_to_rely_on:
        raise UnverifiedRuleError(
            f"{rail.value}: the response window rests on a "
            f"{window.provenance.confidence.value} source and must not drive a deadline. "
            f"{window.provenance.note or ''} Source: {window.provenance.source_url}"
        )

    return window


def house_policy_window() -> ResponseWindow:
    """Interlock's own deadline, for rails and cases where no rail rule applies.

    Separate function rather than a silent fallback inside
    :func:`require_verified_window`, so that every use of our own number is
    visible at the call site.
    """
    return ResponseWindow(
        amount=HOUSE_POLICY_WINDOW_HOURS,
        unit=WindowUnit.HOURS,
        provenance=_HOUSE_POLICY_PROVENANCE,
    )


def rails_with_enforceable_deadlines() -> tuple[Rail, ...]:
    """Rails where a rule actually obliges an answer within a stated window.

    Currently exactly one, which is the finding this whole product rests on.
    """
    return tuple(
        sorted(
            (rail for rail, profile in RAIL_PROFILES.items() if profile.has_enforceable_deadline),
            key=lambda r: r.value,
        )
    )


def unverified_rules() -> tuple[tuple[Rail, Provenance], ...]:
    """Every rule in the matrix that is not confirmed at a primary source.

    Surfaced deliberately: the console renders this, the documentation renders
    this, and a reviewer can see the project's epistemic debt in one call
    instead of reading four docstrings.
    """
    return tuple(
        sorted(
            (
                (rail, profile.window.provenance)
                for rail, profile in RAIL_PROFILES.items()
                if not profile.window.provenance.confidence.is_safe_to_rely_on
            ),
            key=lambda pair: pair[0].value,
        )
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_matrix_markdown() -> str:
    """Render the matrix as the documentation table.

    Generated rather than hand-written, and a test asserts the checked-in file
    matches this output. Documentation that restates a data structure by hand
    goes stale within two commits, and a stale rail rule in a document somebody
    trusts is worse than no document.
    """
    header = (
        "| Rail | Disposition format | Response obligation | Window | Source confidence |\n"
        "|---|---|---|---|---|\n"
    )

    rows = []
    for rail in sorted(RAIL_PROFILES, key=lambda r: r.value):
        profile = RAIL_PROFILES[rail]
        window = profile.window

        descriptor = f"{window.amount} {window.unit.value.replace('_', ' ')}"
        if profile.window_is_house_policy:
            descriptor += " *(Interlock house policy - not a rail rule)*"
        if window.fraud_exempt:
            descriptor += " *(fraud claims reportedly exempt)*"

        # The confidence column describes how well established the RAIL's rule
        # is. Printing the house policy's own confidence here would read as
        # "FedNow's window is confirmed", which is the opposite of the truth -
        # the rail states no window we could find, which is why we invented one.
        confidence = (
            "n/a - no rail rule found"
            if profile.window_is_house_policy
            else window.provenance.confidence.value.upper()
        )

        rows.append(
            f"| **{rail.value}** "
            f"| {profile.disposition_format.value} "
            f"| {profile.obligation.value} "
            f"| {descriptor} "
            f"| {confidence} |"
        )

    lines = [
        "<!-- Generated by interlock.schema.rails.render_matrix_markdown(). Do not edit. -->",
        "",
        "# Rail capability matrix",
        "",
        "What each US payment rail obliges when someone asks for money back, and how",
        "well established each rule is. This table is generated from",
        "`src/interlock/schema/rails.py`, which is the single source of truth; a test",
        "fails if this file drifts from it.",
        "",
        header.rstrip("\n"),
        *rows,
        "",
        "## Why this table is the product",
        "",
        "No US rail has both a machine-readable disposition format and an enforceable",
        "obligation to provide one. ACH has the obligation and no format; the instant",
        "rails have the format and no enforceable obligation. A bank working across all",
        "four runs four incompatible processes for one business event.",
        "",
        "## Provenance",
        "",
    ]

    for rail in sorted(RAIL_PROFILES, key=lambda r: r.value):
        provenance = RAIL_PROFILES[rail].window.provenance
        lines += [
            f"### {rail.value}",
            "",
            f"> {provenance.claim}",
            "",
            f"- Source: {provenance.source_url}",
            f"- Confidence: **{provenance.confidence.value.upper()}**",
            f"- Retrieved: {provenance.retrieved.isoformat()}",
        ]
        if provenance.note:
            lines.append(f"- Note: {provenance.note}")
        lines.append("")

        for note in RAIL_PROFILES[rail].notes:
            lines.append(f"- {note}")
        if RAIL_PROFILES[rail].notes:
            lines.append("")

    return "\n".join(lines)
