"""
Unit tests for core.clock.

These confirm the Clock abstraction never silently falls back to a naive or
hardcoded date, and that FixedClock gives deterministic, injectable "now"
for the rest of the test suite.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.clock import DEFAULT_CLINIC_TIMEZONE, FixedClock, SystemClock


class TestSystemClock:
    def test_now_is_timezone_aware(self):
        clock = SystemClock()
        assert clock.now().tzinfo is not None

    def test_default_timezone_is_asia_singapore(self):
        clock = SystemClock()
        assert clock.timezone_name == DEFAULT_CLINIC_TIMEZONE
        assert clock.now().tzinfo.key == "Asia/Singapore"

    def test_custom_timezone_is_respected(self):
        clock = SystemClock(timezone_name="America/New_York")
        assert clock.now().tzinfo.key == "America/New_York"

    def test_today_returns_a_date_not_a_datetime(self):
        clock = SystemClock()
        today = clock.today()
        assert not hasattr(today, "hour")


class TestFixedClock:
    def test_returns_the_fixed_instant_every_time(self):
        fixed = datetime(2026, 9, 24, 10, 0, tzinfo=ZoneInfo("Asia/Singapore"))
        clock = FixedClock(fixed_now=fixed)
        assert clock.now() == fixed
        assert clock.now() == fixed  # calling twice must not drift

    def test_today_matches_the_fixed_date(self):
        fixed = datetime(2026, 9, 24, 23, 59, tzinfo=ZoneInfo("Asia/Singapore"))
        clock = FixedClock(fixed_now=fixed)
        assert clock.today() == fixed.date()

    def test_naive_datetime_is_rejected(self):
        naive = datetime(2026, 9, 24, 10, 0)  # no tzinfo
        clock = FixedClock(fixed_now=naive)
        with pytest.raises(ValueError):
            clock.now()
