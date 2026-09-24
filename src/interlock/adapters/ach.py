"""ACH - the rail with the obligation and no format.

Two very different inputs live here, and the difference between them is the
whole reason this product exists.

**The return entry** is real, standardised and fixed-width: a 94-character
Nacha addenda record, type 99, carrying a return reason code. It is machine
readable. What it can say is "returned", and nothing else - not *funds already
gone*, not *account closed*, not *our customer disputes the claim*.

**The request for return** has no standard format at all. Since 1 October 2024
an ODFI may request a return for any reason, which is what brought scam cases
into scope; since 1 April 2025 the RDFI must answer within ten banking days
whether or not it complies, which is the strongest response obligation on any US
rail. Nacha prescribes no format for either the request or the answer - the
method is flexible, portal or phone - and the supporting artifact is a
standardised *PDF* letter of indemnity.

So on the one rail where silence is a rules violation, there is nothing to parse.
The :class:`AchRequestAdapter` below therefore defines a structured form, and it
is **Interlock's own invention, not a standard**. Every place that could be
mistaken for a rail requirement says so.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from interlock.adapters.base import (
    LossyRoundTripError,
    MalformedMessageError,
    UnsupportedMessageError,
)
from interlock.adapters.codes import CANONICAL_TO_ACH_CODE, canonical_reason_from_ach
from interlock.schema.case import (
    Channel,
    Direction,
    NativeEnvelope,
    RecallCase,
)
from interlock.schema.common import Rail

# ---------------------------------------------------------------------------
# The return entry - a real Nacha record
# ---------------------------------------------------------------------------

ADDENDA_RECORD_LENGTH = 94
"""Every Nacha record is exactly 94 characters. Not a guideline - a file with a
short record is rejected by the operator, so the length is validated on both
parse and emit."""

_RECORD_TYPE_ADDENDA = "7"
_ADDENDA_TYPE_RETURN = "99"

# Field positions, zero-based half-open, from the Nacha addenda record layout.
_POS_RECORD_TYPE = (0, 1)
_POS_ADDENDA_TYPE = (1, 3)
_POS_RETURN_REASON = (3, 6)
_POS_ORIGINAL_TRACE = (6, 21)
_POS_DATE_OF_DEATH = (21, 27)
_POS_ORIGINAL_RDFI = (27, 35)
_POS_ADDENDA_INFO = (35, 79)
_POS_TRACE_NUMBER = (79, 94)


def _slice(record: str, span: tuple[int, int]) -> str:
    return record[span[0] : span[1]].strip()


class AchReturnEntryAdapter:
    """The 94-character return addenda record.

    Parses into a case for completeness of the queue - an unexpected return
    arriving is an event operations needs to see - but note what it cannot
    carry. There is no amount on this record and no institution identifiers
    beyond an eight-digit routing prefix, so a case built from one is
    deliberately sparse and the missing values are left absent rather than
    invented.
    """

    rail = Rail.ACH
    channel = Channel.ACH_RETURN_ENTRY

    def __init__(self, *, requesting_institution_id: str, responding_institution_id: str) -> None:
        # Not present on the record. The caller knows them from the file the
        # record arrived in; passing them explicitly is more honest than
        # parsing an eight-digit prefix and calling it an institution.
        self.requesting_institution_id = requesting_institution_id
        self.responding_institution_id = responding_institution_id

    def parse(self, raw: bytes) -> RecallCase:
        record = raw.decode("ascii", errors="strict").rstrip("\r\n")

        if len(record) != ADDENDA_RECORD_LENGTH:
            raise MalformedMessageError(
                f"Nacha addenda records are exactly {ADDENDA_RECORD_LENGTH} characters; "
                f"got {len(record)}"
            )
        if _slice(record, _POS_RECORD_TYPE) != _RECORD_TYPE_ADDENDA:
            raise UnsupportedMessageError("Not an addenda record (record type code is not 7)")
        if _slice(record, _POS_ADDENDA_TYPE) != _ADDENDA_TYPE_RETURN:
            raise UnsupportedMessageError(
                "Not a return addenda record (addenda type code is not 99)"
            )

        reason_code = _slice(record, _POS_RETURN_REASON)
        if not reason_code:
            raise MalformedMessageError("Return reason code is blank")

        original_trace = _slice(record, _POS_ORIGINAL_TRACE)
        if not original_trace:
            raise MalformedMessageError("Original entry trace number is blank")

        extra = {
            "original_rdfi": _slice(record, _POS_ORIGINAL_RDFI),
            "addenda_information": _slice(record, _POS_ADDENDA_INFO),
            "date_of_death": _slice(record, _POS_DATE_OF_DEATH),
        }

        return RecallCase(
            case_id=f"ach-return-{_slice(record, _POS_TRACE_NUMBER)}",
            rail=Rail.ACH,
            direction=Direction.INBOUND,
            channel=Channel.ACH_RETURN_ENTRY,
            original_payment_reference=original_trace,
            # The record carries no amount. Zero is the only representable
            # value and it is wrong; M4 reconciles the real figure from the
            # original entry. Flagged in native.extra so nothing downstream
            # mistakes it for a free recall.
            amount_cents=0,
            reason=canonical_reason_from_ach(reason_code),
            requesting_institution_id=self.requesting_institution_id,
            responding_institution_id=self.responding_institution_id,
            native=NativeEnvelope(
                message_id=_slice(record, _POS_TRACE_NUMBER),
                reason_code=reason_code,
                extra=extra | {"amount_unavailable_on_record": "true"},
            ),
        )

    def emit(self, case: RecallCase) -> bytes:
        if case.rail is not Rail.ACH:
            raise LossyRoundTripError(f"{case.rail.value} does not use Nacha addenda records")

        fields = [
            (_RECORD_TYPE_ADDENDA, 1, "<"),
            (_ADDENDA_TYPE_RETURN, 2, "<"),
            (case.native.reason_code, 3, "<"),
            (case.original_payment_reference, 15, "<"),
            (case.native.extra.get("date_of_death", ""), 6, "<"),
            (case.native.extra.get("original_rdfi", ""), 8, "<"),
            (case.native.extra.get("addenda_information", ""), 44, "<"),
            (case.native.message_id, 15, "<"),
        ]

        parts = []
        for value, width, _align in fields:
            if len(value) > width:
                raise LossyRoundTripError(
                    f"Value {value!r} exceeds its {width}-character field; emitting would "
                    "truncate a identifier a counterparty reconciles against"
                )
            parts.append(value.ljust(width))

        record = "".join(parts)
        if len(record) != ADDENDA_RECORD_LENGTH:  # pragma: no cover - widths are fixed above
            raise LossyRoundTripError(f"Assembled record is {len(record)} characters, not 94")

        return record.encode("ascii")


# ---------------------------------------------------------------------------
# The request for return - our format, because the rail has none
# ---------------------------------------------------------------------------

ACH_REQUEST_FORMAT_VERSION = "interlock-ach-request/1"
"""Version tag for a format Interlock invented.

Nacha mandates the response and prescribes no format for the request. This
structure is ours. The version tag is in every message so that a future reader -
or a counterparty we persuade to adopt it - can tell our convention from a
standard, and so that we can change it without ambiguity.
"""


class AchRequestAdapter:
    """A request for return of funds on ACH.

    The format is Interlock's own. It exists because the alternative is an email
    with a PDF attached, which is what the rail actually does today and which
    nothing can put a clock on.

    Keeping it structured here is what lets the SLA engine treat ACH like any
    other rail - which matters more on ACH than anywhere else, because ACH is
    the only rail where the ten-day clock is a rule rather than a courtesy.
    """

    rail = Rail.ACH
    channel = Channel.ACH_R06_REQUEST

    def parse(self, raw: bytes) -> RecallCase:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MalformedMessageError(f"Not valid UTF-8 JSON: {exc}") from exc

        if not isinstance(payload, dict):
            raise MalformedMessageError("Expected a JSON object")

        if payload.get("format") != ACH_REQUEST_FORMAT_VERSION:
            raise UnsupportedMessageError(
                f"Expected format {ACH_REQUEST_FORMAT_VERSION!r}, got {payload.get('format')!r}. "
                "This is an Interlock convention, not a Nacha standard, so a counterparty "
                "sending something else is expected rather than wrong"
            )

        try:
            settled_raw = payload.get("original_settled_at")
            reported_raw = payload.get("victim_reported_at")

            return RecallCase(
                case_id=payload["case_id"],
                rail=Rail.ACH,
                direction=Direction.INBOUND,
                channel=Channel.ACH_R06_REQUEST,
                original_payment_reference=payload["original_trace_number"],
                amount_cents=payload["amount_cents"],
                currency=payload.get("currency", "USD"),
                reason=canonical_reason_from_ach(payload["return_reason_code"]),
                requesting_institution_id=payload["odfi"],
                responding_institution_id=payload["rdfi"],
                original_settled_at=_parse_iso(settled_raw) if settled_raw else None,
                victim_reported_at=_parse_iso(reported_raw) if reported_raw else None,
                native=NativeEnvelope(
                    message_id=payload["request_id"],
                    reason_code=payload["return_reason_code"],
                    creation_time=_parse_iso(payload["created_at"]),
                    extra={
                        k: str(v)
                        for k, v in (payload.get("indemnity") or {}).items()
                        if v is not None
                    },
                ),
            )
        except KeyError as exc:
            raise MalformedMessageError(f"Missing required field {exc.args[0]!r}") from exc

    def emit(self, case: RecallCase) -> bytes:
        if case.rail is not Rail.ACH:
            raise LossyRoundTripError(f"{case.rail.value} is not ACH")
        if case.native.creation_time is None:
            raise LossyRoundTripError("Cannot emit an ACH request without a creation time")

        reason_code = (
            case.native.reason_code
            if case.channel is Channel.ACH_R06_REQUEST
            else CANONICAL_TO_ACH_CODE[case.reason]
        )

        payload = {
            "format": ACH_REQUEST_FORMAT_VERSION,
            "request_id": case.native.message_id,
            "case_id": case.case_id,
            "created_at": _format_iso(case.native.creation_time),
            "odfi": case.requesting_institution_id,
            "rdfi": case.responding_institution_id,
            "original_trace_number": case.original_payment_reference,
            "amount_cents": case.amount_cents,
            "currency": case.currency,
            "return_reason_code": reason_code,
            "original_settled_at": (
                _format_iso(case.original_settled_at) if case.original_settled_at else None
            ),
            "victim_reported_at": (
                _format_iso(case.victim_reported_at) if case.victim_reported_at else None
            ),
            "indemnity": dict(sorted(case.native.extra.items())) or None,
        }

        # sort_keys for byte-stable output: dict ordering is insertion-ordered
        # in CPython but that is a property of how we happened to build it, and
        # a round-trip test that depends on it would pass for the wrong reason.
        return (json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )


def _parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MalformedMessageError(f"Unparseable timestamp {value!r}") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _format_iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
