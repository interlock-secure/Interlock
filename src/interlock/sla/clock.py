"""When a case is due, and on whose authority.

Every deadline in Interlock comes from here, and every deadline carries the
reason it exists. That second part is not decoration: on three of four rails the
deadline is Interlock's own house policy rather than a rule, and an operator
escalating a case needs to know which kind they are looking at before they
phone a counterparty to say they are late.

The rule this module enforces
-----------------------------
:func:`deadline_for` never invents a number. It asks
:func:`~interlock.schema.rails.require_verified_window` first, and when that
declines - because the rail states no window, or because the rule rests on a
source we could not read - it falls back to house policy **visibly**, recording
which happened on the returned :class:`Deadline`.

That is why the fallback is not buried inside the matrix accessor. A silent
fallback produces a system where nobody can tell a legal obligation from a
preference, which is precisely the confusion the product exists to remove.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from interlock.schema.case import RecallCase
from interlock.schema.common import utc_now
from interlock.schema.rails import (
    Provenance,
    ResponseWindow,
    UnverifiedRuleError,
    WindowUnit,
    house_policy_window,
    profile_for,
    require_verified_window,
)
from interlock.sla.calendar import banking_day_deadline


class DeadlineAuthority(StrEnum):
    """Where a deadline's force comes from."""

    RAIL_RULE = "rail_rule"
    """A rule obliges an answer by this time. Currently ACH alone."""

    HOUSE_POLICY = "house_policy"
    """Interlock's own deadline, applied because the rail sets none we could
    establish. Real operationally, but not something a counterparty agreed to -
    so never presented to them as an obligation."""


@dataclass(frozen=True, slots=True)
class Deadline:
    """When a case is due, and why that is the answer."""

    due_at: datetime
    authority: DeadlineAuthority
    window: ResponseWindow
    reason: str
    """One sentence an operator can read. Rendered in the console next to the
    clock, so the person deciding whether to escalate knows what they are
    holding a counterparty to."""

    @property
    def is_binding(self) -> bool:
        """True only where a rule, not a preference, sets this deadline."""
        return self.authority is DeadlineAuthority.RAIL_RULE

    @property
    def provenance(self) -> Provenance:
        return self.window.provenance

    def remaining(self, *, at: datetime | None = None) -> float:
        """Seconds left. Negative once the deadline has passed."""
        return (self.due_at - (at or utc_now())).total_seconds()

    def is_breached(self, *, at: datetime | None = None) -> bool:
        return not answered_in_time(at or utc_now(), self.due_at)


def answered_in_time(answered_at: datetime, due_at: datetime) -> bool:
    """The single rule for which side of a deadline an instant falls on.

    Strictly before ``due_at`` is in time; ``due_at`` itself is late.

    One function, used by the clock, the evidence summary and the export
    verifier alike. An earlier version had the clock treat ``due_at`` as
    breached while the evidence summary treated it as answered, so a case
    disposed at exactly the deadline carried ``breached_at_transition: True``
    on its own audit entry and counted as compliant in the export built from
    that entry - and verification passed, because nothing compared the two.
    """
    return answered_at < due_at


def _resolve(start: datetime, window: ResponseWindow) -> datetime:
    """Turn a window into an instant, using the right arithmetic for its unit."""
    if window.unit is WindowUnit.BANKING_DAYS:
        return banking_day_deadline(start, window.amount)
    return start + window.as_timedelta()


def deadline_for(case: RecallCase, *, received_at: datetime | None = None) -> Deadline:
    """When this case must be answered.

    The clock starts when the request was received, not when the payment
    settled. Those are different instants and conflating them is how a system
    reports a case as already breached on arrival.

    Args:
        received_at: overrides the case's own timestamp, for replaying history
            and for tests. Production callers should leave it alone.
    """
    start = received_at or case.received_at

    try:
        window = require_verified_window(case.rail, is_fraud_claim=case.is_fraud_claim)
    except UnverifiedRuleError as declined:
        # The visible fallback. The matrix refused to let an unverified or
        # fraud-exempt rule drive a deadline, so we apply our own and say so.
        window = house_policy_window()
        return Deadline(
            due_at=_resolve(start, window),
            authority=DeadlineAuthority.HOUSE_POLICY,
            window=window,
            reason=_house_policy_reason(case, str(declined)),
        )

    return Deadline(
        due_at=_resolve(start, window),
        authority=DeadlineAuthority.RAIL_RULE,
        window=window,
        reason=(
            f"{case.rail.value.upper()} obliges a response within "
            f"{window.amount} {window.unit.value.replace('_', ' ')}, "
            f"whether or not funds are returned."
        ),
    )


def _house_policy_reason(case: RecallCase, declined_because: str) -> str:
    """Explain, in one operator-readable sentence, why our own clock applies."""
    profile = profile_for(case.rail)

    if case.is_fraud_claim and profile.window.fraud_exempt:
        return (
            f"{case.rail.value.upper()} reportedly exempts fraud claims from its response "
            f"window, so no rail deadline applies. Interlock's own 24-hour policy is shown "
            f"instead - a counterparty has not agreed to it."
        )

    if profile.window_is_house_policy:
        return (
            f"{case.rail.value.upper()} states that participants 'should' respond but sets "
            f"no window we could establish. Interlock's own 24-hour policy applies - a "
            f"counterparty has not agreed to it."
        )

    return (
        f"The {case.rail.value.upper()} window could not be verified at a primary source, so "
        f"it must not drive a deadline. Interlock's own 24-hour policy applies instead. "
        f"({declined_because})"
    )


def is_at_risk(deadline: Deadline, *, at: datetime | None = None, fraction: float = 0.25) -> bool:
    """True when less than ``fraction`` of the window remains, and it has not passed.

    Used by the console to escalate before a breach rather than after one. A
    proportion rather than a fixed period, because a quarter of ten banking days
    and a quarter of twenty-four hours are both "time to worry" on their own
    scales, and a fixed threshold would be permanently red on one rail and
    useless on the other.

    Returns False once breached: a breached case is a different state with a
    different action, and lumping the two together loses that distinction in the
    one place an operator needs it.
    """
    span_seconds = _window_span_seconds(deadline)
    remaining = deadline.remaining(at=at or utc_now())
    return 0 < remaining < span_seconds * fraction


def _window_span_seconds(deadline: Deadline) -> float:
    """The window's nominal length in seconds, for proportional warnings.

    Banking days are approximated as calendar days here. The approximation is
    safe because this figure only decides when to show a warning colour, never
    when a case is actually due - that comes from the calendar arithmetic in
    :func:`~interlock.sla.calendar.banking_day_deadline`.
    """
    if deadline.window.unit is WindowUnit.BANKING_DAYS:
        return deadline.window.amount * 86_400.0
    return deadline.window.as_timedelta().total_seconds()
