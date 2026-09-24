"""Persistence, the chain, and whether an examiner could actually check it.

The tests that matter here are the tamper tests. A hash chain nobody has tried
to break is decoration.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from interlock.recall.evidence import (
    EXPORT_FORMAT,
    build_export,
    summarise_obligations,
    verify_export,
)
from interlock.recall.ledger import rebuild_chain
from interlock.recall.state import Transition
from interlock.recall.store import SqliteCaseRepository
from interlock.schema.case import (
    Channel,
    Direction,
    NativeEnvelope,
    RecallCase,
    RecallReason,
)
from interlock.schema.common import Rail
from interlock.sla.clock import DeadlineAuthority

RECEIVED_AT = datetime(2026, 9, 14, 15, 0, 0, tzinfo=UTC)


def a_case(case_id: str = "ILK-2026-0914-000001", **overrides) -> RecallCase:
    defaults = {
        "case_id": case_id,
        "rail": Rail.ACH,
        "direction": Direction.INBOUND,
        "channel": Channel.ACH_R06_REQUEST,
        "original_payment_reference": "091000019887766",
        "amount_cents": 250_000,
        "reason": RecallReason.FRAUD_SCAM,
        "requesting_institution_id": "inst-cedar-trust",
        "responding_institution_id": "inst-harbor-national",
        "original_settled_at": datetime(2026, 9, 14, 11, 0, 0, tzinfo=UTC),
        "received_at": RECEIVED_AT,
        "native": NativeEnvelope(
            message_id=f"CT-{case_id}", reason_code="R06", creation_time=RECEIVED_AT
        ),
    }
    return RecallCase(**(defaults | overrides))


@pytest.fixture
def repo() -> SqliteCaseRepository:
    return SqliteCaseRepository(":memory:")


class TestStore:
    def test_open_and_read_back(self, repo: SqliteCaseRepository) -> None:
        opened = repo.open_new(a_case())
        fetched = repo.get(opened.case_id)
        assert fetched is not None
        assert fetched.case == opened.case
        assert fetched.state is opened.state

    def test_a_replayed_message_cannot_overwrite_history(self, repo: SqliteCaseRepository) -> None:
        repo.open_new(a_case())
        with pytest.raises(ValueError, match="already exists"):
            repo.open_new(a_case())

    def test_history_survives_a_round_trip(self, repo: SqliteCaseRepository) -> None:
        opened = repo.open_new(a_case())
        moved = opened.apply(Transition.ACKNOWLEDGE, actor="operator-1")
        repo.save_transition(opened, moved)

        fetched = repo.get(opened.case_id)
        assert fetched is not None
        assert [s.transition for s in fetched.history] == [Transition.ACKNOWLEDGE]
        assert fetched.history[0].actor == "operator-1"

    def test_saving_two_steps_at_once_is_refused(self, repo: SqliteCaseRepository) -> None:
        """Each step gets its own chain entry, so they must be saved one at a time."""
        opened = repo.open_new(a_case())
        two = opened.apply(Transition.ACKNOWLEDGE, actor="op").apply(
            Transition.BEGIN_INVESTIGATION, actor="op"
        )
        with pytest.raises(ValueError, match="exactly one new transition"):
            repo.save_transition(opened, two)

    def test_open_cases_excludes_disposed(self, repo: SqliteCaseRepository) -> None:
        a = repo.open_new(a_case("ILK-2026-0914-000001"))
        repo.open_new(a_case("ILK-2026-0914-000002"))
        repo.save_transition(a, a.apply(Transition.DISPOSE_FUNDS_RETURNED, actor="op"))

        assert {c.case_id for c in repo.open_cases()} == {"ILK-2026-0914-000002"}
        assert len(repo.all_cases()) == 2

    def test_the_chain_is_global_not_per_case(self, repo: SqliteCaseRepository) -> None:
        """A per-case chain would let a whole case be deleted undetectably."""
        repo.open_new(a_case("ILK-2026-0914-000001"))
        repo.open_new(a_case("ILK-2026-0914-000002"))
        sequences = [e.entry.sequence_number for e in repo.audit_entries()]
        assert sequences == [0, 1]


class TestTheChainDetectsTampering:
    def _populated(self) -> SqliteCaseRepository:
        repo = SqliteCaseRepository(":memory:")
        for n in (1, 2, 3):
            case_id = f"ILK-2026-0914-00000{n}"
            opened = repo.open_new(a_case(case_id))
            acked = opened.apply(Transition.ACKNOWLEDGE, actor="operator-1")
            repo.save_transition(opened, acked)
            repo.save_transition(
                acked,
                acked.apply(
                    Transition.DISPOSE_INSUFFICIENT_FUNDS,
                    actor="operator-1",
                    reason="Account emptied before the request arrived",
                ),
            )
        return repo

    def test_an_untampered_chain_verifies(self) -> None:
        assert rebuild_chain(self._populated().audit_entries()) == []

    def test_an_edited_payload_is_caught(self) -> None:
        """The chain still links; only re-digesting the payload catches this.

        The nastiest case, and the one a naive implementation misses: an
        examiner handed a cryptographically sound chain describing events that
        did not happen.
        """
        repo = self._populated()
        repo._conn.execute(
            "UPDATE audit SET payload_json = ? WHERE sequence_number = 1",
            (json.dumps({"case_id": "ILK-2026-0914-000001", "transition": "acknowledge"}),),
        )
        repo._conn.commit()

        problems = rebuild_chain(repo.audit_entries())
        assert any("payload" in p for p in problems), problems

    def test_a_deleted_entry_is_caught(self) -> None:
        repo = self._populated()
        repo._conn.execute("DELETE FROM audit WHERE sequence_number = 4")
        repo._conn.commit()

        problems = rebuild_chain(repo.audit_entries())
        assert any("sequence gap" in p or "removed" in p for p in problems), problems

    def test_a_modified_entry_breaks_everything_after_it(self) -> None:
        repo = self._populated()
        entries = repo.audit_entries()
        target = entries[2].entry
        forged = target.model_copy(update={"correlation_id": "ILK-9999-9999-999999"})
        repo._conn.execute(
            "UPDATE audit SET entry_json = ? WHERE sequence_number = ?",
            (forged.model_dump_json(), target.sequence_number),
        )
        repo._conn.commit()

        assert rebuild_chain(repo.audit_entries())


class TestEvidenceExport:
    def _export(self) -> dict:
        repo = SqliteCaseRepository(":memory:")

        # One answered in time.
        a = repo.open_new(a_case("ILK-2026-0914-000001"))
        repo.save_transition(
            a,
            a.apply(
                Transition.DISPOSE_FUNDS_RETURNED,
                actor="operator-1",
                at=RECEIVED_AT + timedelta(days=2),
            ),
        )
        # One answered late.
        b = repo.open_new(a_case("ILK-2026-0914-000002"))
        repo.save_transition(
            b,
            b.apply(
                Transition.ACKNOWLEDGE_SLA_EXPIRY,
                actor="operator-1",
                reason="No response from the counterparty",
                at=RECEIVED_AT + timedelta(days=45),
            ),
        )
        # One still open.
        repo.open_new(a_case("ILK-2026-0914-000003"))

        return build_export(
            cases=repo.all_cases(),
            entries=repo.audit_entries(),
            period_start=RECEIVED_AT,
            period_end=RECEIVED_AT + timedelta(days=60),
            institution_id="inst-harbor-national",
        )

    def test_export_verifies_standalone(self) -> None:
        """The property that makes it worth anything: checkable without us."""
        assert verify_export(self._export()) == []

    def test_export_declares_the_format_is_ours(self) -> None:
        export = self._export()
        assert export["format"] == EXPORT_FORMAT
        assert "Interlock's own" in export["disclaimer"]

    def test_obligations_are_split_by_whether_the_deadline_is_real(self) -> None:
        export = self._export()
        ach = next(o for o in export["obligations"] if o["rail"] == "ach")
        assert ach["deadline_is_binding"] is True
        assert ach["answered_within_window"] == 1
        assert ach["breached"] == 1
        assert ach["still_open"] == 1
        assert ach["compliance_rate"] == pytest.approx(0.5)

    def test_disposition_completeness_is_zero_by_construction(self) -> None:
        export = self._export()
        assert export["disposition_completeness"]["closed_without_disposition"] == 0
        assert "by construction" in export["disposition_completeness"]["note"]

    def test_verification_catches_a_tampered_export(self) -> None:
        """An examiner must not have to trust the export's own 'intact' flag."""
        export = self._export()
        assert export["verification"]["intact"] is True

        export["chain"][1]["payload"]["amount_cents"] = 1
        problems = verify_export(export)
        assert problems
        assert any("payload" in p for p in problems)

    def test_verification_catches_a_removed_link(self) -> None:
        export = self._export()
        del export["chain"][2]
        assert verify_export(export)

    def test_house_policy_rails_are_not_reported_as_obligations(self) -> None:
        repo = SqliteCaseRepository(":memory:")
        repo.open_new(
            a_case(
                "ILK-2026-0914-000010",
                rail=Rail.FEDNOW,
                channel=Channel.CAMT_056,
                native=NativeEnvelope(
                    message_id="FN-1", reason_code="FRAD", creation_time=RECEIVED_AT
                ),
            )
        )
        export = build_export(
            cases=repo.all_cases(),
            entries=repo.audit_entries(),
            period_start=RECEIVED_AT,
            period_end=RECEIVED_AT + timedelta(days=60),
            institution_id="inst-harbor-national",
        )
        fednow = next(o for o in export["obligations"] if o["rail"] == "fednow")
        assert fednow["deadline_is_binding"] is False
        assert "house policy" in fednow["deadline_basis"].lower()
        assert "not an obligation" in fednow["deadline_basis"].lower()


class TestSummaries:
    def test_compliance_rate_is_none_when_nothing_has_closed(self) -> None:
        repo = SqliteCaseRepository(":memory:")
        repo.open_new(a_case())
        summary = summarise_obligations(repo.all_cases())[0]
        assert summary.compliance_rate is None
        assert summary.still_open == 1

    def test_ach_deadlines_are_binding_and_fednow_are_not(self) -> None:
        repo = SqliteCaseRepository(":memory:")
        repo.open_new(a_case("ILK-2026-0914-000001"))
        repo.open_new(
            a_case(
                "ILK-2026-0914-000002",
                rail=Rail.FEDNOW,
                channel=Channel.CAMT_056,
                native=NativeEnvelope(
                    message_id="FN-2", reason_code="FRAD", creation_time=RECEIVED_AT
                ),
            )
        )
        by_rail = {s.rail: s for s in summarise_obligations(repo.all_cases())}
        assert by_rail[Rail.ACH].deadline_is_binding
        assert not by_rail[Rail.FEDNOW].deadline_is_binding


class TestDeadlineAuthorityIsCarriedThrough:
    def test_ach_is_a_rail_rule(self) -> None:
        repo = SqliteCaseRepository(":memory:")
        opened = repo.open_new(a_case())
        assert opened.deadline.authority is DeadlineAuthority.RAIL_RULE

    def test_fednow_is_house_policy(self) -> None:
        repo = SqliteCaseRepository(":memory:")
        opened = repo.open_new(
            a_case(
                rail=Rail.FEDNOW,
                channel=Channel.CAMT_056,
                native=NativeEnvelope(
                    message_id="FN-3", reason_code="FRAD", creation_time=RECEIVED_AT
                ),
            )
        )
        assert opened.deadline.authority is DeadlineAuthority.HOUSE_POLICY
        assert "not agreed" in opened.deadline.reason
