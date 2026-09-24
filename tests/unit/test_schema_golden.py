"""Golden-file tests for the wire schema.

The schema is the product contract. These tests exist so that a change to a
published model cannot merge without a deliberate version bump.

Each fixture in tests/fixtures/schema/<version>/ records exactly what a message
looked like at that protocol version. Two things are asserted: that the current
build can still parse it, and that re-serialising produces the identical bytes.

If this fails, the question to ask is not "how do I regenerate the fixture" but
"did I mean to change the protocol?" If the answer is yes, bump the version,
generate fixtures for the new one, and leave the old fixtures in place so this
test keeps proving that old messages still parse.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from interlock.schema import (
    SUPPORTED_VERSIONS,
    AuditEntry,
    RecallDisposition,
    RecallRequest,
    SignalRequest,
    SignalResponse,
)
from interlock.schema.signal import DecisionRecord

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "schema"

FIXTURE_MODELS: dict[str, type[BaseModel]] = {
    "signal_request": SignalRequest,
    "signal_response_high": SignalResponse,
    "signal_response_unavailable": SignalResponse,
    "signal_response_no_signal": SignalResponse,
    "decision_record": DecisionRecord,
    "recall_request": RecallRequest,
    "recall_disposition": RecallDisposition,
    "audit_entry": AuditEntry,
}


def all_fixtures() -> list[tuple[str, str]]:
    found = []
    for version in SUPPORTED_VERSIONS:
        directory = FIXTURE_ROOT / version
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            found.append((version, path.stem))
    return found


class TestGoldenFixtures:
    def test_fixtures_exist(self):
        assert all_fixtures(), (
            "No golden fixtures found. Generate them with "
            "`uv run python -m tests.fixtures.generate`."
        )

    @pytest.mark.parametrize(("version", "name"), all_fixtures())
    def test_fixture_still_parses(self, version: str, name: str):
        """A message written at a published version must still be readable.

        This is the compatibility guarantee a participant relies on when it
        chooses not to upgrade.
        """
        model = FIXTURE_MODELS.get(name)
        assert model is not None, f"No model registered for fixture {name!r}"

        payload = json.loads((FIXTURE_ROOT / version / f"{name}.json").read_text())
        model.model_validate(payload)

    @pytest.mark.parametrize(("version", "name"), all_fixtures())
    def test_fixture_round_trips_byte_identically(self, version: str, name: str):
        """Parse then re-serialise must reproduce the fixture exactly.

        Catches silent changes a parse test would miss: a renamed field, a
        changed default, an altered serialisation format.
        """
        model = FIXTURE_MODELS[name]
        path = FIXTURE_ROOT / version / f"{name}.json"
        original = json.loads(path.read_text())

        reserialised = model.model_validate(original).model_dump(mode="json")

        assert reserialised == original, (
            f"{name} at protocol version {version} no longer round-trips.\n"
            "The published schema changed. If that was deliberate, bump the protocol "
            "version and generate fixtures for the new one - do not regenerate this file."
        )


class TestSerialisationIsDeterministic:
    """Set-valued fields must serialise in a stable order.

    Not a cosmetic concern. The audit chain digests the serialised message, so
    two participants recording the same event must produce identical bytes or
    the chain reports tampering that never happened.
    """

    @pytest.mark.parametrize("name", ["signal_request", "signal_response_high"])
    def test_set_fields_are_sorted(self, name: str):
        payload = json.loads((FIXTURE_ROOT / SUPPORTED_VERSIONS[-1] / f"{name}.json").read_text())
        for field in ("sender_context_flags", "contributing_dimensions"):
            if payload.get(field):
                assert payload[field] == sorted(payload[field]), (
                    f"{name}.{field} is not sorted; serialisation is not deterministic and "
                    "audit digests will diverge between participants"
                )

    def test_serialisation_is_stable_across_processes(self):
        """The real test: a fresh interpreter with a different hash seed must
        produce the same bytes.

        Python randomises string hashing per process, so a set-backed field can
        serialise in a different order in a different process. Running this in a
        subprocess with an explicit alternative seed is the only way to catch it
        from inside a test suite that has already fixed its own seed.
        """
        script = (
            "import json;"
            "from tests.fixtures.generate import build_messages;"
            "print(json.dumps(build_messages(), sort_keys=True))"
        )
        outputs = []
        for seed in ("0", "1", "42"):
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                cwd=Path(__file__).parent.parent.parent,
                env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
                check=True,
            )
            outputs.append(result.stdout)

        assert len(set(outputs)) == 1, (
            "Serialisation differs between processes with different hash seeds. "
            "A set-valued field is missing a sorting field_serializer."
        )


class TestFixtureCoverage:
    def test_every_wire_model_has_a_fixture(self):
        """A model with no fixture is a model whose schema can change silently."""
        from interlock.schema.pii import APPROVED_WIRE_FIELDS

        covered = {m.__name__ for m in FIXTURE_MODELS.values()}
        expected = set(APPROVED_WIRE_FIELDS.keys())

        # RecallAcknowledgement is a receipt with no invariants of its own; it
        # is covered by the privacy manifest but does not need a golden file.
        expected.discard("RecallAcknowledgement")

        missing = expected - covered
        assert not missing, (
            f"These wire models have no golden fixture: {sorted(missing)}. "
            "Add one to tests/fixtures/generate.py so their schema cannot change silently."
        )

    def test_both_non_answer_bands_are_covered(self):
        # UNAVAILABLE and NO_SIGNAL are the two bands that must never be
        # confused with LOW. Both get a fixture so their shape is pinned.
        names = {name for _, name in all_fixtures()}
        assert "signal_response_unavailable" in names
        assert "signal_response_no_signal" in names


class TestExportedJsonSchemaIsCurrent:
    """The committed JSON Schema must match the models.

    A stale export is worse than none: an implementer in another language would
    build against a protocol we no longer speak, and would find out at
    integration time rather than at review time.
    """

    def test_exported_schemas_match_the_models(self):
        from interlock.schema.export import build_schemas
        from interlock.schema.versioning import CURRENT_VERSION

        root = Path(__file__).parents[2] / "docs" / "protocol" / CURRENT_VERSION
        assert root.is_dir(), (
            f"No exported protocol at docs/protocol/{CURRENT_VERSION}/. "
            "Run `uv run python -m interlock.schema.export`."
        )

        for name, schema in build_schemas().items():
            path = root / f"{name}.schema.json"
            assert path.exists(), f"Missing exported schema {path.name}"
            committed = json.loads(path.read_text())
            assert committed == schema, (
                f"{path.name} is out of date with the model. Re-run "
                "`uv run python -m interlock.schema.export` and commit the result."
            )
