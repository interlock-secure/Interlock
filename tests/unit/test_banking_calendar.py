"""Federal Reserve banking days, checked against the Fed's published schedule.

The asymmetry these tests exist for: a holiday falling on Saturday leaves
Federal Reserve *Banks* open the preceding Friday - only the Board of Governors
closes - while one falling on Sunday closes everything the following Monday.

Borrowing the federal-employee rule instead would close that Friday and compute
deadlines a full banking day late, in the institution's own favour, on the one
obligation US rails actually enforce.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from interlock.sla.calendar import (
    BankingCalendarError,
    add_banking_days,
    bank_closure_for,
    banking_day_deadline,
    banking_days_between,
    federal_reserve_holidays,
    is_banking_day,
)

# Observed closure dates exactly as the Federal Reserve publishes them.
# https://www.federalreserve.gov/aboutthefed/k8.htm
PUBLISHED = {
    2026: [
        "2026-01-01",
        "2026-01-19",
        "2026-02-16",
        "2026-05-25",
        "2026-06-19",
        "2026-09-07",
        "2026-10-12",
        "2026-11-11",
        "2026-11-26",
        "2026-12-25",
    ],
    2027: [
        "2027-01-01",
        "2027-01-18",
        "2027-02-15",
        "2027-05-31",
        "2027-07-05",
        "2027-09-06",
        "2027-10-11",
        "2027-11-11",
        "2027-11-25",
    ],
}


class TestAgainstThePublishedSchedule:
    @pytest.mark.parametrize("year", sorted(PUBLISHED))
    def test_computed_closures_match_the_fed(self, year: int) -> None:
        assert federal_reserve_holidays(year) == frozenset(
            date.fromisoformat(d) for d in PUBLISHED[year]
        )

    def test_saturday_holidays_close_nothing(self) -> None:
        """2027 is the year that catches a wrong implementation.

        Juneteenth and Christmas both fall on Saturdays, and the Fed's own
        schedule says Banks are open the preceding Fridays.
        """
        for saturday in (date(2027, 6, 19), date(2027, 12, 25), date(2026, 7, 4)):
            assert saturday.weekday() == 5
            assert bank_closure_for(saturday) is None
            preceding_friday = date.fromordinal(saturday.toordinal() - 1)
            assert is_banking_day(preceding_friday), (
                f"Reserve Banks are open on {preceding_friday}; only the Board closes"
            )

    def test_sunday_holidays_close_the_following_monday(self) -> None:
        independence = date(2027, 7, 4)
        assert independence.weekday() == 6
        assert bank_closure_for(independence) == date(2027, 7, 5)
        assert not is_banking_day(date(2027, 7, 5))

    def test_new_year_observance_crossing_a_year_boundary(self) -> None:
        """A Sunday New Year's Day is observed on the Monday - in the next year.

        The year is derived rather than written down: an earlier version of this
        test asserted 1 Jan 2033 was a Sunday, which it is not, and the test
        failed on my arithmetic rather than on the code's.
        """
        sunday_new_year = next(y for y in range(2027, 2060) if date(y, 1, 1).weekday() == 6)
        assert bank_closure_for(date(sunday_new_year, 1, 1)) == date(sunday_new_year, 1, 2)
        assert date(sunday_new_year, 1, 2) in federal_reserve_holidays(sunday_new_year)

        saturday_new_year = next(y for y in range(2027, 2060) if date(y, 1, 1).weekday() == 5)
        assert bank_closure_for(date(saturday_new_year, 1, 1)) is None


class TestBankingDayArithmetic:
    def test_weekends_are_never_banking_days(self) -> None:
        assert not is_banking_day(date(2026, 9, 19))
        assert not is_banking_day(date(2026, 9, 20))
        assert is_banking_day(date(2026, 9, 21))

    def test_counting_starts_the_day_after(self) -> None:
        """A request on Monday with a one-day window is due Tuesday."""
        assert add_banking_days(date(2026, 9, 21), 1) == date(2026, 9, 22)

    def test_a_window_spans_weekends(self) -> None:
        # Mon 14 Sep 2026 + 10 banking days lands a fortnight later, not ten days.
        assert add_banking_days(date(2026, 9, 14), 10) == date(2026, 9, 28)

    def test_a_window_spans_a_holiday(self) -> None:
        # Thanksgiving, Thu 26 Nov 2026, pushes the tenth banking day out by one.
        assert add_banking_days(date(2026, 11, 16), 10) == date(2026, 12, 1)

    def test_a_saturday_holiday_does_not_extend_a_window(self) -> None:
        """The whole point. Christmas 2027 is a Saturday and costs nothing."""
        spanning = add_banking_days(date(2027, 12, 20), 5)
        assert spanning == date(2027, 12, 27)
        assert is_banking_day(date(2027, 12, 24)), "Banks are open that Friday"

    def test_zero_days_still_lands_on_an_open_day(self) -> None:
        assert add_banking_days(date(2026, 9, 19), 0) == date(2026, 9, 21)

    def test_between_is_the_inverse_of_add(self) -> None:
        start = date(2026, 9, 14)
        for n in range(1, 25):
            assert banking_days_between(start, add_banking_days(start, n)) == n

    def test_negative_windows_are_refused(self) -> None:
        with pytest.raises(BankingCalendarError, match="forwards"):
            add_banking_days(date(2026, 9, 14), -1)


class TestDeadlineResolution:
    def test_deadline_lands_at_end_of_the_target_day(self) -> None:
        """Ten banking days means until the close of the tenth, not the same
        clock time - which would shave up to a day off a legal obligation."""
        start = datetime(2026, 9, 14, 15, 0, 0, tzinfo=UTC)
        due = banking_day_deadline(start, 10)
        assert due.date() == date(2026, 9, 28)
        assert (due.hour, due.minute, due.second) == (23, 59, 59)

    def test_naive_timestamps_are_refused(self) -> None:
        with pytest.raises(BankingCalendarError, match="timezone-aware"):
            banking_day_deadline(datetime(2026, 9, 14, 15, 0, 0), 10)
