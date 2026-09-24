"""The verifier, attacked the way an institution with something to hide would.

The earlier tamper tests edited payloads and removed links. The second review
showed the more realistic attack leaves the chain alone and edits the summary:
turn breaches into compliance, or drop the blocks the verifier would check.
Every test here is one of those routes, and each must be reported.
"""

from __future__ import annotations

import copy
from datetime import timedelta

import pytest

from interlock.recall.evidence import build_export, verify_export
from interlock.recall.state import Transition
from interlock.recall.store import SqliteCaseRepository
from interlock.sla.clock import answered_in_time, deadline_for
from tests.unit.test_ledger_and_evidence import RECEIVED_AT, a_case


def _export_with(repo: SqliteCaseRepository) -> dict:
    return build_export(
        cases=repo.all_cases(),
        entries=repo.audit_entries(),
        period_start=RECEIVED_AT,
        period_end=RECEIVED_AT + timedelta(days=60),
        institution_id="inst-harbor-national",
    )


@pytest.fixture
def export() -> dict:
    """One answered in time, one late disposal, one expiry, one still open."""
    repo = SqliteCaseRepository(":memory:")
    due = deadline_for(a_case()).due_at

    a = repo.open_new(a_case("ILK-2026-0914-000001"))
    repo.save_transition(
        a,
        a.apply(Transition.DISPOSE_FUNDS_RETURNED, actor="op", at=RECEIVED_AT + timedelta(days=1)),
    )
    b = repo.open_new(a_case("ILK-2026-0914-000002"))
    repo.save_transition(
        b,
        b.apply(
            Transition.DISPOSE_INSUFFICIENT_FUNDS,
            actor="op",
            reason="Emptied",
            at=due + timedelta(days=3),
        ),
    )
    c = repo.open_new(a_case("ILK-2026-0914-000003"))
    repo.save_transition(
        c,
        c.apply(
            Transition.ACKNOWLEDGE_SLA_EXPIRY,
            actor="op",
            reason="No response",
            at=due + timedelta(days=30),
        ),
    )
    repo.open_new(a_case("ILK-2026-0914-000004"))
    return _export_with(repo)


def _ach(export: dict) -> dict:
    return next(o for o in export["obligations"] if o["rail"] == "ach")


class TestTheHonestExport:
    def test_verifies(self, export: dict) -> None:
        assert verify_export(export) == []

    def test_figures_are_what_happened(self, export: dict) -> None:
        ach = _ach(export)
        assert (ach["answered_within_window"], ach["breached"], ach["still_open"]) == (1, 2, 1)
        assert ach["compliance_rate"] == pytest.approx(1 / 3)


class TestEditedSummariesAreCaught:
    def test_flipping_breaches_to_compliance(self, export: dict) -> None:
        """The attack the second review found passing: totals intact, split flipped."""
        ach = _ach(export)
        ach["answered_within_window"] += ach["breached"]
        ach["breached"] = 0
        ach["compliance_rate"] = 1.0
        problems = verify_export(export)
        assert any("answered_within_window" in p for p in problems)
        assert any("compliance_rate" in p for p in problems)

    def test_only_the_rate_is_edited(self, export: dict) -> None:
        _ach(export)["compliance_rate"] = 0.99
        assert any("compliance_rate" in p for p in verify_export(export))

    def test_rate_as_a_string_is_rejected(self, export: dict) -> None:
        _ach(export)["compliance_rate"] = "0.3333333333333333"
        assert any("compliance_rate" in p for p in verify_export(export))

    def test_still_open_is_hidden(self, export: dict) -> None:
        ach = _ach(export)
        ach["still_open"] = 0
        ach["total_cases"] -= 1
        assert verify_export(export)

    def test_deleting_the_obligations_block(self, export: dict) -> None:
        del export["obligations"]
        assert any("omits obligations" in p for p in verify_export(export))

    def test_deleting_disposition_completeness(self, export: dict) -> None:
        del export["disposition_completeness"]
        assert any("disposition_completeness" in p for p in verify_export(export))

    def test_dropping_a_rail_from_the_summary(self, export: dict) -> None:
        export["obligations"] = [o for o in export["obligations"] if o["rail"] != "ach"]
        assert any("missing from the obligations summary" in p for p in verify_export(export))

    def test_claiming_a_nonzero_undisposed_count(self, export: dict) -> None:
        export["disposition_completeness"]["closed_without_disposition"] = 2
        assert verify_export(export)


class TestSelfDeclaredFieldsAreNotTrusted:
    def test_empty_chain_cannot_support_claimed_cases(self, export: dict) -> None:
        """Declaring a zero-length chain used to skip every check beneath it."""
        export["chain"] = []
        export["verification"]["chain_length"] = 0
        assert verify_export(export)

    def test_chain_length_must_be_an_integer(self, export: dict) -> None:
        export["verification"]["chain_length"] = str(len(export["chain"]))
        assert any("chain_length must be an integer" in p for p in verify_export(export))

    def test_boolean_is_not_an_integer_here(self, export: dict) -> None:
        export["verification"]["chain_length"] = True
        assert any("chain_length must be an integer" in p for p in verify_export(export))

    def test_the_intact_flag_is_ignored(self, export: dict) -> None:
        export["chain"][0]["payload"]["amount_cents"] = 1
        export["verification"]["intact"] = True
        export["verification"]["problems"] = []
        assert verify_export(export)

    def test_wrong_format_is_refused(self, export: dict) -> None:
        export["format"] = "something-else"
        assert verify_export(export)


class TestMalformedInputIsReportedNotRaised:
    @pytest.mark.parametrize(
        "damage",
        [
            lambda e: e["chain"].__setitem__(0, "not an object"),
            lambda e: e["chain"][0].pop("entry_hash"),
            lambda e: e["chain"][0].__setitem__("sequence_number", "zero"),
            lambda e: e["chain"][1].__setitem__("payload", None),
            lambda e: e.__setitem__("chain", None),
            lambda e: e.__setitem__("verification", "gone"),
            lambda e: e["obligations"].append("junk"),
        ],
    )
    def test_damage_is_a_problem_not_a_crash(self, export: dict, damage) -> None:
        damaged = copy.deepcopy(export)
        damage(damaged)
        assert verify_export(damaged)

    def test_unreadable_disposal_time_counts_against_the_institution(self, export: dict) -> None:
        link = next(
            x for x in export["chain"] if x["payload"].get("disposition") == "funds_returned"
        )
        link["payload"]["at"] = "yesterday-ish"
        problems = verify_export(export)
        assert any("no readable time" in p for p in problems)


class TestTheDeadlineBoundaryIsOneRule:
    def test_disposal_at_exactly_due_is_late_everywhere(self) -> None:
        """Clock, audit entry, summary and verifier must agree on the instant itself."""
        repo = SqliteCaseRepository(":memory:")
        due = deadline_for(a_case()).due_at
        opened = repo.open_new(a_case())
        closed = opened.apply(Transition.DISPOSE_FUNDS_RETURNED, actor="op", at=due)
        repo.save_transition(opened, closed)

        assert answered_in_time(due, due) is False
        disposal = repo.audit_entries()[-1].payload
        # Stored as text because audit payloads are str | int | None.
        assert disposal["breached_at_transition"] == "True"

        export = _export_with(repo)
        assert _ach(export)["breached"] == 1
        assert _ach(export)["answered_within_window"] == 0
        assert verify_export(export) == []

    def test_one_second_early_is_in_time(self) -> None:
        repo = SqliteCaseRepository(":memory:")
        due = deadline_for(a_case()).due_at
        opened = repo.open_new(a_case())
        repo.save_transition(
            opened,
            opened.apply(
                Transition.DISPOSE_FUNDS_RETURNED, actor="op", at=due - timedelta(seconds=1)
            ),
        )
        export = _export_with(repo)
        assert _ach(export)["answered_within_window"] == 1
        assert verify_export(export) == []


class TestEntriesAgreeWithThemselves:
    def test_moving_a_disposal_time_is_caught(self, export: dict) -> None:
        """Editing 'at' and recomputing digests still leaves the recorded verdict behind."""
        link = next(
            x for x in export["chain"] if x["payload"].get("disposition") == "insufficient_funds"
        )
        link["payload"]["at"] = RECEIVED_AT.isoformat()
        problems = verify_export(export)
        assert any("breached_at_transition" in p for p in problems)


class TestBuildExportRefusesPartialInputs:
    def test_filtered_case_list(self) -> None:
        repo = SqliteCaseRepository(":memory:")
        repo.open_new(a_case("ILK-2026-0914-000001"))
        repo.open_new(a_case("ILK-2026-0914-000002"))
        with pytest.raises(ValueError, match="cases and entries disagree"):
            build_export(
                cases=repo.all_cases()[:1],
                entries=repo.audit_entries(),
                period_start=RECEIVED_AT,
                period_end=RECEIVED_AT + timedelta(days=1),
                institution_id="inst-harbor-national",
            )

    def test_duplicate_disposal_on_chain_is_reported(self, export: dict) -> None:
        extra = copy.deepcopy(
            next(x for x in export["chain"] if x["payload"].get("disposition") == "funds_returned")
        )
        export["chain"].append(extra)
        assert any("disposed twice" in p for p in verify_export(export))


class TestThirdReview:
    def test_relabelling_a_binding_rail_as_house_policy(self, export: dict) -> None:
        _ach(export).update(deadline_is_binding=False)
        assert any("deadline_is_binding" in p for p in verify_export(export))

    def test_falsified_source_fields(self, export: dict) -> None:
        _ach(export).update(source="x", source_confidence="verified", deadline_basis="made up")
        problems = verify_export(export)
        for name in ("source", "source_confidence", "deadline_basis"):
            assert any(f"{name} does not match" in p for p in problems)

    def test_boolean_counts_are_not_integers(self, export: dict) -> None:
        _ach(export)["still_open"] = True
        assert verify_export(export)

    def test_a_disposal_cannot_predate_the_case(self) -> None:
        repo = SqliteCaseRepository(":memory:")
        opened = repo.open_new(a_case())
        with pytest.raises(ValueError, match="precede"):
            opened.apply(
                Transition.DISPOSE_FUNDS_RETURNED, actor="op", at=RECEIVED_AT - timedelta(days=30)
            )

    def test_a_backdated_disposal_on_the_chain_is_reported(self, export: dict) -> None:
        link = next(
            x for x in export["chain"] if x["payload"].get("disposition") == "funds_returned"
        )
        link["payload"]["at"] = (RECEIVED_AT - timedelta(days=30)).isoformat()
        assert any("before the request arrived" in p for p in verify_export(export))

    @pytest.mark.parametrize(
        "damage",
        [
            lambda e: e["chain"][0]["payload"].__setitem__("case_id", [1]),
            lambda e: e["obligations"][0].__setitem__("rail", [1]),
            lambda e: e["chain"][0].__setitem__("recorded_at", {1}),
            lambda e: [
                x["payload"].__setitem__("at", x["payload"]["at"][:19])
                for x in e["chain"]
                if x["payload"].get("disposition") == "funds_returned"
            ],
        ],
    )
    def test_more_malformed_input_is_reported(self, export: dict, damage) -> None:
        damaged = copy.deepcopy(export)
        damage(damaged)
        assert verify_export(damaged)

    def test_a_non_object_export(self) -> None:
        assert verify_export([1])  # type: ignore[arg-type]
