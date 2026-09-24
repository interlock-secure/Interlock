"""Generate golden fixtures for the current protocol version.

Run deliberately, never automatically:

    uv run python -m tests.fixtures.generate

Regenerating fixtures for a version that already exists defeats their purpose -
they exist precisely so that a change to a published model cannot pass review
unnoticed. If the golden test fails, the correct response is almost always to
bump the protocol version and generate fixtures for the new one, leaving the
old fixtures in place so the compatibility test keeps proving the old messages
still parse.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from interlock.schema import (
    CURRENT_VERSION,
    EventType,
    PaymentDecision,
    Rail,
    RecallDisposition,
    RecallDispositionCode,
    RecallRequest,
    RiskBand,
    ScamCategory,
    SenderContextFlag,
    SignalDimension,
    SignalRequest,
    SignalResponse,
    append_entry,
    hash_account,
)
from interlock.schema.signal import DecisionRecord

# Fixed so fixtures are byte-stable across runs. A fixture that changes because
# time passed is a fixture that gets regenerated out of annoyance.
FIXED_TIME = datetime.fromisoformat("2026-09-17T16:00:00+00:00")
FIXTURE_SALT = "interlock-golden-fixture-salt"
ACCOUNT = hash_account("000123456789", network_salt=FIXTURE_SALT)


def build_messages() -> dict[str, dict]:
    """One canonical example of every model that crosses a boundary."""
    signal_request = SignalRequest(
        protocol_version=CURRENT_VERSION,
        request_id="req_0000000000001",
        requesting_institution_id="first-community-cu",
        destination_institution_id="metro-savings-bank",
        destination_account_hash=ACCOUNT,
        amount_cents=540_000,
        currency="USD",
        rail=Rail.FEDNOW,
        sender_context_flags={
            SenderContextFlag.FIRST_TIME_PAYEE,
            SenderContextFlag.AMOUNT_ANOMALOUS_FOR_CUSTOMER,
        },
        requested_at=FIXED_TIME,
    )

    signal_response = SignalResponse(
        protocol_version=CURRENT_VERSION,
        request_id="req_0000000000001",
        risk_band=RiskBand.HIGH,
        contributing_dimensions={
            SignalDimension.ACCOUNT_TENURE,
            SignalDimension.INBOUND_VELOCITY_ANOMALY,
            SignalDimension.ONWARD_MOVEMENT_PATTERN,
        },
        confidence=0.87,
        explanation_strings=(
            "Account opened 11 days ago",
            "14 first-time inbound transfers in the last 24 hours",
            "Funds swept onward within 6 minutes on 11 of those transfers",
        ),
        responding_institution_id="metro-savings-bank",
        responded_at=FIXED_TIME,
        valid_until=FIXED_TIME + timedelta(minutes=5),
    )

    signal_unavailable = SignalResponse(
        protocol_version=CURRENT_VERSION,
        request_id="req_0000000000002",
        risk_band=RiskBand.UNAVAILABLE,
        explanation_strings=("Receiving institution exceeded the 300ms budget",),
        responded_at=FIXED_TIME,
    )

    signal_no_signal = SignalResponse(
        protocol_version=CURRENT_VERSION,
        request_id="req_0000000000003",
        risk_band=RiskBand.NO_SIGNAL,
        explanation_strings=("No activity on file for this account",),
        responding_institution_id="metro-savings-bank",
        responded_at=FIXED_TIME,
    )

    decision = DecisionRecord(
        protocol_version=CURRENT_VERSION,
        request_id="req_0000000000001",
        case_reference="CASE-2026-44821",
        decision=PaymentDecision.HELD,
        acted_on_band=RiskBand.HIGH,
        institution_threshold="hold_at_high",
        decided_at=FIXED_TIME,
    )

    recall_request = RecallRequest(
        protocol_version=CURRENT_VERSION,
        recall_id="rcl_0000000000001",
        originating_case_reference="CASE-2026-44821",
        original_request_id="req_0000000000001",
        requesting_institution_id="first-community-cu",
        destination_institution_id="metro-savings-bank",
        destination_account_hash=ACCOUNT,
        amount_cents=540_000,
        claimed_scam_category=ScamCategory.INVESTMENT,
        victim_reported_at=FIXED_TIME + timedelta(hours=3),
        requested_at=FIXED_TIME + timedelta(hours=4),
        sla_expires_at=FIXED_TIME + timedelta(hours=28),
    )

    recall_disposition = RecallDisposition(
        protocol_version=CURRENT_VERSION,
        recall_id="rcl_0000000000001",
        disposition=RecallDispositionCode.PARTIAL_RETURN,
        responding_institution_id="metro-savings-bank",
        returned_amount_cents=180_000,
        disposition_reason="Remaining balance had already been withdrawn at an ATM",
        resolved_at=FIXED_TIME + timedelta(hours=9),
    )

    audit_entry = append_entry(
        previous=None,
        event_type=EventType.SIGNAL_REQUESTED,
        correlation_id="req_0000000000001",
        payload={"amount_cents": 540_000, "rail": "fednow"},
        actor_institution_id="first-community-cu",
        recorded_at=FIXED_TIME,
    )

    return {
        "signal_request": signal_request.model_dump(mode="json"),
        "signal_response_high": signal_response.model_dump(mode="json"),
        "signal_response_unavailable": signal_unavailable.model_dump(mode="json"),
        "signal_response_no_signal": signal_no_signal.model_dump(mode="json"),
        "decision_record": decision.model_dump(mode="json"),
        "recall_request": recall_request.model_dump(mode="json"),
        "recall_disposition": recall_disposition.model_dump(mode="json"),
        "audit_entry": audit_entry.model_dump(mode="json"),
    }


def main() -> None:
    target = Path(__file__).parent / "schema" / CURRENT_VERSION
    target.mkdir(parents=True, exist_ok=True)

    for name, payload in build_messages().items():
        path = target / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"wrote {path.relative_to(Path(__file__).parent.parent.parent)}")


if __name__ == "__main__":
    main()
