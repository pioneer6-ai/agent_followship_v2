"""
Tests for booking-window enforcement in core.data_access and
agent.action_handlers.

The clinic's configured `booking_window_days` must bound how far ahead a
slot may be offered, independent of (and never wider than) the 60-day
safety cap that already existed.
"""

from datetime import date, timedelta

from core.data_access import MockCalendarIntegration
from agent.action_handlers import AppointmentScheduler

from tests.conftest import make_case


class TestCalendarIntegrationBookingWindow:
    def test_default_behavior_unchanged_without_to_date(self):
        """Omitting to_date must behave exactly as before (60-day cap only)."""
        calendar = MockCalendarIntegration()
        after = date(2026, 9, 24)
        slots = calendar.find_available_slots("cleaning", after=after, limit=5)
        assert all(s <= after + timedelta(days=60) for s in slots)
        assert len(slots) == 5

    def test_to_date_narrows_the_search_window(self):
        calendar = MockCalendarIntegration()
        after = date(2026, 9, 24)
        narrow_deadline = after + timedelta(days=7)

        slots = calendar.find_available_slots(
            "cleaning", after=after, limit=20, to_date=narrow_deadline
        )
        assert slots, "expected at least one weekday slot within 7 days"
        assert all(s <= narrow_deadline for s in slots)

    def test_to_date_cannot_widen_beyond_the_60_day_safety_cap(self):
        calendar = MockCalendarIntegration()
        after = date(2026, 9, 24)
        far_future = after + timedelta(days=120)

        slots = calendar.find_available_slots(
            "cleaning", after=after, limit=200, to_date=far_future
        )
        assert all(s <= after + timedelta(days=60) for s in slots)

    def test_no_slots_returned_when_window_excludes_every_weekday(self):
        calendar = MockCalendarIntegration()
        # A Friday: the very next day (Saturday) and day after (Sunday) are
        # weekends, so a 2-day window offers nothing.
        friday = date(2026, 9, 25)
        assert friday.weekday() == 4
        slots = calendar.find_available_slots(
            "cleaning", after=friday, limit=5, to_date=friday + timedelta(days=2)
        )
        assert slots == []


class TestAppointmentSchedulerBookingWindow:
    def test_find_available_slots_forwards_to_date(self):
        calendar = MockCalendarIntegration()
        scheduler = AppointmentScheduler(calendar)
        case = make_case()
        after = date(2026, 9, 24)
        narrow_deadline = after + timedelta(days=7)

        slots = scheduler.find_available_slots(
            case, after=after, limit=20, to_date=narrow_deadline
        )
        assert all(s <= narrow_deadline for s in slots)

    def test_try_book_without_preferred_date_respects_to_date(self):
        calendar = MockCalendarIntegration()
        # A Friday with a 2-day window: the search starts the next day
        # (Saturday) and the window closes on Sunday, so no weekday slot
        # falls inside it and booking must fail.
        friday = date(2026, 9, 25)
        assert friday.weekday() == 4
        narrow_deadline = friday + timedelta(days=2)
        scheduler = AppointmentScheduler(calendar)
        case = make_case()

        success, booked_date = scheduler.try_book(
            case, after=friday, to_date=narrow_deadline
        )
        assert success is False
        assert booked_date is None

    def test_try_book_without_preferred_date_books_within_window(self):
        calendar = MockCalendarIntegration()
        after = date(2026, 9, 24)
        wide_deadline = after + timedelta(days=30)
        scheduler = AppointmentScheduler(calendar)
        case = make_case()

        success, booked_date = scheduler.try_book(
            case, after=after, to_date=wide_deadline
        )
        assert success is True
        assert booked_date is not None
        assert booked_date <= wide_deadline
