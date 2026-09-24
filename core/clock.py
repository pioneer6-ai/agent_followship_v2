"""
Clock - a small, explicit abstraction for "what time is it right now".

This exists so that:
  1. The application never hardcodes today's date. The real clock reads
     the actual system time, at call time, in a configurable clinic
     timezone (default: Asia/Singapore).
  2. Anything that needs "now" (the orchestrator's daily cycle, the
     scheduling tools' slot search, the trigger service's actionability
     check) can be given an INJECTED clock in tests, so test behavior is
     deterministic and never depends on the machine's real wall-clock date.

Prefer zoneinfo.ZoneInfo + timezone-aware datetimes throughout, so this
never falls back to naive datetime handling for date-relative logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

DEFAULT_CLINIC_TIMEZONE = "Asia/Singapore"


class Clock(ABC):
    """
    Abstract source of "now" for the application. Implementations must
    return timezone-aware datetimes.
    """

    @abstractmethod
    def now(self) -> datetime:
        """Current timezone-aware datetime in the clinic's timezone."""

    def today(self) -> date:
        """Convenience: just the current date component of `now()`."""
        return self.now().date()


@dataclass
class SystemClock(Clock):
    """
    The real clock. Reads the actual system time via
    `datetime.now(tz=...)` every time `now()` is called - the current
    date/time is therefore never hardcoded and always reflects when the
    application is actually running.

    Attributes:
        timezone_name: IANA timezone name (e.g. "Asia/Singapore"). Defaults
            to the clinic's configured timezone.
    """
    timezone_name: str = DEFAULT_CLINIC_TIMEZONE

    def now(self) -> datetime:
        return datetime.now(tz=ZoneInfo(self.timezone_name))


@dataclass
class FixedClock(Clock):
    """
    A deterministic, injectable clock for tests. Always returns the same
    timezone-aware instant, so date-grounding logic can be tested without
    depending on the machine's actual clock.

    Usage:
        clock = FixedClock(datetime(2026, 9, 23, 10, 0, tzinfo=ZoneInfo("Asia/Singapore")))
    """
    fixed_now: datetime

    def now(self) -> datetime:
        if self.fixed_now.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        return self.fixed_now
