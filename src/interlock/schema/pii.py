"""The privacy boundary guard.

Specification FR-1 requires an automated check that no personally identifiable
field is reachable in any serialised message. This module is that check, and it
is deliberately built as an allowlist rather than a denylist.

A denylist - "reject fields called ``name`` or ``ssn``" - only catches the PII
someone thought to name obviously. It does not catch ``customer_ref``,
``memo``, ``narrative`` or ``device_id``, all of which carry identifying
information and none of which look like PII to a substring match.

The allowlist inverts the burden. Every field on every wire model must be
declared here as reviewed. A developer who adds a field gets a failing test
naming their field, and the fix is to add it to the manifest - which is a
thirty-second edit that forces the question "should this cross an
institutional boundary?" to be asked out loud, by someone, once.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# --------------------------------------------------------------------------
# The manifest
# --------------------------------------------------------------------------

APPROVED_WIRE_FIELDS: dict[str, frozenset[str]] = {
    "SignalRequest": frozenset(
        {
            "protocol_version",
            "request_id",
            "requesting_institution_id",
            "destination_institution_id",
            "destination_account_hash",
            "amount_cents",
            "currency",
            "rail",
            "sender_context_flags",
            "requested_at",
        }
    ),
    "SignalResponse": frozenset(
        {
            "protocol_version",
            "request_id",
            "risk_band",
            "contributing_dimensions",
            "confidence",
            "explanation_strings",
            "responding_institution_id",
            "valid_until",
            "responded_at",
        }
    ),
    "DecisionRecord": frozenset(
        {
            "protocol_version",
            "request_id",
            "case_reference",
            "decision",
            "acted_on_band",
            "institution_threshold",
            "decided_at",
        }
    ),
    "RecallRequest": frozenset(
        {
            "protocol_version",
            "recall_id",
            "originating_case_reference",
            "original_request_id",
            "requesting_institution_id",
            "destination_institution_id",
            "destination_account_hash",
            "amount_cents",
            "claimed_scam_category",
            "victim_reported_at",
            "requested_at",
            "sla_expires_at",
        }
    ),
    "RecallDisposition": frozenset(
        {
            "protocol_version",
            "recall_id",
            "disposition",
            "responding_institution_id",
            "returned_amount_cents",
            "disposition_reason",
            "resolved_at",
        }
    ),
    "RecallAcknowledgement": frozenset(
        {
            "protocol_version",
            "recall_id",
            "acknowledging_institution_id",
            "acknowledged_at",
        }
    ),
    "AuditEntry": frozenset(
        {
            "sequence_number",
            "previous_hash",
            "entry_hash",
            "event_type",
            "actor_institution_id",
            "correlation_id",
            "payload_digest",
            "recorded_at",
        }
    ),
}
"""Every field permitted to cross an institutional boundary, by model.

Changing this is a privacy decision, not a refactor. A PR that edits this dict
should say why in its description.
"""


# --------------------------------------------------------------------------
# The denylist, kept as a second line rather than the only one
# --------------------------------------------------------------------------

FORBIDDEN_FIELD_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "name",
        "address",
        "email",
        "phone",
        "ssn",
        "social_security",
        "dob",
        "date_of_birth",
        "birth",
        "balance",
        "account_number",
        "routing",
        "card",
        "iban",
        "postcode",
        "zip",
        "ip_address",
        "device_id",
        "fingerprint",
        "memo",
        "narrative",
        "free_text",
        "note",
        "description",
        "customer",
    }
)
"""Substrings that indicate a field is carrying identifying information.

Redundant with the allowlist by design. It exists to produce a better error
message - "field ``customer_memo`` looks like PII" is more useful to the
developer who just added it than "field is not in the manifest" - and to catch
a case where someone adds both the field and its manifest entry in one go
without thinking about it.

Two deliberate exemptions are handled in ``_is_exempt`` below.
"""

_DENYLIST_EXEMPTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        # "hash" fields name an account but cannot be reversed to one, and the
        # AccountHash type constrains them to a 64-character digest.
        ("SignalRequest", "destination_account_hash"),
        ("RecallRequest", "destination_account_hash"),
        # An institution id identifies a participant, not a person. Public
        # network membership is not a privacy concern.
        ("SignalRequest", "requesting_institution_id"),
        ("SignalRequest", "destination_institution_id"),
        ("SignalResponse", "responding_institution_id"),
        ("DecisionRecord", "institution_threshold"),
        ("RecallRequest", "requesting_institution_id"),
        ("RecallRequest", "destination_institution_id"),
        ("RecallDisposition", "responding_institution_id"),
        ("RecallDisposition", "disposition_reason"),
        ("RecallAcknowledgement", "acknowledging_institution_id"),
        ("AuditEntry", "actor_institution_id"),
    }
)


def _is_exempt(model_name: str, field_name: str) -> bool:
    return (model_name, field_name) in _DENYLIST_EXEMPTIONS


class PrivacyViolation(BaseModel):
    """One located privacy problem. Reported rather than raised, so a single
    run names every offending field instead of the first."""

    model_name: str
    field_name: str
    problem: str


def audit_model(model: type[BaseModel]) -> list[PrivacyViolation]:
    """Check one wire model against the manifest and the denylist."""
    violations: list[PrivacyViolation] = []
    model_name = model.__name__

    approved = APPROVED_WIRE_FIELDS.get(model_name)
    if approved is None:
        return [
            PrivacyViolation(
                model_name=model_name,
                field_name="*",
                problem=(
                    f"{model_name} crosses an institutional boundary but has no entry in "
                    "APPROVED_WIRE_FIELDS. Add one, and say in the PR why each field "
                    "belongs on the wire."
                ),
            )
        ]

    actual = set(model.model_fields.keys())

    for undeclared in sorted(actual - approved):
        violations.append(
            PrivacyViolation(
                model_name=model_name,
                field_name=undeclared,
                problem=(
                    f"{model_name}.{undeclared} is not in the approved manifest. If it should "
                    "cross an institutional boundary, add it to APPROVED_WIRE_FIELDS and "
                    "explain why in the PR."
                ),
            )
        )

    for stale in sorted(approved - actual):
        violations.append(
            PrivacyViolation(
                model_name=model_name,
                field_name=stale,
                problem=(
                    f"{model_name}.{stale} is in the manifest but no longer on the model. "
                    "Remove the stale entry so the manifest keeps meaning something."
                ),
            )
        )

    for field_name in sorted(actual):
        if _is_exempt(model_name, field_name):
            continue
        lowered = field_name.lower()
        for token in FORBIDDEN_FIELD_SUBSTRINGS:
            if token in lowered:
                violations.append(
                    PrivacyViolation(
                        model_name=model_name,
                        field_name=field_name,
                        problem=(
                            f"{model_name}.{field_name} contains {token!r}, which suggests it "
                            "carries identifying information. If it genuinely does not, add it "
                            "to _DENYLIST_EXEMPTIONS with a comment explaining why."
                        ),
                    )
                )
                break

    return violations


def audit_all_wire_models() -> list[PrivacyViolation]:
    """Audit every model that crosses an institutional boundary.

    Imports inside the function to avoid a circular import: the schema modules
    do not depend on this guard, the guard depends on them, and the test suite
    depends on the guard.
    """
    from interlock.schema.audit import AuditEntry
    from interlock.schema.recall import (
        RecallAcknowledgement,
        RecallDisposition,
        RecallRequest,
    )
    from interlock.schema.signal import DecisionRecord, SignalRequest, SignalResponse

    models: tuple[type[BaseModel], ...] = (
        SignalRequest,
        SignalResponse,
        DecisionRecord,
        RecallRequest,
        RecallDisposition,
        RecallAcknowledgement,
        AuditEntry,
    )

    violations: list[PrivacyViolation] = []
    for model in models:
        violations.extend(audit_model(model))
    return violations


def scan_serialised(payload: dict[str, Any]) -> list[str]:
    """Scan an already-serialised message for values that look like raw
    identifiers.

    The manifest catches PII added as a declared field. This catches PII
    smuggled into a field that was approved for something else - a raw account
    number pasted into ``disposition_reason``, for instance, which is a free
    text field precisely because a human wrote it.
    """
    from interlock.schema.common import looks_like_raw_identifier

    problems: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list | tuple | set | frozenset):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str):
            # A 64-char hex digest is the one thing that is allowed to look
            # like an identifier, because it is one that cannot be reversed.
            if len(node) == 64 and all(c in "0123456789abcdef" for c in node):
                return
            if looks_like_raw_identifier(node):
                problems.append(
                    f"{path} contains a value that looks like a raw account identifier, "
                    "an IBAN or an email address"
                )

    walk(payload, "")
    return problems
