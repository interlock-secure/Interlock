"""The schema invariants that exist to prevent specific failures.

Each test here maps to a rule in CLAUDE.md or a requirement in the
specification. If one of these fails, the failure is the point: something that
was supposed to be impossible became possible.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from interlock.schema import (
    CURRENT_VERSION,
    DecisionRecord,
    PaymentDecision,
    Rail,
    RiskBand,
    SenderContextFlag,
    SignalDimension,
    SignalRequest,
    SignalResponse,
    hash_account,
    utc_now,
)

SALT = "test-network-salt-not-for-production"
ACCT = hash_account("000123456789", network_salt=SALT)


def a_request(**overrides) -> SignalRequest:
    base = {
        "protocol_version": CURRENT_VERSION,
        "request_id": "req_00000001",
        "requesting_institution_id": "bank-a",
        "destination_institution_id": "bank-c",
        "destination_account_hash": ACCT,
        "amount_cents": 540_000,
        "rail": Rail.FEDNOW,
    }
    return SignalRequest(**(base | overrides))


class TestMoneyIsIntegerCents:
    """CLAUDE.md: money is integer cents, strictly typed.

    The failure this prevents: a float amount that silently loses cents, in a
    system where the amount is quoted back to a customer whose payment was held.
    """

    def test_float_amount_is_rejected_not_coerced(self):
        with pytest.raises(ValidationError) as exc:
            a_request(amount_cents=5400.75)
        assert "amount_cents" in str(exc.value)

    def test_whole_float_is_also_rejected(self):
        # 5400.0 is a float that happens to be whole. Accepting it would mean
        # the type is advisory rather than enforced, and the next one will not
        # be whole.
        with pytest.raises(ValidationError):
            a_request(amount_cents=5400.0)

    def test_negative_amount_is_rejected(self):
        with pytest.raises(ValidationError):
            a_request(amount_cents=-1)

    def test_amount_above_the_rail_cap_is_rejected(self):
        # Both FedNow and RTP raised their cap to $10 million in 2025.
        with pytest.raises(ValidationError):
            a_request(amount_cents=10_000_00_001)


class TestAccountIdentifiersNeverLeaveRaw:
    """Specification FR-1: hashed identifiers, no PII on the wire."""

    def test_raw_account_number_cannot_be_used_as_a_hash(self):
        with pytest.raises(ValidationError):
            a_request(destination_account_hash="000123456789")

    def test_email_cannot_be_used_as_a_hash(self):
        with pytest.raises(ValidationError):
            a_request(destination_account_hash="victim@example.com")

    def test_uppercase_hex_is_rejected_so_hashes_compare_equal(self):
        # Two participants must produce byte-identical hashes for the same
        # account, or the prior-network-flags dimension silently fails to match.
        with pytest.raises(ValidationError):
            a_request(destination_account_hash=ACCT.upper())

    def test_same_account_hashes_identically_across_participants(self):
        assert hash_account("000123456789", network_salt=SALT) == hash_account(
            "  000123456789  ", network_salt=SALT
        )

    def test_different_salt_produces_a_different_hash(self):
        assert hash_account("000123456789", network_salt="other") != ACCT

    def test_unsalted_hashing_is_refused(self):
        with pytest.raises(ValueError, match="salt"):
            hash_account("000123456789", network_salt="")


class TestUnknownIsNeverReportedAsLowRisk:
    """CLAUDE.md rule 2, and the failure mode the whole network exists to remove.

    An analyst who sees "low risk" believes the receiving institution looked
    and found nothing. If a timeout can produce that same band, the analyst is
    being misled by the system rather than informed by it.
    """

    def test_low_band_without_a_dimension_is_invalid(self):
        with pytest.raises(ValidationError, match="contributing dimension"):
            SignalResponse(
                protocol_version=CURRENT_VERSION,
                request_id="req_00000001",
                risk_band=RiskBand.LOW,
                responding_institution_id="bank-c",
                explanation_strings=("nothing found",),
            )

    def test_low_band_without_an_explanation_is_invalid(self):
        with pytest.raises(ValidationError, match="explanation"):
            SignalResponse(
                protocol_version=CURRENT_VERSION,
                request_id="req_00000001",
                risk_band=RiskBand.LOW,
                responding_institution_id="bank-c",
                contributing_dimensions=frozenset({SignalDimension.ACCOUNT_TENURE}),
            )

    def test_unavailable_cannot_carry_a_confidence(self):
        # A confidence on a non-answer implies an assessment nobody made.
        with pytest.raises(ValidationError, match="confidence"):
            SignalResponse(
                protocol_version=CURRENT_VERSION,
                request_id="req_00000001",
                risk_band=RiskBand.UNAVAILABLE,
                confidence=0.5,
            )

    def test_no_signal_is_distinct_from_low(self):
        # A dormant account produces no signal. Reporting that as LOW would
        # present absence of evidence as evidence of absence.
        assert RiskBand.NO_SIGNAL is not RiskBand.LOW
        assert not RiskBand.NO_SIGNAL.is_actionable
        assert RiskBand.LOW.is_actionable

    def test_unavailable_constructor_produces_a_valid_non_answer(self):
        r = SignalResponse.unavailable(
            request_id="req_00000001",
            protocol_version=CURRENT_VERSION,
            reason="hub exceeded the 300ms budget",
        )
        assert r.risk_band is RiskBand.UNAVAILABLE
        assert not r.risk_band.is_actionable
        assert r.explanation_strings


class TestNetworkOutageIsNotPaymentOutage:
    """Specification FR-7. Losing the network must not lose the payment."""

    def test_cannot_hold_a_payment_on_an_unavailable_band(self):
        with pytest.raises(ValidationError, match="payment outage"):
            DecisionRecord(
                protocol_version=CURRENT_VERSION,
                request_id="req_00000001",
                decision=PaymentDecision.HELD,
                acted_on_band=RiskBand.UNAVAILABLE,
                institution_threshold="hold_above_elevated",
            )

    def test_passing_on_an_unavailable_band_is_allowed(self):
        record = DecisionRecord(
            protocol_version=CURRENT_VERSION,
            request_id="req_00000001",
            decision=PaymentDecision.PASSED,
            acted_on_band=RiskBand.UNAVAILABLE,
            institution_threshold="fallback_to_local_score",
        )
        assert record.decision is PaymentDecision.PASSED

    def test_warning_on_an_unavailable_band_is_allowed(self):
        # A warning is friction, not a block. An institution may reasonably
        # choose to warn its customer when it could not reach the network.
        record = DecisionRecord(
            protocol_version=CURRENT_VERSION,
            request_id="req_00000001",
            decision=PaymentDecision.WARNED,
            acted_on_band=RiskBand.UNAVAILABLE,
            institution_threshold="warn_when_degraded",
        )
        assert record.decision is PaymentDecision.WARNED


class TestWireModelsRejectUndeclaredFields:
    """extra="forbid" is the mechanism that stops undeclared data crossing."""

    def test_extra_field_is_rejected(self):
        with pytest.raises(ValidationError):
            a_request(customer_name="Jane Doe")

    def test_models_are_immutable(self):
        req = a_request()
        with pytest.raises(ValidationError):
            req.amount_cents = 1  # type: ignore[misc]


class TestSignalValidity:
    def test_expired_signal_is_detected(self):
        now = utc_now()
        response = SignalResponse(
            protocol_version=CURRENT_VERSION,
            request_id="req_00000001",
            risk_band=RiskBand.HIGH,
            contributing_dimensions=frozenset({SignalDimension.ONWARD_MOVEMENT_PATTERN}),
            explanation_strings=("funds swept within 4 minutes on 11 of 14 inbound transfers",),
            responding_institution_id="bank-c",
            responded_at=now,
            valid_until=now + timedelta(minutes=5),
        )
        assert not response.is_expired(at=now + timedelta(minutes=4))
        assert response.is_expired(at=now + timedelta(minutes=6))

    def test_validity_window_must_be_in_the_future(self):
        now = utc_now()
        with pytest.raises(ValidationError, match="valid_until"):
            SignalResponse(
                protocol_version=CURRENT_VERSION,
                request_id="req_00000001",
                risk_band=RiskBand.NO_SIGNAL,
                responded_at=now,
                valid_until=now - timedelta(seconds=1),
            )


class TestRequestSanity:
    def test_an_institution_cannot_query_itself(self):
        with pytest.raises(ValidationError, match="itself"):
            a_request(requesting_institution_id="bank-a", destination_institution_id="bank-a")

    def test_naive_timestamp_is_rejected(self):
        from datetime import datetime

        with pytest.raises(ValidationError, match="timezone-aware"):
            a_request(requested_at=datetime(2026, 9, 17, 12, 0, 0))

    def test_sender_context_flags_are_optional(self):
        # Empty is valid and means "nothing unusual", which is information.
        assert a_request().sender_context_flags == frozenset()

    def test_sender_context_flags_round_trip(self):
        req = a_request(
            sender_context_flags={
                SenderContextFlag.FIRST_TIME_PAYEE,
                SenderContextFlag.AMOUNT_ANOMALOUS_FOR_CUSTOMER,
            }
        )
        assert SenderContextFlag.FIRST_TIME_PAYEE in req.sender_context_flags
