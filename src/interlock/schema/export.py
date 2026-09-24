"""Export the wire protocol as JSON Schema.

    uv run python -m interlock.schema.export

Interoperability is the entire product claim, and a claim that only holds for
Python participants is not one. A Java or Go institution implements against
these files, not against our models.

Output goes to docs/protocol/<version>/, which is committed. Reviewers can see
a protocol change as a diff in the schema files rather than having to infer it
from a Pydantic model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from interlock.schema.audit import AuditEntry
from interlock.schema.recall import (
    RecallAcknowledgement,
    RecallDisposition,
    RecallRequest,
)
from interlock.schema.signal import DecisionRecord, SignalRequest, SignalResponse
from interlock.schema.versioning import CURRENT_VERSION

WIRE_MODELS: tuple[type[BaseModel], ...] = (
    SignalRequest,
    SignalResponse,
    DecisionRecord,
    RecallRequest,
    RecallDisposition,
    RecallAcknowledgement,
    AuditEntry,
)


def _to_snake(name: str) -> str:
    out: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


def build_schemas() -> dict[str, dict[str, Any]]:
    """One JSON Schema document per wire model."""
    return {_to_snake(model.__name__): model.model_json_schema() for model in WIRE_MODELS}


def build_index() -> dict[str, Any]:
    """A manifest tying the protocol version to its documents.

    Carries the invariants that JSON Schema cannot express. A cross-model rule -
    "an actionable band must name a contributing dimension" - is enforced in our
    validators but invisible in a generated schema, so an implementer working
    only from the JSON would satisfy the types and still break the protocol.
    """
    return {
        "protocol_version": CURRENT_VERSION,
        "documents": sorted(build_schemas().keys()),
        "invariants_not_expressible_in_json_schema": [
            "SignalResponse: a risk_band of low, elevated or high must carry at least one "
            "contributing dimension, at least one explanation string, and a responding "
            "institution id.",
            "SignalResponse: a risk_band of no_signal or unavailable must carry no "
            "contributing dimensions and no confidence value.",
            "DecisionRecord: a decision of held is invalid when acted_on_band is unavailable. "
            "A network outage must not become a payment outage.",
            "RecallDisposition: funds_returned and partial_return require "
            "returned_amount_cents; every other disposition must omit it entirely rather "
            "than sending zero.",
            "RecallDisposition: declined_with_reason, account_holder_disputes and "
            "insufficient_funds require a disposition_reason.",
            "RecallRequest: sla_expires_at must be strictly after requested_at.",
            "All models: set-valued fields serialise in sorted order. Implementations that "
            "emit them unsorted will produce audit digests that diverge from ours.",
            "All models: monetary values are integer cents. A fractional amount is a "
            "protocol violation, not a value to round.",
            "All models: undeclared fields are rejected, not ignored. Negotiate the "
            "protocol version first and send only fields defined at that version.",
        ],
    }


def main() -> None:
    target = Path(__file__).resolve().parents[3] / "docs" / "protocol" / CURRENT_VERSION
    target.mkdir(parents=True, exist_ok=True)

    for name, schema in build_schemas().items():
        path = target / f"{name}.schema.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
        print(f"wrote {path.name}")

    index_path = target / "index.json"
    index_path.write_text(json.dumps(build_index(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {index_path.name}")


if __name__ == "__main__":
    main()
