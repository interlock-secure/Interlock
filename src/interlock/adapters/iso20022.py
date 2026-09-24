"""camt.056 and camt.029 - the instant rails' return request and its answer.

One module for both, because they are two halves of one exchange and splitting
them puts the namespace handling and the canonical writer in two places where
they drift apart.

FedNow, RTP and (since 14 July 2025) Fedwire all use these two messages. FedNow
does not use the ISO canonical names anywhere in its own documentation - it
calls them Return Request and Return Request Response, never
FIToFIPaymentCancellationRequest or ResolutionOfInvestigation. The element names
below are ISO's; the vocabulary a FedNow integration document uses is not.

A parsing hazard worth stating once
-----------------------------------
camt.029 is overloaded on FedNow. The same message answers a Return Request, an
RFP Cancellation Request (camt.055) and an Information Request (camt.026). You
cannot infer intent from the message type. This adapter resolves it by matching
the case identification back to an outstanding request, and refuses rather than
guesses when it cannot - see :meth:`Camt029Adapter.parse`.

Security
--------
Parsing uses :mod:`defusedxml`. These messages arrive from outside, and stdlib
``xml.etree`` will happily expand a billion-laughs entity bomb. This is not
hypothetical caution: the input is an untrusted network message from a
counterparty we do not control.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from xml.etree.ElementTree import Element

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import ParseError
from defusedxml.ElementTree import fromstring as defused_fromstring

from interlock.adapters.base import (
    LossyRoundTripError,
    MalformedMessageError,
    UnsupportedMessageError,
)
from interlock.adapters.codes import (
    CAMT029_STATUS_CODES,
    CANONICAL_TO_CAMT_REASON,
    canonical_reason_from_camt,
)
from interlock.schema.case import (
    Channel,
    Direction,
    NativeEnvelope,
    RecallCase,
    cents_from_decimal,
)
from interlock.schema.common import Rail

CAMT056_NS = "urn:iso:std:iso:20022:tech:xsd:camt.056.001.08"
CAMT029_NS = "urn:iso:std:iso:20022:tech:xsd:camt.029.001.09"

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    """Strip the namespace from an element tag.

    Matching on local names rather than fully-qualified ones is deliberate.
    Counterparties differ in which minor version of a camt schema they send, and
    a parser keyed to one exact namespace URI rejects messages it understands
    perfectly well. Version differences that actually matter show up as missing
    elements, which are caught below with a useful error.
    """
    return tag.rsplit("}", 1)[-1]


def _find(parent: Element, *path: str) -> Element | None:
    """Walk a path of local element names, returning None if it breaks."""
    node: Element | None = parent
    for name in path:
        if node is None:
            return None
        node = next((child for child in node if _local(child.tag) == name), None)
    return node


def _require(parent: Element, *path: str) -> Element:
    """Walk a path, raising a message naming what was missing."""
    node = _find(parent, *path)
    if node is None:
        raise MalformedMessageError(f"Required element {'/'.join(path)} is missing")
    return node


def _text(parent: Element, *path: str) -> str | None:
    node = _find(parent, *path)
    if node is None or node.text is None:
        return None
    return node.text.strip()


def _require_text(parent: Element, *path: str) -> str:
    value = _text(parent, *path)
    if not value:
        raise MalformedMessageError(f"Required element {'/'.join(path)} is empty or missing")
    return value


def _parse_timestamp(value: str) -> datetime:
    """Parse an ISO 20022 timestamp, normalising to aware UTC.

    ISO 20022 permits an offset, a trailing Z, or neither. A naive timestamp is
    read as UTC because that is what every US rail specifies, but it is worth
    knowing that this is the assumption which produces the
    ``victim_reported_at`` before ``original_settled_at`` failures that
    :class:`~interlock.schema.case.RecallCase` checks for - a counterparty
    sending local time lands here.
    """
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise MalformedMessageError(f"Unparseable timestamp {value!r}") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    """Render a timestamp in the canonical form this module emits.

    Seconds precision with a trailing Z. ISO 20022 allows fractional seconds;
    we do not emit them, because two systems disagreeing about whether to write
    three or six decimal places is a classic source of "identical" messages that
    do not compare equal.
    """
    return value.astimezone(UTC).strftime(_ISO_FORMAT) + "Z"


def _escape(value: str) -> str:
    """XML-escape a text value.

    Hand-rolled because the canonical writer below builds strings directly. The
    ampersand must be replaced first or it double-escapes the entities produced
    by the later replacements.
    """
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _rail_from_native(case: RecallCase) -> Rail:
    if case.rail not in {Rail.FEDNOW, Rail.RTP, Rail.FEDWIRE}:
        raise LossyRoundTripError(
            f"{case.rail.value} does not use ISO 20022 return messages; "
            "this case belongs to another adapter"
        )
    return case.rail


# ---------------------------------------------------------------------------
# camt.056 - the request
# ---------------------------------------------------------------------------


class Camt056Adapter:
    """Return Request. The sending institution asking for money back."""

    channel = Channel.CAMT_056

    def __init__(self, rail: Rail = Rail.FEDNOW) -> None:
        if rail not in {Rail.FEDNOW, Rail.RTP, Rail.FEDWIRE}:
            raise ValueError(f"camt.056 is not used on {rail.value}")
        self.rail = rail

    # -- parse -------------------------------------------------------------

    def parse(self, raw: bytes) -> RecallCase:
        """Translate a camt.056 into a canonical case.

        The case is always INBOUND. An institution parsing a camt.056 is by
        definition the one being asked, so direction is a property of who is
        running the parser, not of anything in the message.
        """
        try:
            root = defused_fromstring(raw)
        except (DefusedXmlException, ParseError, ValueError) as exc:
            # Named, not `except Exception`. A blanket catch here turned
            # MemoryError and RecursionError into "not well-formed XML",
            # which misdiagnoses a resource-exhaustion attack as a typo.
            raise MalformedMessageError(f"Not well-formed XML: {exc}") from exc

        # Some senders wrap the Document in an outer envelope and some do not,
        # so search from the root either way rather than assuming a level.
        envelope = _find(root, "FIToFIPmtCxlReq")
        if envelope is None:
            raise UnsupportedMessageError(
                "No FIToFIPmtCxlReq element; this is not a camt.056 return request"
            )

        assignment = _require(envelope, "Assgnmt")
        message_id = _require_text(assignment, "Id")
        creation_time = _parse_timestamp(_require_text(assignment, "CreDtTm"))

        requesting = _require_text(assignment, "Assgnr", "Agt", "FinInstnId", "Othr", "Id")
        responding = _require_text(assignment, "Assgne", "Agt", "FinInstnId", "Othr", "Id")

        underlying = _require(envelope, "Undrlyg")
        tx_info = _require(underlying, "TxInf")

        case_id = _require_text(tx_info, "CxlId")
        original_reference = _require_text(tx_info, "OrgnlEndToEndId")

        amount_node = _require(tx_info, "OrgnlIntrBkSttlmAmt")
        currency = (amount_node.get("Ccy") or "USD").strip().upper()
        if amount_node.text is None:
            raise MalformedMessageError("OrgnlIntrBkSttlmAmt has no value")
        try:
            amount_cents = cents_from_decimal(Decimal(amount_node.text.strip()))
        except (ValueError, ArithmeticError) as exc:
            raise MalformedMessageError(f"Unusable settlement amount: {exc}") from exc

        settled_raw = _text(tx_info, "OrgnlIntrBkSttlmDt")
        settled_at = _parse_timestamp(settled_raw) if settled_raw else None

        reason_code = _require_text(tx_info, "CxlRsnInf", "Rsn", "Cd")

        extra: dict[str, str] = {}
        narrative = _text(tx_info, "CxlRsnInf", "AddtlInf")
        if narrative:
            extra["additional_information"] = narrative

        return RecallCase(
            case_id=case_id,
            rail=self.rail,
            direction=Direction.INBOUND,
            channel=Channel.CAMT_056,
            original_payment_reference=original_reference,
            amount_cents=amount_cents,
            currency=currency,
            reason=canonical_reason_from_camt(reason_code),
            requesting_institution_id=requesting,
            responding_institution_id=responding,
            original_settled_at=settled_at,
            native=NativeEnvelope(
                message_id=message_id,
                reason_code=reason_code,
                creation_time=creation_time,
                extra=extra,
            ),
        )

    # -- emit --------------------------------------------------------------

    def emit(self, case: RecallCase) -> bytes:
        """Render a canonical case as camt.056, in canonical form.

        The reason code comes from ``native.reason_code`` when the case arrived
        as a camt message, and only falls back to the canonical mapping for
        cases Interlock originated. The mapping cannot distinguish the two fraud
        reasons - both emit FRAD - so using it on a parsed case would rewrite
        the counterparty's own code.
        """
        _rail_from_native(case)

        if case.native.creation_time is None:
            raise LossyRoundTripError(
                "Cannot emit camt.056 without a creation time; re-generating one makes a "
                "re-sent message indistinguishable from a new request"
            )

        reason_code = (
            case.native.reason_code
            if case.channel is Channel.CAMT_056
            else CANONICAL_TO_CAMT_REASON[case.reason]
        )

        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<Document xmlns="{CAMT056_NS}">',
            "  <FIToFIPmtCxlReq>",
            "    <Assgnmt>",
            f"      <Id>{_escape(case.native.message_id)}</Id>",
            "      <Assgnr>",
            "        <Agt>",
            "          <FinInstnId>",
            "            <Othr>",
            f"              <Id>{_escape(case.requesting_institution_id)}</Id>",
            "            </Othr>",
            "          </FinInstnId>",
            "        </Agt>",
            "      </Assgnr>",
            "      <Assgne>",
            "        <Agt>",
            "          <FinInstnId>",
            "            <Othr>",
            f"              <Id>{_escape(case.responding_institution_id)}</Id>",
            "            </Othr>",
            "          </FinInstnId>",
            "        </Agt>",
            "      </Assgne>",
            f"      <CreDtTm>{_format_timestamp(case.native.creation_time)}</CreDtTm>",
            "    </Assgnmt>",
            "    <Undrlyg>",
            "      <TxInf>",
            f"        <CxlId>{_escape(case.case_id)}</CxlId>",
            f"        <OrgnlEndToEndId>{_escape(case.original_payment_reference)}</OrgnlEndToEndId>",
            f'        <OrgnlIntrBkSttlmAmt Ccy="{case.currency}">'
            f"{case.amount_decimal():.2f}</OrgnlIntrBkSttlmAmt>",
        ]

        if case.original_settled_at is not None:
            lines.append(
                f"        <OrgnlIntrBkSttlmDt>"
                f"{_format_timestamp(case.original_settled_at)}</OrgnlIntrBkSttlmDt>"
            )

        lines += [
            "        <CxlRsnInf>",
            "          <Rsn>",
            f"            <Cd>{_escape(reason_code)}</Cd>",
            "          </Rsn>",
        ]

        narrative = case.native.extra.get("additional_information")
        if narrative:
            lines.append(f"          <AddtlInf>{_escape(narrative)}</AddtlInf>")

        lines += [
            "        </CxlRsnInf>",
            "      </TxInf>",
            "    </Undrlyg>",
            "  </FIToFIPmtCxlReq>",
            "</Document>",
            "",
        ]

        return "\n".join(lines).encode("utf-8")


# ---------------------------------------------------------------------------
# camt.029 - the answer
# ---------------------------------------------------------------------------


class Camt029Adapter:
    """Return Request Response. What the receiving institution decided.

    Parsing this one needs the case it answers, because of the overloading noted
    in the module docstring: on FedNow the same message type also answers
    camt.055 and camt.026, and nothing inside it reliably says which. The
    adapter therefore takes the outstanding case and refuses to guess when the
    identifiers do not line up.
    """

    channel = Channel.CAMT_029

    def __init__(self, rail: Rail = Rail.FEDNOW) -> None:
        if rail not in {Rail.FEDNOW, Rail.RTP, Rail.FEDWIRE}:
            raise ValueError(f"camt.029 is not used on {rail.value}")
        self.rail = rail

    def parse(self, raw: bytes, *, against: RecallCase) -> tuple[str, str | None]:
        """Read the status and reason out of a response.

        Returns the status code and its optional reason, rather than a case.
        A camt.029 is not a case - it is an event on one - and returning a
        half-populated :class:`RecallCase` here would invite callers to treat
        the response as though it were the request.

        M4 turns this into a disposition on the ledger. M3 only reads it.

        Raises:
            UnsupportedMessageError: the response does not refer to ``against``,
                which on FedNow usually means it answers a camt.055 or camt.026
                instead.
        """
        try:
            root = defused_fromstring(raw)
        except (DefusedXmlException, ParseError, ValueError) as exc:
            raise MalformedMessageError(f"Not well-formed XML: {exc}") from exc

        envelope = _find(root, "RsltnOfInvstgtn")
        if envelope is None:
            raise UnsupportedMessageError(
                "No RsltnOfInvstgtn element; this is not a camt.029 response"
            )

        case_id = _require_text(envelope, "CxlDtls", "TxInfAndSts", "CxlStsId")
        if case_id != against.case_id:
            raise UnsupportedMessageError(
                f"camt.029 answers case {case_id!r}, not {against.case_id!r}. On FedNow this "
                "message type also answers camt.055 and camt.026, so a mismatch is more "
                "likely a misrouted response than a corrupt one - do not treat it as this "
                "case's disposition"
            )

        status = _require_text(envelope, "CxlDtls", "TxInfAndSts", "CxlStsRsnInf", "Rsn", "Cd")
        if status not in CAMT029_STATUS_CODES:
            raise MalformedMessageError(
                f"Unknown investigation status {status!r}; expected one of "
                f"{sorted(CAMT029_STATUS_CODES)}"
            )

        return status, _text(envelope, "CxlDtls", "TxInfAndSts", "CxlStsRsnInf", "AddtlInf")

    def emit(self, case: RecallCase, *, status: str, reason: str | None = None) -> bytes:
        """Render a response to a case.

        Raises:
            LossyRoundTripError: for a status outside the known set, rather than
                emitting a code a counterparty cannot act on.
        """
        _rail_from_native(case)

        if status not in CAMT029_STATUS_CODES:
            raise LossyRoundTripError(
                f"Refusing to emit unknown investigation status {status!r}; expected one of "
                f"{sorted(CAMT029_STATUS_CODES)}"
            )

        if case.native.creation_time is None:
            raise LossyRoundTripError("Cannot emit camt.029 without a creation time")

        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<Document xmlns="{CAMT029_NS}">',
            "  <RsltnOfInvstgtn>",
            "    <Assgnmt>",
            f"      <Id>{_escape(case.native.message_id)}</Id>",
            "      <Assgnr>",
            "        <Agt>",
            "          <FinInstnId>",
            "            <Othr>",
            f"              <Id>{_escape(case.responding_institution_id)}</Id>",
            "            </Othr>",
            "          </FinInstnId>",
            "        </Agt>",
            "      </Assgnr>",
            "      <Assgne>",
            "        <Agt>",
            "          <FinInstnId>",
            "            <Othr>",
            f"              <Id>{_escape(case.requesting_institution_id)}</Id>",
            "            </Othr>",
            "          </FinInstnId>",
            "        </Agt>",
            "      </Assgne>",
            f"      <CreDtTm>{_format_timestamp(case.native.creation_time)}</CreDtTm>",
            "    </Assgnmt>",
            "    <CxlDtls>",
            "      <TxInfAndSts>",
            f"        <CxlStsId>{_escape(case.case_id)}</CxlStsId>",
            "        <CxlStsRsnInf>",
            "          <Rsn>",
            f"            <Cd>{_escape(status)}</Cd>",
            "          </Rsn>",
        ]

        if reason:
            lines.append(f"          <AddtlInf>{_escape(reason)}</AddtlInf>")

        lines += [
            "        </CxlStsRsnInf>",
            "      </TxInfAndSts>",
            "    </CxlDtls>",
            "  </RsltnOfInvstgtn>",
            "</Document>",
            "",
        ]

        return "\n".join(lines).encode("utf-8")
