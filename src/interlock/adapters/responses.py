"""The formal reply for each rail, generated from the recorded outcome.

The AI drafts the human note; this module produces the machine message that
goes with it, deterministically, so the part a counterparty's system will
act on is never written by a model.

- **FedNow, RTP, Fedwire:** an ISO 20022 camt.029 (resolution of
  investigation), via :class:`~interlock.adapters.iso20022.Camt029Adapter`.
- **ACH:** Nacha requires an answer within ten banking days and prescribes no
  format for it, so there is no standard message to emit. Interlock's own
  ``interlock-ach-response/1`` JSON is produced instead and labelled as ours.

How outcomes map to camt.029 status codes
-----------------------------------------
``CNCL`` (cancellation accepted) for a full or partial return; ``PDCR``
(pending) for funds frozen but not yet returned - an honest "not decided
yet", and the note says so, because PDCR is the code a counterparty can use to
stop a clock without deciding anything; ``RJCR`` (rejected) for insufficient
funds, a dispute, a decline, or an acknowledged missed window. The stated
basis travels in ``AddtlInf``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from interlock.adapters.iso20022 import Camt029Adapter
from interlock.recall.state import CaseFile
from interlock.schema.common import Rail
from interlock.schema.recall import RecallDispositionCode

ACH_RESPONSE_FORMAT = "interlock-ach-response/1"

CAMT029_STATUS_OF: dict[RecallDispositionCode, str] = {
    RecallDispositionCode.FUNDS_RETURNED: "CNCL",
    RecallDispositionCode.PARTIAL_RETURN: "CNCL",
    RecallDispositionCode.FUNDS_FROZEN: "PDCR",
    RecallDispositionCode.INSUFFICIENT_FUNDS: "RJCR",
    RecallDispositionCode.ACCOUNT_HOLDER_DISPUTES: "RJCR",
    RecallDispositionCode.DECLINED_WITH_REASON: "RJCR",
    RecallDispositionCode.SLA_EXPIRED_ACKNOWLEDGED: "RJCR",
}


@dataclass(frozen=True, slots=True)
class FormalReply:
    format: str
    media_type: str
    content: str
    note: str


def formal_reply(case_file: CaseFile) -> FormalReply:
    """The rail's machine-readable answer for a closed case."""
    disposition = case_file.disposition
    if disposition is None:
        raise ValueError("a formal reply exists only once an outcome is recorded")

    case = case_file.case
    basis = case_file.disposition_reason or disposition.value.replace("_", " ")

    if case.rail in {Rail.FEDNOW, Rail.RTP, Rail.FEDWIRE}:
        status = CAMT029_STATUS_OF[disposition]
        xml = Camt029Adapter(case.rail).emit(case, status=status, reason=basis[:105])
        note = f"ISO 20022 camt.029, status {status}."
        if status == "PDCR":
            note += (
                " PDCR means pending: funds are frozen but not returned, and a final "
                "answer is still owed."
            )
        return FormalReply("camt.029", "application/xml", xml.decode("utf-8"), note)

    payload = {
        "format": ACH_RESPONSE_FORMAT,
        "case_id": case.case_id,
        "original_payment_reference": case.original_payment_reference,
        "amount_cents": case.amount_cents,
        "outcome": disposition.value,
        "stated_basis": case_file.disposition_reason,
        "responded_at": case_file.disposed_at.isoformat() if case_file.disposed_at else None,
        "responding_institution_id": case.responding_institution_id,
        "requesting_institution_id": case.requesting_institution_id,
    }
    return FormalReply(
        ACH_RESPONSE_FORMAT,
        "application/json",
        json.dumps(payload, indent=2, sort_keys=True),
        "Nacha requires a response within ten banking days and prescribes no format. "
        "This JSON is Interlock's own, not a Nacha standard.",
    )
