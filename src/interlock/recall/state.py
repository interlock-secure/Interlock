"""The case state machine, and the disposition guarantee it exists to enforce.

The product's core claim is that no case goes quiet. That claim is worth
nothing as a policy - policies are what a tired operator works around at 17:55
on a Friday - so it is enforced by the type system instead.

How the guarantee is constructed
--------------------------------
There is exactly one way to reach :attr:`~interlock.schema.case.CaseState.DISPOSED`:
apply a transition that carries a :class:`~interlock.schema.recall.RecallDispositionCode`.
Every such transition is defined in :class:`Transition` with its code attached,
so a disposing transition without a disposition is not a bug to catch at
runtime - it is a thing that cannot be written.

Everything else follows from that. Window expiry does not close a case; an
operator acknowledging the expiry does, and that acknowledgement is itself a
disposition. A case nobody has touched stays open and visible, which is the
correct outcome: the failure mode this product removes is the case that
disappears, not the case that is late.

Immutability
------------
:meth:`CaseFile.apply` returns a new file. Nothing mutates. The ledger records
a sequence of states rather than one object whose history has been overwritten,
and an illegal transition therefore cannot leave a case half-changed - the
caller simply still holds the old one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from interlock.schema.case import CaseState, RecallCase
from interlock.schema.common import utc_now
from interlock.schema.recall import RecallDispositionCode
from interlock.sla.clock import Deadline, deadline_for


class IllegalTransitionError(RuntimeError):
    """A transition that is not legal from the current state.

    A RuntimeError rather than a ValueError so that a broad ``except
    ValueError`` around request parsing cannot swallow it.
    """


class Transition(StrEnum):
    """Everything that can be done to a case.

    The disposing transitions each carry a disposition code (see
    :data:`DISPOSITION_OF`). That mapping is what makes the guarantee
    structural: there is no generic ``close`` verb to call without one.
    """

    ACKNOWLEDGE = "acknowledge"
    BEGIN_INVESTIGATION = "begin_investigation"

    DISPOSE_FUNDS_RETURNED = "dispose_funds_returned"
    DISPOSE_PARTIAL_RETURN = "dispose_partial_return"
    DISPOSE_FUNDS_FROZEN = "dispose_funds_frozen"
    DISPOSE_INSUFFICIENT_FUNDS = "dispose_insufficient_funds"
    DISPOSE_ACCOUNT_HOLDER_DISPUTES = "dispose_account_holder_disputes"
    DISPOSE_DECLINED = "dispose_declined"
    ACKNOWLEDGE_SLA_EXPIRY = "acknowledge_sla_expiry"
    """Not a way of avoiding a disposition - it is one.

    The window ran out and a named operator has accepted that it did. The
    alternative design, where expiry quietly closes a case on its own, is
    exactly the silence this system exists to remove.
    """


DISPOSITION_OF: dict[Transition, RecallDispositionCode] = {
    Transition.DISPOSE_FUNDS_RETURNED: RecallDispositionCode.FUNDS_RETURNED,
    Transition.DISPOSE_PARTIAL_RETURN: RecallDispositionCode.PARTIAL_RETURN,
    Transition.DISPOSE_FUNDS_FROZEN: RecallDispositionCode.FUNDS_FROZEN,
    Transition.DISPOSE_INSUFFICIENT_FUNDS: RecallDispositionCode.INSUFFICIENT_FUNDS,
    Transition.DISPOSE_ACCOUNT_HOLDER_DISPUTES: RecallDispositionCode.ACCOUNT_HOLDER_DISPUTES,
    Transition.DISPOSE_DECLINED: RecallDispositionCode.DECLINED_WITH_REASON,
    Transition.ACKNOWLEDGE_SLA_EXPIRY: RecallDispositionCode.SLA_EXPIRED_ACKNOWLEDGED,
}
"""Which disposition each disposing transition records.

Every key reaches DISPOSED; every transition not listed here cannot. A reviewer
checking the guarantee reads this dict and :data:`LEGAL_FROM` and needs nothing
else.
"""

LEGAL_FROM: dict[CaseState, frozenset[Transition]] = {
    CaseState.RECEIVED: frozenset(
        {Transition.ACKNOWLEDGE, Transition.BEGIN_INVESTIGATION, *DISPOSITION_OF}
    ),
    CaseState.ACKNOWLEDGED: frozenset({Transition.BEGIN_INVESTIGATION, *DISPOSITION_OF}),
    CaseState.INVESTIGATING: frozenset(DISPOSITION_OF),
    CaseState.DISPOSED: frozenset(),
}
"""Legal transitions out of each state.

A case may be disposed straight from RECEIVED without acknowledgement, which is
realistic: a counterparty can answer immediately, and forcing a ceremonial
acknowledgement first would mean the fastest real answers looked like protocol
violations.

DISPOSED has no outgoing transitions at all. Reopening is deliberately absent:
a case that needs revisiting is a new case referencing the old one, so the
first outcome stays on the record rather than being edited away.
"""


@dataclass(frozen=True, slots=True)
class HistoryStep:
    """One applied transition, for the ledger and the console timeline."""

    transition: Transition
    actor: str
    at: datetime
    reason: str | None = None
    ai_suggested: Transition | None = None
    """What the AI recommended, if a recommendation was on screen when this
    step was recorded. Kept so the audit trail shows whether a person followed
    the model or overrode it. The model itself never applies a step."""


@dataclass(frozen=True, slots=True)
class CaseFile:
    """A case plus where it has got to.

    Separate from :class:`~interlock.schema.case.RecallCase`, which is the
    immutable set of facts that arrived on the wire. This is what happens to
    those facts afterwards, and keeping them apart means a replayed message
    cannot silently rewrite a case's history.
    """

    case: RecallCase
    state: CaseState
    deadline: Deadline
    history: tuple[HistoryStep, ...] = ()

    disposition: RecallDispositionCode | None = None
    disposed_by: str | None = None
    disposed_at: datetime | None = None
    disposition_reason: str | None = None

    def __post_init__(self) -> None:
        """The invariant, enforced on every construction.

        An earlier version put the guarantee entirely inside :meth:`apply`,
        and a review found three ways round it: the constructor directly,
        ``dataclasses.replace``, and - the one that mattered - the repository
        rehydrating a row from storage. A database row with ``state='disposed'``
        and ``disposition IS NULL``, from a partial write or a future bug,
        became exactly the object the product claims cannot exist, and the
        evidence export then printed ``closed_without_disposition: 1`` directly
        beside the words "zero by construction".

        The property test and the 66,430-sequence walk could not find it,
        because both only ever call :meth:`apply`. So the check lives here,
        where every path must pass through it, and the storage layer carries a
        matching SQL CHECK so a bad row cannot even be written.
        """
        if self.state.is_terminal and self.disposition is None:
            raise ValueError(
                f"{self.case.case_id} is {self.state.value} with no disposition. A closed "
                f"case without a recorded outcome is the exact failure this system exists "
                f"to remove; if this came from storage, the row is corrupt."
            )
        if self.disposition is not None and not self.state.is_terminal:
            raise ValueError(
                f"{self.case.case_id} carries a disposition while still {self.state.value}. "
                f"The two must not drift apart."
            )
        # An outcome nobody is accountable for, at no known time, is not
        # evidence of anything - a review pointed out both could be left empty.
        if self.state.is_terminal and (
            not self.disposed_by or not self.disposed_by.strip() or self.disposed_at is None
        ):
            raise ValueError(
                f"{self.case.case_id} is {self.state.value} without a recorded actor and time. "
                f"A disposition must say who made it and when."
            )
        if not self.state.is_terminal and (
            self.disposed_by is not None or self.disposed_at is not None
        ):
            raise ValueError(
                f"{self.case.case_id} records who disposed it while still {self.state.value}."
            )

    # -- queries -----------------------------------------------------------

    @property
    def case_id(self) -> str:
        return self.case.case_id

    @property
    def is_closed(self) -> bool:
        return self.state.is_terminal

    def is_breached(self, *, at: datetime | None = None) -> bool:
        """Whether the window has passed.

        Note what this does *not* do: it does not close the case, and it does
        not set a disposition. A breached case is an open case that is late,
        and it stays in the queue until somebody accounts for it.
        """
        return self.deadline.is_breached(at=at)

    def legal_transitions(self) -> frozenset[Transition]:
        return LEGAL_FROM[self.state]

    # -- the only mutator --------------------------------------------------

    def apply(
        self,
        transition: Transition,
        *,
        actor: str,
        reason: str | None = None,
        at: datetime | None = None,
        ai_suggested: Transition | None = None,
    ) -> CaseFile:
        """Apply a transition, returning a new file.

        Args:
            actor: who did this. Required and non-empty - an unattributed
                state change is not evidence of anything.
            reason: required for dispositions that will be quoted to a
                customer or a regulator.
            at: the moment it happened, for replay and tests.

        Raises:
            IllegalTransitionError: the transition is not legal from this state.
            ValueError: the actor is missing, or a reason is owed and absent.
        """
        if not actor or not actor.strip():
            raise ValueError(
                "actor is required: an unattributed state change cannot serve as evidence"
            )

        now = at or utc_now()
        # A step cannot predate the case or the step before it. A third review
        # dated a disposal a month before the request arrived; it counted as
        # answered in time and verified clean.
        earliest = self.history[-1].at if self.history else self.case.received_at
        if now < earliest:
            raise ValueError(
                f"{self.case_id}: a step at {now.isoformat()} would precede "
                f"{earliest.isoformat()}; history only runs forwards"
            )

        if self.state.is_terminal:
            raise IllegalTransitionError(
                f"{self.case_id} is terminal ({self.state.value}); a case that needs "
                f"revisiting is a new case referencing this one, so the recorded outcome "
                f"stays on the record"
            )

        if transition not in LEGAL_FROM[self.state]:
            legal = ", ".join(sorted(t.value for t in LEGAL_FROM[self.state])) or "none"
            raise IllegalTransitionError(
                f"{transition.value} is not legal from {self.state.value}; legal here: {legal}"
            )

        disposition = DISPOSITION_OF.get(transition)

        if disposition is None:
            next_state = (
                CaseState.ACKNOWLEDGED
                if transition is Transition.ACKNOWLEDGE
                else CaseState.INVESTIGATING
            )
            return replace(
                self,
                state=next_state,
                history=(
                    *self.history,
                    HistoryStep(transition, actor.strip(), now, reason, ai_suggested),
                ),
            )

        # -- disposing --------------------------------------------------

        if transition is Transition.ACKNOWLEDGE_SLA_EXPIRY and not self.is_breached(at=now):
            raise IllegalTransitionError(
                f"{self.case_id} has not breached its window yet (due {self.deadline.due_at}); "
                f"acknowledging an expiry that has not happened would turn this into a "
                f"convenient way to close an awkward case"
            )

        if disposition.requires_reason and not (reason and reason.strip()):
            raise ValueError(
                f"{disposition.value} requires a reason; this outcome gets quoted to a "
                f"customer or a regulator and is unusable without a stated basis"
            )

        return replace(
            self,
            state=CaseState.DISPOSED,
            disposition=disposition,
            disposed_by=actor.strip(),
            disposed_at=now,
            disposition_reason=reason.strip() if reason else None,
            history=(
                *self.history,
                HistoryStep(transition, actor.strip(), now, reason, ai_suggested),
            ),
        )


def open_case(case: RecallCase, *, received_at: datetime | None = None) -> CaseFile:
    """Start a case file.

    The deadline is computed once, here, and carried on the file. Recomputing
    it per request would let a case's due date drift if the rail matrix changed
    underneath it - which is exactly the sort of silent change a compliance
    record must not have.
    """
    return CaseFile(
        case=case,
        state=CaseState.RECEIVED,
        deadline=deadline_for(case, received_at=received_at),
    )
