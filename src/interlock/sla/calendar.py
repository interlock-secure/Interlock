"""Federal Reserve banking days.

Nacha's ten-banking-day response obligation is the only enforceable deadline on
any US rail, so getting this arithmetic right is not housekeeping - it is the
one number a regulator could actually hold an institution to.

The rule everyone gets wrong
----------------------------
Federal Reserve Banks and the federal government do not observe holidays the
same way, and the difference only shows up on Saturdays.

From the Federal Reserve's own holiday schedule:

    For holidays falling on Saturday, Federal Reserve Banks and Branches will
    be open the preceding Friday; however, the Board of Governors will be
    closed.

    For holidays falling on Sunday, all Federal Reserve offices will be closed
    the following Monday.

So the two cases are not symmetric:

- **Sunday holiday** - the following Monday is not a banking day. A deadline
  crossing it gains a day.
- **Saturday holiday** - *nothing happens to the banking calendar at all*. The
  preceding Friday stays open, and Saturday was never a banking day. An
  implementation that borrowed the federal-employee rule and closed that Friday
  would compute deadlines a full day late, in the institution's favour, on a
  rule it is legally obliged to meet.

2027 is the year that catches this: Juneteenth falls on Saturday 19 June and
Christmas on Saturday 25 December, and Federal Reserve Banks are open on both
preceding Fridays.

Why the holidays are computed rather than tabulated
---------------------------------------------------
A hardcoded table of dates is correct until the year it runs out, and then it is
silently wrong. These are generated from the rules, so the calendar is right for
any year without anyone remembering to extend it.

Source: https://www.federalreserve.gov/aboutthefed/k8.htm
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

SATURDAY = 5
SUNDAY = 6

MONDAY = 0
THURSDAY = 3


class BankingCalendarError(ValueError):
    """A request the banking calendar cannot answer."""


# ---------------------------------------------------------------------------
# Holiday computation
# ---------------------------------------------------------------------------


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth given weekday of a month. ``n = -1`` means the last one."""
    if n > 0:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (n - 1))

    # Walk back from the first of the next month.
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _fixed_date_holidays(year: int) -> tuple[date, ...]:
    """Holidays that land on a calendar date rather than a weekday.

    These are the only ones the weekend observance rules touch, because the
    floating holidays below are defined as Mondays or a Thursday and can never
    fall on a weekend.
    """
    return (
        date(year, 1, 1),  # New Year's Day
        date(year, 6, 19),  # Juneteenth National Independence Day
        date(year, 7, 4),  # Independence Day
        date(year, 11, 11),  # Veterans Day
        date(year, 12, 25),  # Christmas Day
    )


def _floating_holidays(year: int) -> tuple[date, ...]:
    return (
        _nth_weekday(year, 1, MONDAY, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, MONDAY, 3),  # Washington's Birthday
        _nth_weekday(year, 5, MONDAY, -1),  # Memorial Day
        _nth_weekday(year, 9, MONDAY, 1),  # Labor Day
        _nth_weekday(year, 10, MONDAY, 2),  # Columbus Day
        _nth_weekday(year, 11, THURSDAY, 4),  # Thanksgiving Day
    )


def bank_closure_for(holiday: date) -> date | None:
    """The date Federal Reserve Banks actually close for a given holiday.

    Returns None when the holiday produces no banking closure at all, which is
    the Saturday case and the whole point of this function. See the module
    docstring.
    """
    weekday = holiday.weekday()
    if weekday == SUNDAY:
        return holiday + timedelta(days=1)
    if weekday == SATURDAY:
        return None
    return holiday


@lru_cache(maxsize=32)
def federal_reserve_holidays(year: int) -> frozenset[date]:
    """Every date in a year on which Federal Reserve Banks are closed.

    Note this is a set of *closures*, not of holidays. A Saturday holiday
    appears nowhere in it, correctly: the banks never shut for it.

    A holiday whose observance falls in the next calendar year - New Year's Day
    on a Sunday, observed the following Monday - is deliberately included under
    the year it is observed, not the year it nominally belongs to, so that a
    lookup on 1 January finds it.
    """
    closures: set[date] = set()

    # The year either side, so a New Year's Day observance that slides across a
    # year boundary is not lost at the seam.
    for candidate_year in (year - 1, year, year + 1):
        for holiday in _fixed_date_holidays(candidate_year):
            closure = bank_closure_for(holiday)
            if closure is not None and closure.year == year:
                closures.add(closure)

    closures.update(_floating_holidays(year))
    return frozenset(closures)


# ---------------------------------------------------------------------------
# Banking day arithmetic
# ---------------------------------------------------------------------------


def is_banking_day(day: date) -> bool:
    """True if Federal Reserve Banks are open."""
    if day.weekday() >= SATURDAY:
        return False
    return day not in federal_reserve_holidays(day.year)


def next_banking_day(day: date) -> date:
    """The first banking day strictly after ``day``."""
    candidate = day + timedelta(days=1)
    while not is_banking_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def add_banking_days(start: date, count: int) -> date:
    """The date ``count`` banking days after ``start``.

    Counting starts the day *after* ``start``, which is how a response window
    works: a request received on Monday with a one-banking-day window is due
    Tuesday, not Monday. If ``start`` is itself a weekend or holiday, the count
    begins from the next banking day.

    Raises:
        BankingCalendarError: for a negative count. Deadlines run forwards, and
            a negative value here is a sign bug somewhere upstream rather than a
            request to look backwards.
    """
    if count < 0:
        raise BankingCalendarError(
            f"Cannot add {count} banking days; deadlines run forwards. Check the sign of "
            "the caller's window."
        )

    current = start
    remaining = count
    while remaining > 0:
        current = next_banking_day(current)
        remaining -= 1

    # A zero-day window still cannot fall due on a closed day.
    while not is_banking_day(current):
        current = next_banking_day(current)
    return current


def banking_days_between(start: date, end: date) -> int:
    """How many banking days separate two dates, excluding ``start`` itself.

    The inverse of :func:`add_banking_days`, and tested against it so the two
    cannot drift apart. Returns a negative count when ``end`` precedes
    ``start``, because this one is a measurement rather than a deadline.
    """
    if end == start:
        return 0

    step = 1 if end > start else -1
    count = 0
    current = start
    while current != end:
        current += timedelta(days=step)
        if is_banking_day(current):
            count += step
    return count


def banking_day_deadline(start: datetime, count: int) -> datetime:
    """Resolve a banking-day window into an instant.

    The deadline lands at the end of the target banking day - 23:59:59 UTC -
    rather than at the same clock time as the request. An institution told it
    has ten banking days has until the close of the tenth, and computing
    otherwise would shave up to a day off a window it is legally obliged to
    meet.

    Using UTC for "end of day" is a simplification worth stating: a real
    deployment would use the receiving institution's local business timezone,
    and the difference is up to several hours. It is recorded here rather than
    hidden because it is the kind of assumption that silently becomes a
    compliance gap.

    The docstring once said UTC while the code used ``start.tzinfo``, so a
    caller passing an Eastern timestamp got a deadline five hours later than
    one passing UTC - on a legally binding Nacha window. Both the banking-day
    arithmetic and the end-of-day boundary are now computed in UTC regardless
    of what the caller passed, which is what every other timestamp in the
    system already assumes.
    """
    if start.tzinfo is None:
        raise BankingCalendarError("Timestamps must be timezone-aware; use UTC")

    due_date = add_banking_days(start.astimezone(UTC).date(), count)
    return datetime(due_date.year, due_date.month, due_date.day, 23, 59, 59, tzinfo=UTC)
