"""Recall protocol schema invariants.

The recall path is the differentiating component, and its defining property is
that nothing closes silently. These tests exist to make a silent close
impossible to write rather than merely discouraged.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from interlock.schema import (
    CURRENT_VERSION,
    RecallDisposition,
    RecallDispositionCode,
    RecallRequest,
    ScamCategory,
    default_recall_sla,
    hash_account,
    utc_now,
)
from interlock.schema.rails import house_policy_window

SALT = "test-network-salt-not-for-production"
ACCT = hash_account("000987654321", network_salt=SALT)


def a_recall(**overrides) -> RecallRequest:
    now = utc_now()
    base = {
        "protocol_version": CURRENT_VERSION,
        "recall_id": "rcl_00000001",
        "originating_case_reference": "CASE-2026-44821",
        "requesting_institution_id": "bank-a",
        "destination_institution_id": "bank-c",
        "destination_account_hash": ACCT,
        "amount_cents": 540_000,
        "claimed_scam_category": ScamCategory.INVESTMENT,
        "requested_at": now,
        "sla_expires_at": now + default_recall_sla(),
    }
    return RecallRequest(**(base | overrides))


class TestEveryDispositionIsTerminal:
    """There is no PENDING and no NULL. A request that goes quiet is the exact
    failure this protocol removes, so the type system offers no way to say it."""

    def test_no_pending_state_exists(self):
        values = {d.value for d in RecallDispositionCode}
        for forbidden in ("pending", "open", "in_progress", "unknown", "none"):
            assert forbidden not in values, (
                f"{forbidden!r} would let a request stay open indefinitely, which is the "
                "silent-close failure the protocol exists to prevent"
            )

    def test_sla_expiry_is_itself_a_disposition(self):
        # Expiry is acknowledged, not inferred from absence. An examiner asking
        # what happened gets an answer rather than a gap.
        assert RecallDispositionCode.SLA_EXPIRED_ACKNOWLEDGED in RecallDispositionCode

    def test_account_holder_dispute_is_expressible(self):
        # The receiving customer may be an account-takeover victim rather than
        # a criminal. A protocol without this forces institutions to mislabel
        # their own customers.
        assert RecallDispositionCode.ACCOUNT_HOLDER_DISPUTES.requires_reason


class TestReturnedAmountsReconcile:
    def _disposition(self, **overrides) -> RecallDisposition:
        base = {
            "protocol_version": CURRENT_VERSION,
            "recall_id": "rcl_00000001",
            "disposition": RecallDispositionCode.FUNDS_RETURNED,
            "responding_institution_id": "bank-c",
            "returned_amount_cents": 540_000,
        }
        return RecallDisposition(**(base | overrides))

    def test_returning_disposition_must_state_an_amount(self):
        with pytest.raises(ValidationError, match="returned_amount_cents"):
            self._disposition(returned_amount_cents=None)

    def test_partial_return_must_state_an_amount(self):
        with pytest.raises(ValidationError, match="returned_amount_cents"):
            self._disposition(
                disposition=RecallDispositionCode.PARTIAL_RETURN,
                returned_amount_cents=None,
            )

    def test_non_returning_disposition_must_not_state_an_amount(self):
        # An explicit zero reads as a partial return that failed. Absence is
        # the honest encoding of "no funds moved".
        with pytest.raises(ValidationError, match="must be absent"):
            self._disposition(
                disposition=RecallDispositionCode.INSUFFICIENT_FUNDS,
                returned_amount_cents=0,
                disposition_reason="funds left the institution before the request arrived",
            )

    def test_zero_return_on_a_returning_disposition_is_rejected(self):
        with pytest.raises(ValidationError, match="positive"):
            self._disposition(returned_amount_cents=0)


class TestReasonsWhereAReasonIsOwed:
    """These outcomes get quoted to a customer or a regulator."""

    @pytest.mark.parametrize(
        "code",
        [
            RecallDispositionCode.DECLINED_WITH_REASON,
            RecallDispositionCode.ACCOUNT_HOLDER_DISPUTES,
            RecallDispositionCode.INSUFFICIENT_FUNDS,
        ],
    )
    def test_reason_is_required(self, code: RecallDispositionCode):
        with pytest.raises(ValidationError, match="disposition_reason"):
            RecallDisposition(
                protocol_version=CURRENT_VERSION,
                recall_id="rcl_00000001",
                disposition=code,
                responding_institution_id="bank-c",
            )

    def test_freeze_does_not_require_a_reason(self):
        # Freezing is cooperation. Demanding an explanation for it would add
        # friction to the outcome the network most wants.
        d = RecallDisposition(
            protocol_version=CURRENT_VERSION,
            recall_id="rcl_00000001",
            disposition=RecallDispositionCode.FUNDS_FROZEN,
            responding_institution_id="bank-c",
        )
        assert d.disposition is RecallDispositionCode.FUNDS_FROZEN


class TestSlaClock:
    def test_sla_must_expire_after_it_was_requested(self):
        now = utc_now()
        with pytest.raises(ValidationError, match="sla_expires_at"):
            a_recall(requested_at=now, sla_expires_at=now - timedelta(seconds=1))

    def test_breach_is_detected_at_the_boundary(self):
        now = utc_now()
        recall = a_recall(requested_at=now, sla_expires_at=now + timedelta(hours=24))
        assert not recall.is_breached(at=now + timedelta(hours=23, minutes=59))
        assert recall.is_breached(at=now + timedelta(hours=24))

    def test_default_sla_comes_from_the_rail_matrix(self):
        # The fallback window is not this module's to invent. It comes from the
        # capability matrix, which carries the provenance and the reasoning, so
        # a silent change to the operational default cannot slip through review
        # and cannot happen in two places independently.
        assert default_recall_sla() == house_policy_window().as_timedelta()


class TestRecallSanity:
    def test_original_request_id_is_optional(self):
        # Absent means the payment was never screened, which is worth knowing:
        # the network had no chance to intercept it.
        assert a_recall().original_request_id is None

    def test_an_institution_cannot_recall_from_itself(self):
        with pytest.raises(ValidationError, match="itself"):
            a_recall(requesting_institution_id="bank-a", destination_institution_id="bank-a")

    def test_float_amount_is_rejected(self):
        with pytest.raises(ValidationError):
            a_recall(amount_cents=5400.50)
