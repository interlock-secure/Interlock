"""The privacy boundary guard.

Specification FR-1: an automated check confirms no PII field is reachable in
any serialised message. This is that check, run against the real models.

The test that matters most is ``test_adding_a_pii_field_fails_the_guard``: it
proves the guard would actually catch a mistake, rather than passing because
the current models happen to be clean.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from interlock.schema import (
    CURRENT_VERSION,
    Rail,
    RecallDisposition,
    RecallDispositionCode,
    SignalRequest,
    audit_all_wire_models,
    audit_model,
    hash_account,
    scan_serialised,
)

SALT = "test-network-salt-not-for-production"
ACCT = hash_account("000123456789", network_salt=SALT)


class TestTheRealModelsAreClean:
    def test_no_wire_model_violates_the_privacy_boundary(self):
        violations = audit_all_wire_models()
        assert violations == [], "\n".join(
            f"{v.model_name}.{v.field_name}: {v.problem}" for v in violations
        )


class TestTheGuardActuallyCatchesThings:
    """A guard that has never caught anything is indistinguishable from one
    that cannot."""

    def test_adding_a_pii_field_fails_the_guard(self):
        class SignalRequestWithLeak(BaseModel):
            model_config = ConfigDict(extra="forbid")
            protocol_version: str
            customer_name: str  # the mistake

        # Rename so the auditor looks it up under a manifest entry that exists.
        SignalRequestWithLeak.__name__ = "SignalRequest"
        violations = audit_model(SignalRequestWithLeak)

        assert violations
        problems = " ".join(v.problem for v in violations)
        assert "customer_name" in problems

    def test_an_unmanifested_model_is_rejected(self):
        class BrandNewWireModel(BaseModel):
            model_config = ConfigDict(extra="forbid")
            whatever: str

        violations = audit_model(BrandNewWireModel)
        assert violations
        assert "APPROVED_WIRE_FIELDS" in violations[0].problem

    def test_a_stale_manifest_entry_is_reported(self):
        class SignalRequestMissingFields(BaseModel):
            model_config = ConfigDict(extra="forbid")
            protocol_version: str

        SignalRequestMissingFields.__name__ = "SignalRequest"
        violations = audit_model(SignalRequestMissingFields)
        assert any("no longer on the model" in v.problem for v in violations)

    @pytest.mark.parametrize(
        "field_name",
        ["customer_email", "sender_address", "device_fingerprint", "account_balance", "memo_text"],
    )
    def test_common_pii_shapes_are_caught(self, field_name: str):
        model = type(
            "SignalRequest",
            (BaseModel,),
            {
                "__annotations__": {"protocol_version": str, field_name: str},
                "model_config": ConfigDict(extra="forbid"),
            },
        )
        violations = audit_model(model)
        assert any(v.field_name == field_name for v in violations)


class TestSerialisedScanCatchesSmuggledValues:
    """The manifest catches PII added as a field. This catches PII pasted into
    a field that was approved for something else."""

    def test_raw_account_number_in_free_text_is_caught(self):
        disposition = RecallDisposition(
            protocol_version=CURRENT_VERSION,
            recall_id="rcl_00000001",
            disposition=RecallDispositionCode.DECLINED_WITH_REASON,
            responding_institution_id="bank-c",
            disposition_reason="Account 000123456789 belongs to a long-standing customer",
        )
        problems = scan_serialised(disposition.model_dump(mode="json"))
        assert problems
        assert "disposition_reason" in problems[0]

    def test_email_in_free_text_is_caught(self):
        payload = {"disposition_reason": "Confirmed with victim@example.com by phone"}
        assert scan_serialised(payload)

    def test_a_clean_message_scans_clean(self):
        request = SignalRequest(
            protocol_version=CURRENT_VERSION,
            request_id="req_00000001",
            requesting_institution_id="bank-a",
            destination_institution_id="bank-c",
            destination_account_hash=ACCT,
            amount_cents=540_000,
            rail=Rail.FEDNOW,
        )
        assert scan_serialised(request.model_dump(mode="json")) == []

    def test_the_account_hash_itself_is_not_flagged(self):
        # A 64-char hex digest is the one identifier-shaped value that is safe,
        # because it cannot be reversed to the account it names.
        assert scan_serialised({"destination_account_hash": ACCT}) == []

    def test_nested_structures_are_walked(self):
        payload = {"evidence": {"notes": ["see account 000123456789"]}}
        problems = scan_serialised(payload)
        assert problems
        assert "evidence.notes[0]" in problems[0]
