"""
SchedulingDatabaseCalendarAdapter - bridges the agent's CalendarIntegration
interface onto the staff calendar's real, persistent scheduling subsystem.

Why this exists:

Before this adapter, the agent (and the Patient Portal, which calls the
agent's own AppointmentScheduler) read/wrote an in-memory
MockCalendarIntegration, while the staff calendar
(web/calendar_routes.py -> scheduling.CalendarService) read/wrote a real
SQLite-backed SchedulingDatabase (scheduling.db). These were two
independent, unsynchronized production data stores - a Patient Portal
booking was invisible to the staff calendar and vice versa.

This adapter makes SchedulingDatabase/CalendarService the single production
source of truth for appointments. It implements the existing
CalendarIntegration interface (so AppointmentScheduler and the whole agent
codebase are unchanged) by delegating every operation to the same
CalendarService the staff calendar routes already use.

MockCalendarIntegration is NOT removed - it remains available (and is still
the default in tests) for tests that explicitly want an isolated,
schema-free in-memory fake. Production wiring (web/app.py) uses this
adapter instead.

Bridging "pick a date" onto a session/time-slot system:

CalendarIntegration.find_available_slots/book_appointment operate on plain
dates (`list[date]`, `book_appointment(patient_id, date, treatment_type)`),
matching the Patient Portal's existing "pick a day" UX. The scheduling
subsystem underneath models slots as (date, session, time) with per-slot
capacity. This adapter bridges the two by:

  - find_available_slots: asking CalendarService for the full slot list in
    the window, then returning each *date* once if it has at least one slot
    with remaining capacity (deduped, sorted) - callers never see session/
    time granularity, matching the pre-adapter MockCalendarIntegration
    behavior of "one bookable unit per date".
  - book_appointment: picking the earliest available (session, time) slot
    on the requested date and booking exactly that slot via
    CalendarService.check_capacity_and_book, which performs the atomic
    capacity check and insert. Confirms the booking immediately (see
    confirm_immediately) rather than leaving it pending, per the Patient
    Portal's booking semantics; use book_appointment_detailed() when the
    caller needs the resulting appointment id/status rather than a bare
    boolean.
  - block_date: not part of the formal CalendarIntegration ABC, but called
    directly by utils.sample_data.initialize_sample_data at real
    application startup, so it must be supported by any calendar object
    passed there. Translated into the scheduling subsystem's existing
    persistent blocked_periods table (SchedulingDatabase.add_blocked_period)
    rather than a second in-memory blocked-date list - see block_date's own
    docstring below.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Optional
from zoneinfo import ZoneInfo

from core.data_access import CalendarIntegration
from scheduling.calendar_service import CalendarService
from scheduling.database import AppointmentStatus, SchedulingDatabase


@dataclass
class SlotOption:
    """
    One concrete, bookable (date, session, time) slot, as surfaced to the
    Patient Portal - the richer sibling of the bare `date` values
    `find_available_slots` returns, for callers (the portal UI) that need
    to show and let the patient pick an exact time, not just a day.

    Attributes:
        slot_date: The calendar date.
        session: Clinic session name (e.g. "morning", "afternoon").
        time: Local start time, "HH:MM", as configured for the clinic.
        datetime_utc: This slot's normalized UTC timestamp - the same
            value used internally to key capacity/booking, and the value
            that must be echoed back (with slot_date/session/time) when
            booking this exact slot.
    """
    slot_date: date
    session: str
    time: str
    datetime_utc: str


@dataclass
class BookingOutcome:
    """
    The result of a single booking attempt against the scheduling
    subsystem, richer than CalendarIntegration.book_appointment's plain
    boolean so callers that need it (the orchestrator's portal-booking
    path) can use the database's own observed result as the source of
    truth rather than inferring success from a boolean alone.

    Attributes:
        success: Whether an appointment record was created and confirmed.
        appointment_id: The SchedulingDatabase appointment_requests.id, if
            successful.
        status: The appointment's status value (e.g. "confirmed") if
            successful.
        error: A short, non-sensitive reason for failure, if unsuccessful.
    """
    success: bool
    appointment_id: Optional[int] = None
    status: Optional[str] = None
    error: Optional[str] = None


class SchedulingDatabaseCalendarAdapter(CalendarIntegration):
    """
    Production CalendarIntegration backed by the scheduling subsystem
    (SchedulingDatabase/CalendarService) instead of an in-memory dict.

    Implements every CalendarIntegration method so AppointmentScheduler and
    the rest of the agent codebase need no changes; adds
    `book_appointment_detailed` for callers (the Patient Portal booking
    path) that need the appointment id/status the database actually
    recorded, rather than trusting a boolean alone.
    """

    def __init__(
        self,
        scheduling_db: SchedulingDatabase,
        calendar_service: Optional[CalendarService] = None,
        *,
        confirm_immediately: bool = True,
        requested_by: str = "patient_portal",
    ) -> None:
        """
        Args:
            scheduling_db: The shared SchedulingDatabase instance - the SAME
                one the staff calendar routes use, so both sides see the
                same rows.
            calendar_service: Optional pre-built CalendarService. Built from
                `scheduling_db` when omitted.
            confirm_immediately: Whether `book_appointment` should mark the
                created appointment CONFIRMED right away (the Patient
                Portal's semantics: a successful, capacity-checked booking
                needs no staff approval) rather than leaving it PENDING
                (the staff-created-appointment default elsewhere in this
                system). Does not affect staff-initiated bookings through
                web/calendar_routes.py at all - those call CalendarService
                directly and are unaffected by this adapter.
            requested_by: Actor name recorded on appointments this adapter
                creates, and used as this adapter's booking source tag.
        """
        self.db = scheduling_db
        self.calendar_service = calendar_service or CalendarService(scheduling_db)
        self.confirm_immediately = confirm_immediately
        self.requested_by = requested_by

    # -- CalendarIntegration interface -----------------------------------

    def find_available_slots(
        self,
        treatment_type: str,
        after: date,
        limit: int = 5,
        to_date: Optional[date] = None,
    ) -> list[date]:
        """
        List distinct dates with at least one available slot, matching
        CalendarIntegration's "one bookable unit per date" contract.

        `treatment_type` is accepted for interface compatibility but is not
        used to filter slots - the current scheduling schema has no
        treatment-type-specific sessions; every session accepts any
        treatment type, same as MockCalendarIntegration's behavior.
        """
        start = after + timedelta(days=1)
        end = to_date if to_date is not None else after + timedelta(days=60)
        if end < start:
            return []

        slots = self.calendar_service.get_availability(
            start.isoformat(), end.isoformat()
        )

        available_dates: list[date] = []
        seen: set[date] = set()
        for slot in sorted(slots, key=lambda s: s["datetime_utc"]):
            if slot["status"] != "available":
                continue
            slot_date = date.fromisoformat(slot["date"])
            if slot_date < start or slot_date > end:
                continue
            if slot_date in seen:
                continue
            seen.add(slot_date)
            available_dates.append(slot_date)
            if len(available_dates) >= limit:
                break

        return available_dates

    def find_available_slot_options(
        self,
        after: date,
        to_date: date,
    ) -> list[SlotOption]:
        """
        Full-granularity availability in [after+1day, to_date]: every
        bookable (date, session, time) slot, not deduped to one-per-date.

        This is what the Patient Portal's slot picker needs to let a
        patient choose a specific time (e.g. "Monday Sep 28, 2:00 PM")
        instead of only a date - `find_available_slots` above stays
        date-only for CalendarIntegration interface compatibility with the
        rest of the agent codebase, which only ever needs "is this date
        bookable at all".

        Args:
            after: Exclusive lower bound (matching find_available_slots'
                own convention - slots start the day AFTER this date).
            to_date: Inclusive upper bound (e.g. the booking window
                deadline).

        Returns:
            SlotOption list, sorted chronologically, for every slot with
            remaining capacity, not blocked. Empty list if the window is
            invalid (to_date before the start) or has no availability.
        """
        start = after + timedelta(days=1)
        if to_date < start:
            return []

        raw_slots = self.calendar_service.get_availability(
            start.isoformat(), to_date.isoformat()
        )

        options = [
            SlotOption(
                slot_date=date.fromisoformat(slot["date"]),
                session=slot["session"],
                time=slot["time"],
                datetime_utc=slot["datetime_utc"],
            )
            for slot in sorted(raw_slots, key=lambda s: s["datetime_utc"])
            if slot["status"] == "available"
        ]
        return options

    def book_specific_slot(
        self,
        patient_id: str,
        slot_date: date,
        session: str,
        time: str,
        treatment_type: str,
        *,
        patient_name: str,
        follow_up_case_id: Optional[str] = None,
        follow_up_reason: Optional[str] = None,
    ) -> BookingOutcome:
        """
        Book the EXACT (date, session, time) slot the caller specifies,
        rather than the earliest available slot on a date (see
        `book_appointment_detailed`).

        This is what the Patient Portal's slot picker uses once a patient
        has chosen a specific time button - the requested slot is
        independently re-validated against clinic configuration and
        capacity by CalendarService.check_capacity_and_book (the same
        atomic check/insert every booking path in this system goes
        through); a tampered or stale (date, session, time) combination
        that is not a real, currently-available slot is rejected there,
        not trusted from the caller.

        Args:
            patient_id: Patient identifier.
            slot_date: The exact date requested.
            session: The exact session name requested (e.g. "morning").
            time: The exact local time requested ("HH:MM").
            treatment_type: Accepted for interface compatibility; not
                persisted as its own column (same as book_appointment_detailed).
            patient_name: Display name to store on the appointment record.
            follow_up_case_id: Optional link back to the FollowUpCase.
            follow_up_reason: Optional human-readable reason.

        Returns:
            BookingOutcome with the appointment id/status on success, or a
            non-sensitive error reason on failure (e.g. "Slot is full",
            or a validation error if the slot doesn't match clinic config).
        """
        config = self.db.get_config()
        tz_name = config.get("timezone_name", "America/New_York")

        normalized_utc, is_valid = self.calendar_service.normalize_slot_timestamp(
            slot_date.isoformat(), time, tz_name
        )
        if not is_valid:
            return BookingOutcome(success=False, error="Invalid slot time.")

        result = self.calendar_service.check_capacity_and_book(
            patient_id=patient_id,
            patient_name=patient_name,
            slot_datetime_utc=normalized_utc,
            slot_date=slot_date.isoformat(),
            slot_session=session,
            slot_time=time,
            requested_by=self.requested_by,
            follow_up_case_id=follow_up_case_id,
            follow_up_reason=follow_up_reason,
            source=self.requested_by,
        )

        if not result.get("success"):
            return BookingOutcome(success=False, error=result.get("error", "Booking failed."))

        appointment_id = result["appointment_id"]

        if self.confirm_immediately:
            approved = self.db.approve_appointment(appointment_id, actor=self.requested_by)
            if not approved:
                self.db.cancel_appointment(
                    appointment_id, actor=self.requested_by,
                    reason="Failed to auto-confirm immediately after creation",
                )
                return BookingOutcome(
                    success=False,
                    error="Could not confirm the appointment; please try again.",
                )
            status_value = AppointmentStatus.CONFIRMED.value
        else:
            status_value = AppointmentStatus.PENDING.value

        return BookingOutcome(success=True, appointment_id=appointment_id, status=status_value)

    def get_last_missed_slot_hint(self, patient_id: str) -> Optional[SlotOption]:
        """
        The patient's most recent CANCELLED/EXPIRED appointment, if any, as
        a SlotOption - used ONLY as a ranking preference signal (see
        `core.slot_ranking`), never as a real appointment or a certainty
        that the patient no-showed. See
        SchedulingDatabase.get_last_non_active_appointment_for_patient for
        why this is a heuristic, not a true no-show record.

        Returns:
            A SlotOption built from that row's session/time, or None if
            the patient has no CANCELLED/EXPIRED history.
        """
        row = self.db.get_last_non_active_appointment_for_patient(patient_id)
        if row is None:
            return None
        return SlotOption(
            slot_date=date.fromisoformat(row["slot_date"]),
            session=row["slot_session"],
            time=row["slot_time"],
            datetime_utc=row["slot_datetime_utc"],
        )

    def book_appointment(
        self, patient_id: str, appointment_date: date, treatment_type: str
    ) -> bool:
        """Plain boolean booking, for CalendarIntegration interface
        compatibility. Prefer `book_appointment_detailed` when the caller
        needs the appointment id/status the database recorded."""
        outcome = self.book_appointment_detailed(
            patient_id, appointment_date, treatment_type, patient_name=patient_id
        )
        return outcome.success

    def book_appointment_detailed(
        self,
        patient_id: str,
        appointment_date: date,
        treatment_type: str,
        *,
        patient_name: str,
        follow_up_case_id: Optional[str] = None,
        follow_up_reason: Optional[str] = None,
    ) -> BookingOutcome:
        """
        Book the earliest available slot on `appointment_date`, returning
        the database's own observed result rather than a bare boolean.

        This performs the SAME atomic capacity/availability check
        (CalendarService.check_capacity_and_book) the staff-created booking
        path uses - there is exactly one code path that actually writes an
        appointment row, regardless of who initiated the booking.

        Args:
            patient_id: Patient identifier.
            appointment_date: The date to book (a specific session/time on
                this date is selected automatically - see module docstring).
            treatment_type: Accepted for interface compatibility; not
                persisted as its own column (the schema tracks
                follow_up_reason/follow_up_case_id instead).
            patient_name: Display name to store on the appointment record.
            follow_up_case_id: Optional link back to the FollowUpCase.
            follow_up_reason: Optional human-readable reason.

        Returns:
            BookingOutcome with the appointment id/status on success, or a
            non-sensitive error reason on failure (e.g. "Slot is full").
        """
        config = self.db.get_config()
        tz_name = config.get("timezone_name", "America/New_York")

        chosen = self._earliest_available_slot(appointment_date, config, tz_name)
        if chosen is None:
            return BookingOutcome(success=False, error="No available slot on that date.")

        result = self.calendar_service.check_capacity_and_book(
            patient_id=patient_id,
            patient_name=patient_name,
            slot_datetime_utc=chosen["datetime_utc"],
            slot_date=chosen["date"],
            slot_session=chosen["session"],
            slot_time=chosen["time"],
            requested_by=self.requested_by,
            follow_up_case_id=follow_up_case_id,
            follow_up_reason=follow_up_reason,
            source=self.requested_by,
        )

        if not result.get("success"):
            return BookingOutcome(success=False, error=result.get("error", "Booking failed."))

        appointment_id = result["appointment_id"]

        if self.confirm_immediately:
            approved = self.db.approve_appointment(appointment_id, actor=self.requested_by)
            if not approved:
                # The atomic check above already confirmed capacity/slot
                # validity a moment ago; a failure here means the hold
                # expired or was raced between the two calls. Treat as a
                # booking failure - do NOT report success with a
                # still-pending appointment, since the caller's contract
                # (Patient Portal semantics) is "confirmed or it did not
                # happen".
                self.db.cancel_appointment(
                    appointment_id, actor=self.requested_by,
                    reason="Failed to auto-confirm immediately after creation",
                )
                return BookingOutcome(
                    success=False,
                    error="Could not confirm the appointment; please try again.",
                )
            status_value = AppointmentStatus.CONFIRMED.value
        else:
            status_value = AppointmentStatus.PENDING.value

        return BookingOutcome(success=True, appointment_id=appointment_id, status=status_value)

    def block_date(self, block_date_value: date) -> None:
        """
        Block an entire calendar date, matching MockCalendarIntegration's
        `block_date(date)` method - called directly by
        utils.sample_data.initialize_sample_data at real application
        startup, so any CalendarIntegration implementation passed there
        must support it even though it is not part of the formal
        CalendarIntegration ABC.

        Delegates to the scheduling subsystem's existing persistent
        blocked-period store (SchedulingDatabase.add_blocked_period /
        CalendarService.is_slot_blocked) rather than keeping a second,
        in-memory blocked-date list - there remains exactly one source of
        truth for unavailability, the same `scheduling_db` every other
        method on this adapter (and the Staff Calendar) reads from.

        The legacy single-date operation is translated into the
        subsystem's (start_datetime, end_datetime) period representation
        by blocking the full calendar day, UTC-anchored (00:00:00Z to
        23:59:59Z) to match the format `web/calendar_routes.py`'s own
        blocked-period endpoint already uses.

        Args:
            block_date_value: The date to block, in full.
        """
        day_str = block_date_value.isoformat()
        self.db.add_blocked_period(
            start=f"{day_str}T00:00:00Z",
            end=f"{day_str}T23:59:59Z",
            reason="Blocked via CalendarIntegration.block_date",
            created_by=self.requested_by,
        )

    def cancel_appointment(self, patient_id: str, appointment_date: date) -> bool:
        """Cancel this patient's active (pending or confirmed) appointment
        on the given date, if any."""
        start = appointment_date.isoformat() + "T00:00:00"
        end = appointment_date.isoformat() + "T23:59:59"
        appointments = self.db.get_appointments_by_date_range(
            appointment_date.isoformat(), appointment_date.isoformat()
        )
        for appt in appointments:
            if appt["patient_id"] != patient_id:
                continue
            if appt["status"] not in (
                AppointmentStatus.PENDING.value,
                AppointmentStatus.CONFIRMED.value,
            ):
                continue
            return self.db.cancel_appointment(
                appt["id"], actor=self.requested_by, reason="Cancelled via CalendarIntegration"
            )
        return False

    # -- helpers -----------------------------------------------------------

    def get_appointments_for_patient(self, patient_id: str) -> list[tuple[date, str]]:
        """
        Convenience method mirroring MockCalendarIntegration's method of
        the same name, for callers/tests that want a quick "what does this
        patient have booked" view without querying SchedulingDatabase
        directly. Returns (date, status) tuples - the scheduling schema has
        no single "treatment_type" column, so status is returned in that
        position instead (kept as a 2-tuple for structural familiarity with
        the mock's return shape).
        """
        today = datetime.now(timezone.utc).date()
        far_future = today + timedelta(days=365)
        appointments = self.db.get_appointments_by_date_range(
            today.isoformat(), far_future.isoformat()
        )
        return [
            (date.fromisoformat(a["slot_date"]), a["status"])
            for a in appointments
            if a["patient_id"] == patient_id
            and a["status"] in (AppointmentStatus.PENDING.value, AppointmentStatus.CONFIRMED.value)
        ]

    def _earliest_available_slot(
        self, target_date: date, config: dict, tz_name: str
    ) -> Optional[dict]:
        """The earliest (session, time) slot on `target_date` that
        currently has capacity, or None if the date has none."""
        day_slots = self.calendar_service.generate_slots_for_date(target_date.isoformat())
        if not day_slots:
            return None

        appointments = self.db.get_appointments_by_date_range(
            target_date.isoformat(), target_date.isoformat()
        )
        booked_counts: dict[str, int] = {}
        for appt in appointments:
            if appt["status"] in (AppointmentStatus.PENDING.value, AppointmentStatus.CONFIRMED.value):
                key = appt["slot_datetime_utc"]
                booked_counts[key] = booked_counts.get(key, 0) + 1

        capacity = config.get("slots_per_session", 1)
        for slot in sorted(day_slots, key=lambda s: s["datetime_utc"]):
            booked = booked_counts.get(slot["datetime_utc"], 0)
            if booked < capacity:
                return slot
        return None
