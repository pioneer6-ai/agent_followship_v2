"""
Integration tests for calendar unification: SchedulingDatabaseCalendarAdapter
makes SchedulingDatabase the single production source of truth for
appointments, shared by the Patient Portal (via AppointmentScheduler /
FollowUpAgentOrchestrator) and the Staff Calendar (via CalendarService /
web/calendar_routes.py) - with no second calendar store and no
synchronization step.

These tests use isolated temp SQLite files (never the repo's own
scheduling.db/auth.db), and drive the SAME instances through both the
agent's CalendarIntegration interface and CalendarService directly, exactly
mirroring how the Patient Portal and Staff Calendar reach the database in
the real running app (see web/app.py).
"""

from __future__ import annotations

import contextlib
import io
import os
from datetime import date, timedelta

import pytest

from agent.orchestrator import FollowUpAgentOrchestrator
from core.clock import FixedClock
from core.config import ClinicPolicyConfig
from core.data_access import MockCalendarIntegration, MockPatientDataStore
from core.models import CaseStatus, ContactChannel, PatientRecord
from core.scheduling_calendar_adapter import SchedulingDatabaseCalendarAdapter
from scheduling.calendar_service import CalendarService
from scheduling.database import AppointmentStatus, SchedulingDatabase
from utils.sample_data import initialize_sample_data
from datetime import datetime
from zoneinfo import ZoneInfo


@pytest.fixture
def scheduling_db(tmp_path):
    db_path = str(tmp_path / "scheduling_unify_test.db")
    return SchedulingDatabase(db_path)


@pytest.fixture
def calendar_service(scheduling_db):
    return CalendarService(scheduling_db)


@pytest.fixture
def calendar(scheduling_db):
    return SchedulingDatabaseCalendarAdapter(scheduling_db, requested_by="patient_portal")


@pytest.fixture
def agent_env(scheduling_db, calendar):
    data_store = MockPatientDataStore()
    policy = ClinicPolicyConfig()
    orchestrator = FollowUpAgentOrchestrator.with_llm_decisions(data_store, calendar, policy)
    return orchestrator, data_store


def _first_weekday_slot(calendar_service, starting_from):
    """Find the first date (searching forward from `starting_from`) that
    has at least one generated slot, and return that slot dict. Avoids
    hardcoding a specific day offset that might land on a
    non-working-day (weekend) given the clinic's configured working_days."""
    probe = starting_from
    for _ in range(14):
        slots = calendar_service.generate_slots_for_date(probe.isoformat())
        if slots:
            return slots[0]
        probe += timedelta(days=1)
    raise AssertionError("no working day with slots found within 14 days")


def make_patient(patient_id="UNI1", name="Unification Patient", days_since_visit=60):
    return PatientRecord(
        patient_id=patient_id,
        name=name,
        contact_info={ContactChannel.SMS: "+15550000000"},
        preferred_channel=ContactChannel.SMS,
        last_visit_date=date.today() - timedelta(days=days_since_visit),
        treatment_type="cleaning",
        recall_interval_days=30,
    )


def seed_case(orchestrator, data_store, patient_id="UNI1", name="Unification Patient"):
    data_store.add_patient(make_patient(patient_id, name))
    with contextlib.redirect_stdout(io.StringIO()):
        orchestrator.run_daily_cycle(date.today())
    return orchestrator.get_case_by_patient_id(patient_id)


class TestProductionStartupPath:
    """
    Regression coverage for a real startup crash: `web/app.py` ->
    utils.sample_data.initialize_sample_data(data_store, calendar) calls
    `calendar.block_date(...)` directly. block_date is not part of the
    formal CalendarIntegration ABC, so it was missed when
    SchedulingDatabaseCalendarAdapter was first written, and the adapter
    raised AttributeError the moment the real app started up with it
    wired in as `calendar` (exactly as web/app.py does in production).

    These tests exercise the actual startup call, not just the adapter's
    individual methods in isolation, so a future regression here would be
    caught the same way this one was found: by initialization actually
    failing.
    """

    def test_initialize_sample_data_does_not_raise_with_the_adapter(self, scheduling_db, calendar):
        data_store = MockPatientDataStore()
        # This is the exact call chain from web/app.py's __main__ block:
        # initialize_sample_data(data_store, calendar) -> calendar.block_date(...)
        initialize_sample_data(data_store, calendar)

        # Sanity: patients were actually added (the function got past the
        # block_date calls, not merely swallowed an exception).
        assert len(data_store.get_all_active_patients()) > 0

    def test_initialize_sample_data_persists_blocked_periods_in_scheduling_db(
        self, scheduling_db, calendar
    ):
        data_store = MockPatientDataStore()
        initialize_sample_data(data_store, calendar)

        conn = scheduling_db.get_connection()
        try:
            count = conn.execute("SELECT COUNT(*) as c FROM blocked_periods").fetchone()["c"]
        finally:
            conn.close()
        # initialize_sample_data blocks every Sunday over the next 60 days -
        # roughly 8-9 rows. The exact count depends on today's weekday, so
        # assert loosely on "some rows were persisted", not an exact number.
        assert count > 0

    def test_a_blocked_date_is_actually_unavailable_through_the_unified_subsystem(
        self, scheduling_db, calendar
    ):
        """Not just that block_date runs without raising, but that it has
        the intended effect: the scheduling subsystem (the single source
        of truth) actually excludes that date from availability."""
        today = date.today()
        # Find a normally-available weekday first, to isolate "blocking
        # worked" from "it was never available anyway" (e.g. a weekend).
        available_before = calendar.find_available_slots(
            "cleaning", after=today, limit=5, to_date=today + timedelta(days=14)
        )
        assert available_before, "expected at least one available weekday slot"
        target = available_before[0]

        calendar.block_date(target)

        available_after = calendar.find_available_slots(
            "cleaning", after=today, limit=10, to_date=today + timedelta(days=14)
        )
        assert target not in available_after

    def test_block_date_uses_the_persistent_blocked_periods_table_not_memory(
        self, scheduling_db, calendar
    ):
        """The fix must not introduce a second, in-memory blocked-date
        list - confirm the block is visible by reopening the database
        fresh, the same way TestPersistenceAcrossRestart does for
        appointments."""
        today = date.today() + timedelta(days=3)
        calendar.block_date(today)

        db_path = scheduling_db.db_path
        reopened = SchedulingDatabase(db_path)
        blocked = reopened.get_blocked_periods(
            today.isoformat() + "T00:00:00Z", today.isoformat() + "T23:59:59Z"
        )
        assert len(blocked) >= 1


class TestAvailabilityComesFromSchedulingSubsystem:
    def test_find_available_slots_delegates_to_calendar_service(self, agent_env, calendar_service):
        orchestrator, data_store = agent_env
        case = seed_case(orchestrator, data_store)
        today = orchestrator.clock.today()

        slots = orchestrator.scheduler.find_available_slots(
            case, after=today, limit=10, to_date=today + timedelta(days=7)
        )
        assert len(slots) > 0

        # Cross-check independently against CalendarService's own view -
        # if the adapter were reading from anywhere else, these would not
        # agree.
        availability = calendar_service.get_availability(
            today.isoformat(), (today + timedelta(days=7)).isoformat()
        )
        available_dates = {
            date.fromisoformat(s["date"]) for s in availability if s["status"] == "available"
        }
        assert set(slots).issubset(available_dates)

    def test_no_slots_returned_beyond_a_fully_blocked_window(self, agent_env, scheduling_db):
        orchestrator, data_store = agent_env
        case = seed_case(orchestrator, data_store)
        today = orchestrator.clock.today()

        scheduling_db.add_blocked_period(
            start=today.isoformat() + "T00:00:00Z",
            end=(today + timedelta(days=14)).isoformat() + "T23:59:59Z",
            reason="test block",
            created_by="test",
        )
        slots = orchestrator.scheduler.find_available_slots(
            case, after=today, limit=10, to_date=today + timedelta(days=7)
        )
        assert slots == []


class TestPortalBookingCreatesExactlyOnePersistentAppointment:
    def test_booking_creates_one_confirmed_row(self, agent_env, scheduling_db):
        orchestrator, data_store = agent_env
        case = seed_case(orchestrator, data_store)
        today = orchestrator.clock.today()
        slots = orchestrator.scheduler.find_available_slots(
            case, after=today, limit=5, to_date=today + timedelta(days=7)
        )
        chosen = slots[0]

        decision = orchestrator.handle_portal_slot_selection("UNI1", chosen, today=today)
        assert decision.action.value == "confirm_booking"

        conn = scheduling_db.get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM appointment_requests WHERE patient_id = ?", ("UNI1",)
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1
        assert rows[0]["status"] == AppointmentStatus.CONFIRMED.value
        assert rows[0]["source"] == "patient_portal"

    def test_case_becomes_booked_only_after_confirmed_db_result(self, agent_env):
        orchestrator, data_store = agent_env
        case = seed_case(orchestrator, data_store)
        today = orchestrator.clock.today()
        slots = orchestrator.scheduler.find_available_slots(
            case, after=today, limit=5, to_date=today + timedelta(days=7)
        )

        orchestrator.handle_portal_slot_selection("UNI1", slots[0], today=today)
        case = orchestrator.get_case_by_patient_id("UNI1")
        assert case.status == CaseStatus.BOOKED


class TestStaffCalendarSeesTheSameAppointment:
    def test_staff_calendar_availability_reflects_portal_booking(
        self, agent_env, calendar_service, scheduling_db
    ):
        orchestrator, data_store = agent_env
        case = seed_case(orchestrator, data_store)
        today = orchestrator.clock.today()
        slots = orchestrator.scheduler.find_available_slots(
            case, after=today, limit=5, to_date=today + timedelta(days=7)
        )
        chosen = slots[0]

        orchestrator.handle_portal_slot_selection("UNI1", chosen, today=today)

        # The Staff Calendar's own availability view (exactly what
        # web/calendar_routes.py's /api/calendar/availability returns)
        # must show reduced capacity on that date - not a copy, the same
        # underlying rows.
        availability = calendar_service.get_availability(
            today.isoformat(), (today + timedelta(days=7)).isoformat()
        )
        matching = [s for s in availability if s["date"] == chosen.isoformat()]
        assert any(s["booked"] >= 1 for s in matching)

    def test_staff_calendar_pending_appointments_list_excludes_confirmed_portal_booking(
        self, agent_env, scheduling_db
    ):
        """A confirmed Patient Portal booking should not show up as
        something staff still needs to approve."""
        orchestrator, data_store = agent_env
        case = seed_case(orchestrator, data_store)
        today = orchestrator.clock.today()
        slots = orchestrator.scheduler.find_available_slots(
            case, after=today, limit=5, to_date=today + timedelta(days=7)
        )
        orchestrator.handle_portal_slot_selection("UNI1", slots[0], today=today)

        pending = scheduling_db.get_pending_requests()
        assert all(p["patient_id"] != "UNI1" for p in pending)


class TestPersistenceAcrossRestart:
    def test_appointment_survives_reopening_the_database(self, scheduling_db, agent_env):
        orchestrator, data_store = agent_env
        case = seed_case(orchestrator, data_store)
        today = orchestrator.clock.today()
        slots = orchestrator.scheduler.find_available_slots(
            case, after=today, limit=5, to_date=today + timedelta(days=7)
        )
        orchestrator.handle_portal_slot_selection("UNI1", slots[0], today=today)

        db_path = scheduling_db.db_path

        # Simulate an app restart: drop every Python reference and reopen
        # a fresh SchedulingDatabase against the same file.
        del orchestrator, data_store, case
        reopened = SchedulingDatabase(db_path)
        conn = reopened.get_connection()
        try:
            rows = conn.execute(
                "SELECT status, source FROM appointment_requests WHERE patient_id = ?",
                ("UNI1",),
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1
        assert rows[0]["status"] == AppointmentStatus.CONFIRMED.value
        assert rows[0]["source"] == "patient_portal"


class TestCapacityProtectionAcrossPortalAndStaffCalendar:
    def test_staff_fill_then_portal_booking_moves_to_next_available_slot(
        self, agent_env, calendar_service, scheduling_db
    ):
        """When the earliest session/time on a date is filled by staff
        bookings, a Patient Portal booking on that same date must not
        exceed capacity - it must land on a different slot with room, not
        overbook the full one."""
        orchestrator, data_store = agent_env
        case = seed_case(orchestrator, data_store)
        today = orchestrator.clock.today()
        slots = orchestrator.scheduler.find_available_slots(
            case, after=today, limit=5, to_date=today + timedelta(days=7)
        )
        target_date = slots[0]

        config = scheduling_db.get_config()
        capacity = config["slots_per_session"]
        first_slot = calendar_service.generate_slots_for_date(target_date.isoformat())[0]

        for i in range(capacity):
            result = calendar_service.check_capacity_and_book(
                patient_id=f"STAFF-FILL-{i}",
                patient_name="Staff Filled",
                slot_datetime_utc=first_slot["datetime_utc"],
                slot_date=first_slot["date"],
                slot_session=first_slot["session"],
                slot_time=first_slot["time"],
                requested_by="staff",
                source="staff",
            )
            assert result["success"]

        outcome = orchestrator.calendar.book_appointment_detailed(
            "UNI1", target_date, "cleaning", patient_name=case.patient.name
        )

        conn = scheduling_db.get_connection()
        try:
            count_on_full_slot = conn.execute(
                "SELECT COUNT(*) as c FROM appointment_requests "
                "WHERE slot_datetime_utc = ? AND status IN ('pending', 'confirmed')",
                (first_slot["datetime_utc"],),
            ).fetchone()["c"]
        finally:
            conn.close()

        assert count_on_full_slot == capacity, "the full slot must never exceed capacity"
        if outcome.success:
            # Booked a different (session, time) on the same date, not the full one.
            conn = scheduling_db.get_connection()
            try:
                row = conn.execute(
                    "SELECT slot_datetime_utc FROM appointment_requests WHERE patient_id = ?",
                    ("UNI1",),
                ).fetchone()
            finally:
                conn.close()
            assert row["slot_datetime_utc"] != first_slot["datetime_utc"]

    def test_exhausting_every_slot_on_a_date_causes_portal_booking_to_fail_cleanly(
        self, agent_env, calendar_service, scheduling_db
    ):
        orchestrator, data_store = agent_env
        case = seed_case(orchestrator, data_store)
        today = orchestrator.clock.today()
        slots = orchestrator.scheduler.find_available_slots(
            case, after=today, limit=5, to_date=today + timedelta(days=7)
        )
        target_date = slots[0]

        config = scheduling_db.get_config()
        capacity = config["slots_per_session"]
        for slot in calendar_service.generate_slots_for_date(target_date.isoformat()):
            for i in range(capacity):
                calendar_service.check_capacity_and_book(
                    patient_id=f"FILL-{slot['time']}-{i}",
                    patient_name="Filler",
                    slot_datetime_utc=slot["datetime_utc"],
                    slot_date=slot["date"],
                    slot_session=slot["session"],
                    slot_time=slot["time"],
                    requested_by="staff",
                    source="staff",
                )

        outcome = orchestrator.calendar.book_appointment_detailed(
            "UNI1", target_date, "cleaning", patient_name=case.patient.name
        )
        assert outcome.success is False
        assert outcome.error


class TestFailedBookingNeverBooksTheCase:
    def test_orchestrator_leaves_case_unbooked_on_db_failure(self, agent_env, calendar_service, scheduling_db):
        orchestrator, data_store = agent_env
        case = seed_case(orchestrator, data_store)
        today = orchestrator.clock.today()
        slots = orchestrator.scheduler.find_available_slots(
            case, after=today, limit=5, to_date=today + timedelta(days=7)
        )
        target_date = slots[0]

        # Exhaust every slot on the date so the DB-level booking must fail.
        config = scheduling_db.get_config()
        capacity = config["slots_per_session"]
        for slot in calendar_service.generate_slots_for_date(target_date.isoformat()):
            for i in range(capacity):
                calendar_service.check_capacity_and_book(
                    patient_id=f"FILL2-{slot['time']}-{i}",
                    patient_name="Filler",
                    slot_datetime_utc=slot["datetime_utc"],
                    slot_date=slot["date"],
                    slot_session=slot["session"],
                    slot_time=slot["time"],
                    requested_by="staff",
                    source="staff",
                )

        decision = orchestrator.handle_portal_slot_selection("UNI1", target_date, today=today)
        case = orchestrator.get_case_by_patient_id("UNI1")
        assert case.status != CaseStatus.BOOKED
        assert decision.action.value == "confirm_booking"  # PolicyGuard allowed it; the DB rejected it


class TestStaffPendingApprovalWorkflowUnaffected:
    def test_staff_created_appointment_still_requires_approval(self, calendar_service, scheduling_db):
        slot = _first_weekday_slot(calendar_service, date.today() + timedelta(days=1))

        result = calendar_service.check_capacity_and_book(
            patient_id="STAFF-CREATED",
            patient_name="Staff Created Patient",
            slot_datetime_utc=slot["datetime_utc"],
            slot_date=slot["date"],
            slot_session=slot["session"],
            slot_time=slot["time"],
            requested_by="staff_member",
            source="staff",
        )
        assert result["success"]

        appt = scheduling_db.get_appointment(result["appointment_id"])
        assert appt["status"] == AppointmentStatus.PENDING.value
        assert appt["source"] == "staff"

        approved = scheduling_db.approve_appointment(result["appointment_id"], actor="staff_member")
        assert approved
        appt_after = scheduling_db.get_appointment(result["appointment_id"])
        assert appt_after["status"] == AppointmentStatus.CONFIRMED.value

    def test_staff_can_decline_a_pending_request(self, calendar_service, scheduling_db):
        slot = _first_weekday_slot(calendar_service, date.today() + timedelta(days=1))
        result = calendar_service.check_capacity_and_book(
            patient_id="STAFF-CREATED-2",
            patient_name="Staff Created Patient 2",
            slot_datetime_utc=slot["datetime_utc"],
            slot_date=slot["date"],
            slot_session=slot["session"],
            slot_time=slot["time"],
            requested_by="staff_member",
            source="staff",
        )
        declined = scheduling_db.decline_appointment(
            result["appointment_id"], actor="staff_member", reason="Patient unreachable"
        )
        assert declined
        appt = scheduling_db.get_appointment(result["appointment_id"])
        assert appt["status"] == AppointmentStatus.DECLINED.value


class TestMockCalendarIntegrationStillAvailableForTests:
    """
    MockCalendarIntegration is kept for tests that explicitly want an
    isolated in-memory fake - it is simply not what the production app
    wires up any more (see web/app.py).
    """

    def test_mock_calendar_still_works_standalone(self):
        mock = MockCalendarIntegration()
        today = date.today()
        slots = mock.find_available_slots("cleaning", after=today, limit=3)
        assert len(slots) == 3

        success = mock.book_appointment("MOCK-PATIENT", slots[0], "cleaning")
        assert success
        assert mock.get_appointments_for_patient("MOCK-PATIENT") == [(slots[0], "cleaning")]

    def test_orchestrator_still_works_with_mock_calendar(self):
        data_store = MockPatientDataStore()
        calendar = MockCalendarIntegration()
        policy = ClinicPolicyConfig()
        orchestrator = FollowUpAgentOrchestrator(data_store, calendar, policy)

        data_store.add_patient(make_patient("MOCK1", "Mock Test Patient"))
        with contextlib.redirect_stdout(io.StringIO()):
            orchestrator.run_daily_cycle(date.today())

        case = orchestrator.get_case_by_patient_id("MOCK1")
        today = orchestrator.clock.today()
        slots = orchestrator.scheduler.find_available_slots(case, after=today, limit=3)
        decision = orchestrator.handle_portal_slot_selection("MOCK1", slots[0], today=today)
        case = orchestrator.get_case_by_patient_id("MOCK1")
        assert case.status == CaseStatus.BOOKED
