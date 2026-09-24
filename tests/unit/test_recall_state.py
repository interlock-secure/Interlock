"""The disposition guarantee, proven rather than asserted.

Written before the implementation, as CLAUDE.md requires for anything in
``recall/``. The state machine is the differentiating component and a subtle bug
in it hides for a long time, because the failure mode is a case that goes quiet -
which looks exactly like a case nobody has got to yet.

The central property
--------------------
    No sequence of legal transitions reaches a closed case without a recorded
    disposition.

Asserting that on a handful of hand-written paths proves almost nothing: the
paths a developer thinks to write are the paths they already had in mind when
writing the code. So it is stated as a hypothesis property over generated
transition sequences, and the generator is free to produce illegal moves,
repeated moves and out-of-order moves.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from interlock.recall.state import (
    IllegalTransitionError,
    Transition,
    open_case,
)
from interlock.schema.case import (
    CaseState,
    Channel,
    Direction,
    NativeEnvelope,
    RecallCase,
    RecallReason,
)
from interlock.schema.common import Rail
from interlock.schema.recall import RecallDispositionCode

RECEIVED_AT = datetime(2026, 9, 14, 15, 0, 0, tzinfo=UTC)


def a_case(**overrides) -> RecallCase:
    defaults = {
        "case_id": "ILK-2026-0914-000001",
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
            message_id="CT-RFR-20260914-000001",
            reason_code="R06",
            creation_time=RECEIVED_AT,
        ),
    }
    return RecallCase(**(defaults | overrides))


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------

TRANSITIONS = list(Transition)


@settings(max_examples=2000, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(moves=st.lists(st.sampled_from(TRANSITIONS), min_size=0, max_size=12))
def test_no_path_reaches_a_closed_case_without_a_disposition(moves: list[Transition]) -> None:
    """The guarantee, over generated sequences including illegal ones.

    2,000 examples of up to 12 moves each. Combined with the exhaustive walk
    below, this clears the 10,000-sequence bar in BUILD_PLAN M4.
    """
    file = open_case(a_case())
    well_past_the_deadline = RECEIVED_AT + timedelta(days=120)

    for move in moves:
        try:
            file = file.apply(
                move,
                actor="operator-1",
                reason="generated sequence",
                at=well_past_the_deadline,
            )
        except IllegalTransitionError:
            # An illegal move must leave the case exactly as it was. A partial
            # application is how a case ends up in a state nobody designed.
            continue

    if file.state.is_terminal:
        assert file.disposition is not None, f"Reached {file.state} with no disposition via {moves}"
    else:
        assert file.disposition is None, (
            "A disposition on a non-terminal case means the two can drift apart"
        )


@pytest.mark.slow
def test_exhaustive_walk_of_every_short_sequence() -> None:
    """Every sequence up to length 5, enumerated rather than sampled.

    Hypothesis samples; this proves. Nine transitions to the fifth power is
    66,430 sequences including the empty one, which clears the 10,000 bar in
    BUILD_PLAN M4 by exhaustion rather than by sampling - a stronger claim.
    """
    from itertools import product

    checked = 0
    for length in range(6):
        for moves in product(TRANSITIONS, repeat=length):
            file = open_case(a_case())
            for move in moves:
                try:
                    file = file.apply(
                        move,
                        actor="operator-1",
                        reason="generated sequence",
                        at=RECEIVED_AT + timedelta(days=120),
                    )
                except IllegalTransitionError:
                    continue
            checked += 1
            if file.state.is_terminal:
                assert file.disposition is not None, f"Closed with no disposition via {moves}"

    assert checked > 10_000, f"Only {checked} sequences walked; the bar is 10,000"


# ---------------------------------------------------------------------------
# Specific behaviours
# ---------------------------------------------------------------------------


class TestTerminalStates:
    def test_a_disposed_case_accepts_nothing_further(self) -> None:
        file = open_case(a_case()).apply(Transition.DISPOSE_FUNDS_RETURNED, actor="operator-1")
        assert file.state is CaseState.DISPOSED

        for move in TRANSITIONS:
            with pytest.raises(IllegalTransitionError, match="terminal"):
                file.apply(move, actor="operator-1")

    def test_disposition_is_recorded_with_its_actor_and_time(self) -> None:
        file = open_case(a_case()).apply(
            Transition.DISPOSE_INSUFFICIENT_FUNDS,
            actor="operator-7",
            reason="Account emptied 11 minutes after settlement",
        )
        assert file.disposition is RecallDispositionCode.INSUFFICIENT_FUNDS
        assert file.disposed_by == "operator-7"
        assert file.disposed_at is not None


class TestDispositionsThatOweAReason:
    @pytest.mark.parametrize(
        "move",
        [
            Transition.DISPOSE_DECLINED,
            Transition.DISPOSE_ACCOUNT_HOLDER_DISPUTES,
            Transition.DISPOSE_INSUFFICIENT_FUNDS,
        ],
    )
    def test_reason_is_required(self, move: Transition) -> None:
        """These outcomes get quoted to a customer or a regulator."""
        with pytest.raises(ValueError, match="reason"):
            open_case(a_case()).apply(move, actor="operator-1")

    def test_reason_is_not_required_for_a_clean_return(self) -> None:
        file = open_case(a_case()).apply(Transition.DISPOSE_FUNDS_RETURNED, actor="operator-1")
        assert file.state.is_terminal


class TestSlaExpiryIsADispositionNotAnAbsence:
    """CLAUDE.md rule 1. The distinction is the entire point of the design."""

    def test_expiry_must_be_acknowledged_explicitly(self) -> None:
        file = open_case(a_case())
        long_after = RECEIVED_AT + timedelta(days=90)

        assert file.is_breached(at=long_after)
        # Breaching does not close anything by itself.
        assert not file.state.is_terminal
        assert file.disposition is None

    def test_acknowledging_expiry_closes_the_case_with_a_disposition(self) -> None:
        file = open_case(a_case()).apply(
            Transition.ACKNOWLEDGE_SLA_EXPIRY,
            actor="operator-1",
            reason="No response from counterparty within the window",
            at=RECEIVED_AT + timedelta(days=30),
        )
        assert file.state.is_terminal
        assert file.disposition is RecallDispositionCode.SLA_EXPIRED_ACKNOWLEDGED

    def test_expiry_cannot_be_acknowledged_before_it_expires(self) -> None:
        """Otherwise it becomes a convenient way to close an awkward case."""
        file = open_case(a_case())
        with pytest.raises(IllegalTransitionError, match=r"not.*breached"):
            file.apply(
                Transition.ACKNOWLEDGE_SLA_EXPIRY,
                actor="operator-1",
                reason="premature",
                at=RECEIVED_AT + timedelta(minutes=5),
            )


class TestOrdering:
    def test_investigation_cannot_precede_receipt(self) -> None:
        file = open_case(a_case())
        assert file.state is CaseState.RECEIVED

    def test_acknowledging_twice_is_illegal(self) -> None:
        file = open_case(a_case()).apply(Transition.ACKNOWLEDGE, actor="operator-1")
        with pytest.raises(IllegalTransitionError):
            file.apply(Transition.ACKNOWLEDGE, actor="operator-1")

    def test_a_case_can_be_disposed_without_being_acknowledged(self) -> None:
        """Realistic: a counterparty can answer immediately."""
        file = open_case(a_case()).apply(Transition.DISPOSE_FUNDS_RETURNED, actor="operator-1")
        assert file.state.is_terminal

    def test_history_records_every_applied_transition(self) -> None:
        file = (
            open_case(a_case())
            .apply(Transition.ACKNOWLEDGE, actor="operator-1")
            .apply(Transition.BEGIN_INVESTIGATION, actor="operator-2")
            .apply(Transition.DISPOSE_FUNDS_RETURNED, actor="operator-2")
        )
        assert [step.transition for step in file.history] == [
            Transition.ACKNOWLEDGE,
            Transition.BEGIN_INVESTIGATION,
            Transition.DISPOSE_FUNDS_RETURNED,
        ]
        assert all(step.actor for step in file.history)


class TestImmutability:
    def test_applying_a_transition_returns_a_new_file(self) -> None:
        """The audit chain records a sequence of states, not one overwritten object."""
        first = open_case(a_case())
        second = first.apply(Transition.ACKNOWLEDGE, actor="operator-1")
        assert first.state is CaseState.RECEIVED
        assert second.state is CaseState.ACKNOWLEDGED
        assert first is not second

    def test_an_illegal_transition_changes_nothing(self) -> None:
        file = open_case(a_case()).apply(Transition.DISPOSE_FUNDS_RETURNED, actor="operator-1")
        before = (file.state, file.disposition, len(file.history))
        with pytest.raises(IllegalTransitionError):
            file.apply(Transition.ACKNOWLEDGE, actor="operator-1")
        assert (file.state, file.disposition, len(file.history)) == before


class TestDeadlineComesFromTheMatrix:
    def test_ach_gets_a_binding_rail_deadline(self) -> None:
        file = open_case(a_case(rail=Rail.ACH))
        assert file.deadline.is_binding

    def test_fednow_gets_house_policy_and_says_so(self) -> None:
        file = open_case(
            a_case(
                rail=Rail.FEDNOW,
                channel=Channel.CAMT_056,
                native=NativeEnvelope(
                    message_id="FN-1",
                    reason_code="FRAD",
                    creation_time=RECEIVED_AT,
                ),
            )
        )
        assert not file.deadline.is_binding
        assert "not agreed" in file.deadline.reason
