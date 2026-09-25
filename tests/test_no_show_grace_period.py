"""
Boundary and persistence tests for the NO_SHOW grace-period rule.

Required rule: current clinic datetime >= appointment datetime +
no_show_grace_period_minutes (default 30). This is a DATETIME comparison
via Clock.now(), never date.today() or a date-only comparison - a
2:00 PM confirmed appointment is NOT a no-show at 2:29 PM and IS one at
exactly 2:30 PM.

Also covers: the NO_SHOW status is PERSISTED in scheduling.db (not
re-inferred forever from an overdue CONFIRMED row), the transition from
CONFIRMED happens exactly once per appointment, and COMPLETED/CANCELLED
appointments never transition to NO_SHOW regardless of how much time has
passed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scheduling.calendar_service import CalendarService
from scheduling.database import AppointmentStatus, SchedulingDatabase


@pytest.fixture
def scheduling_db(tmp_path):
    return SchedulingDatabase(str(tmp_path / "grace_period_test.db"))


@pytest.fixture
def calendar_service(scheduling_db):
    return CalendarService(scheduling_db)


def _book_confirmed_appointment_at(calendar_service, scheduling_db, appointment_dt_utc):
    """Book and confirm a real appointment at an exact UTC datetime,
    bypassing slot generation so the test controls the appointment time
    precisely (needed for minute-level boundary assertions)."""
    result = calendar_service.check_capacity_and_book(
        patient_id="P1",
        patient_name="Sarah",
        slot_datetime_utc=appointment_dt_utc.isoformat(),
        slot_date=appointment_dt_utc.date().isoformat(),
        slot_session="afternoon",
        slot_time=appointment_dt_utc.strftime("%H:%M"),
        requested_by="staff",
        source="staff",
    )
    # check_capacity_and_book re-normalizes slot_datetime_utc from
    # slot_date/slot_time using clinic config's timezone, which would
    # silently shift our exact test datetime - so we skip that path and
    # insert directly for these boundary tests, then approve normally.
    conn = scheduling_db.get_connection()
    try:
        conn.execute("DELETE FROM appointment_requests WHERE id = ?", (result["appointment_id"],))
        conn.commit()
    finally:
        conn.close()

    now_str = datetime.now(timezone.utc).isoformat()
    conn = scheduling_db.get_connection()
    try:
        cursor = conn.execute('''
            INSERT INTO appointment_requests
            (patient_id, patient_name, follow_up_case_id, slot_date, slot_session,
             slot_time, slot_datetime_utc, status, requested_by, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            "P1", "Sarah", None,
            appointment_dt_utc.date().isoformat(), "afternoon",
            appointment_dt_utc.strftime("%H:%M"), appointment_dt_utc.isoformat(),
            AppointmentStatus.CONFIRMED.value, "staff", "staff", now_str, now_str,
        ))
        appt_id = cursor.lastrowid
        conn.commit()
        return appt_id
    finally:
        conn.close()


APPOINTMENT_TIME = datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc)  # Sep 25, 2:00 PM


# ---------------------------------------------------------------------------
# Boundary: 29 minutes / exactly 30 minutes / 31 minutes.
# ---------------------------------------------------------------------------

class TestGracePeriodBoundary:
    def test_29_minutes_after_is_not_yet_a_no_show(self, scheduling_db, calendar_service):
        appt_id = _book_confirmed_appointment_at(calendar_service, scheduling_db, APPOINTMENT_TIME)
        now = APPOINTMENT_TIME + timedelta(minutes=29)

        row = scheduling_db.get_overdue_confirmed_appointment_for_patient(
            "P1", now.isoformat(), grace_period_minutes=30
        )
        assert row is None

        appt = scheduling_db.get_appointment(appt_id)
        assert appt["status"] == AppointmentStatus.CONFIRMED.value

    def test_exactly_30_minutes_after_is_a_no_show(self, scheduling_db, calendar_service):
        appt_id = _book_confirmed_appointment_at(calendar_service, scheduling_db, APPOINTMENT_TIME)
        now = APPOINTMENT_TIME + timedelta(minutes=30)

        row = scheduling_db.get_overdue_confirmed_appointment_for_patient(
            "P1", now.isoformat(), grace_period_minutes=30
        )
        assert row is not None
        assert row["id"] == appt_id

    def test_31_minutes_after_is_a_no_show(self, scheduling_db, calendar_service):
        appt_id = _book_confirmed_appointment_at(calendar_service, scheduling_db, APPOINTMENT_TIME)
        now = APPOINTMENT_TIME + timedelta(minutes=31)

        row = scheduling_db.get_overdue_confirmed_appointment_for_patient(
            "P1", now.isoformat(), grace_period_minutes=30
        )
        assert row is not None
        assert row["id"] == appt_id

    def test_29_minutes_after_via_the_calendar_adapter_is_not_a_no_show(
        self, scheduling_db, calendar_service
    ):
        """Same boundary, exercised through the adapter method the
        orchestrator actually calls."""
        from core.scheduling_calendar_adapter import SchedulingDatabaseCalendarAdapter

        calendar = SchedulingDatabaseCalendarAdapter(scheduling_db, requested_by="patient_portal")
        _book_confirmed_appointment_at(calendar_service, scheduling_db, APPOINTMENT_TIME)
        now = APPOINTMENT_TIME + timedelta(minutes=29)

        assert calendar.has_missed_appointment("P1", now, grace_period_minutes=30) is False

    def test_30_minutes_after_via_the_calendar_adapter_is_a_no_show(
        self, scheduling_db, calendar_service
    ):
        from core.scheduling_calendar_adapter import SchedulingDatabaseCalendarAdapter

        calendar = SchedulingDatabaseCalendarAdapter(scheduling_db, requested_by="patient_portal")
        appt_id = _book_confirmed_appointment_at(calendar_service, scheduling_db, APPOINTMENT_TIME)
        now = APPOINTMENT_TIME + timedelta(minutes=30)

        assert calendar.has_missed_appointment("P1", now, grace_period_minutes=30) is True

        # And the transition was actually PERSISTED, not just inferred.
        appt = scheduling_db.get_appointment(appt_id)
        assert appt["status"] == AppointmentStatus.NO_SHOW.value


# ---------------------------------------------------------------------------
# Persistence: status is written to scheduling.db, not re-derived forever.
# ---------------------------------------------------------------------------

class TestNoShowStatusIsPersisted:
    def test_mark_appointment_no_show_persists_the_status(self, scheduling_db, calendar_service):
        appt_id = _book_confirmed_appointment_at(calendar_service, scheduling_db, APPOINTMENT_TIME)

        transitioned = scheduling_db.mark_appointment_no_show(appt_id, actor="system")
        assert transitioned is True

        appt = scheduling_db.get_appointment(appt_id)
        assert appt["status"] == AppointmentStatus.NO_SHOW.value

    def test_get_no_show_appointment_for_patient_finds_the_persisted_row(
        self, scheduling_db, calendar_service
    ):
        appt_id = _book_confirmed_appointment_at(calendar_service, scheduling_db, APPOINTMENT_TIME)
        scheduling_db.mark_appointment_no_show(appt_id, actor="system")

        row = scheduling_db.get_no_show_appointment_for_patient("P1")
        assert row is not None
        assert row["id"] == appt_id


# ---------------------------------------------------------------------------
# Transition happens exactly once; no duplicate no-show triggers.
# ---------------------------------------------------------------------------

class TestTransitionHappensExactlyOnce:
    def test_marking_an_already_no_show_appointment_again_is_a_no_op(
        self, scheduling_db, calendar_service
    ):
        appt_id = _book_confirmed_appointment_at(calendar_service, scheduling_db, APPOINTMENT_TIME)

        first = scheduling_db.mark_appointment_no_show(appt_id, actor="system")
        second = scheduling_db.mark_appointment_no_show(appt_id, actor="system")

        assert first is True
        assert second is False  # already NO_SHOW - no duplicate transition

    def test_repeated_detection_calls_do_not_re_write_the_appointment(
        self, scheduling_db, calendar_service
    ):
        """Calling has_missed_appointment multiple times (as would happen
        across repeated daily-cycle runs) must transition the appointment
        ONCE, not repeatedly - the same appointment must not keep
        triggering a fresh no-show transition/follow-up."""
        from core.scheduling_calendar_adapter import SchedulingDatabaseCalendarAdapter

        calendar = SchedulingDatabaseCalendarAdapter(scheduling_db, requested_by="patient_portal")
        appt_id = _book_confirmed_appointment_at(calendar_service, scheduling_db, APPOINTMENT_TIME)
        now = APPOINTMENT_TIME + timedelta(hours=2)

        first_call = calendar.has_missed_appointment("P1", now, grace_period_minutes=30)
        appt_after_first = scheduling_db.get_appointment(appt_id)

        second_call = calendar.has_missed_appointment(
            "P1", now + timedelta(days=1), grace_period_minutes=30
        )
        appt_after_second = scheduling_db.get_appointment(appt_id)

        third_call = calendar.has_missed_appointment(
            "P1", now + timedelta(days=2), grace_period_minutes=30
        )
        appt_after_third = scheduling_db.get_appointment(appt_id)

        # Still correctly reported as a no-show every time...
        assert first_call is True
        assert second_call is True
        assert third_call is True

        # ...but the underlying row transitioned exactly once: same
        # status (NO_SHOW) and, critically, the same updated_at from the
        # first transition onward - proving no repeated DB write.
        assert appt_after_first["status"] == AppointmentStatus.NO_SHOW.value
        assert appt_after_second["updated_at"] == appt_after_first["updated_at"]
        assert appt_after_third["updated_at"] == appt_after_first["updated_at"]

    def test_two_separate_missed_appointments_for_the_same_patient_each_transition_once(
        self, scheduling_db, calendar_service
    ):
        """Sanity check that the one-time guard is per-appointment (by
        id), not accidentally a per-patient global flag that would
        silently swallow a second, later no-show."""
        from core.scheduling_calendar_adapter import SchedulingDatabaseCalendarAdapter

        calendar = SchedulingDatabaseCalendarAdapter(scheduling_db, requested_by="patient_portal")
        first_appt_time = APPOINTMENT_TIME
        second_appt_time = APPOINTMENT_TIME + timedelta(days=10)

        first_appt_id = _book_confirmed_appointment_at(calendar_service, scheduling_db, first_appt_time)
        calendar.has_missed_appointment(
            "P1", first_appt_time + timedelta(hours=1), grace_period_minutes=30
        )
        assert scheduling_db.get_appointment(first_appt_id)["status"] == AppointmentStatus.NO_SHOW.value

        second_appt_id = _book_confirmed_appointment_at(calendar_service, scheduling_db, second_appt_time)
        calendar.has_missed_appointment(
            "P1", second_appt_time + timedelta(hours=1), grace_period_minutes=30
        )
        assert scheduling_db.get_appointment(second_appt_id)["status"] == AppointmentStatus.NO_SHOW.value


# ---------------------------------------------------------------------------
# COMPLETED/CANCELLED (and other non-CONFIRMED) states never transition.
# ---------------------------------------------------------------------------

class TestAttendedOrClosedAppointmentsNeverTransitionToNoShow:
    def test_a_completed_appointment_is_never_marked_no_show(self, scheduling_db, calendar_service):
        appt_id = _book_confirmed_appointment_at(calendar_service, scheduling_db, APPOINTMENT_TIME)
        scheduling_db.complete_appointment(appt_id, actor="staff")

        now = APPOINTMENT_TIME + timedelta(hours=5)
        row = scheduling_db.get_overdue_confirmed_appointment_for_patient(
            "P1", now.isoformat(), grace_period_minutes=30
        )
        assert row is None

        transitioned = scheduling_db.mark_appointment_no_show(appt_id, actor="system")
        assert transitioned is False
        assert scheduling_db.get_appointment(appt_id)["status"] == AppointmentStatus.COMPLETED.value

    def test_a_cancelled_appointment_is_never_marked_no_show(self, scheduling_db, calendar_service):
        appt_id = _book_confirmed_appointment_at(calendar_service, scheduling_db, APPOINTMENT_TIME)
        scheduling_db.cancel_appointment(appt_id, actor="staff", reason="Patient called ahead")

        now = APPOINTMENT_TIME + timedelta(hours=5)
        row = scheduling_db.get_overdue_confirmed_appointment_for_patient(
            "P1", now.isoformat(), grace_period_minutes=30
        )
        assert row is None

        transitioned = scheduling_db.mark_appointment_no_show(appt_id, actor="system")
        assert transitioned is False
        assert scheduling_db.get_appointment(appt_id)["status"] == AppointmentStatus.CANCELLED.value

    def test_has_missed_appointment_is_false_for_a_completed_appointment(
        self, scheduling_db, calendar_service
    ):
        from core.scheduling_calendar_adapter import SchedulingDatabaseCalendarAdapter

        calendar = SchedulingDatabaseCalendarAdapter(scheduling_db, requested_by="patient_portal")
        appt_id = _book_confirmed_appointment_at(calendar_service, scheduling_db, APPOINTMENT_TIME)
        scheduling_db.complete_appointment(appt_id, actor="staff")

        now = APPOINTMENT_TIME + timedelta(hours=5)
        assert calendar.has_missed_appointment("P1", now, grace_period_minutes=30) is False


# ---------------------------------------------------------------------------
# Default grace period value (ClinicPolicyConfig).
# ---------------------------------------------------------------------------

class TestDefaultGracePeriodConfig:
    def test_default_is_30_minutes(self):
        from core.config import ClinicPolicyConfig

        assert ClinicPolicyConfig().no_show_grace_period_minutes == 30

    def test_env_override_is_respected(self):
        from core.config import ClinicPolicyConfig

        policy = ClinicPolicyConfig.from_env({"AGENT_NO_SHOW_GRACE_PERIOD_MINUTES": "45"})
        assert policy.no_show_grace_period_minutes == 45


# ---------------------------------------------------------------------------
# Exactly-once guarantee for the OUTBOUND follow-up itself (not just the
# DB status transition). A second `run_daily_cycle` for the same
# already-NO_SHOW appointment, before staff has confirmed the first
# queued send, must not enqueue a second no-show follow-up.
# ---------------------------------------------------------------------------

class TestRepeatedDailyCycleDoesNotDuplicateTheOutboundNoShowFollowUp:
    def test_running_the_daily_cycle_twice_queues_exactly_one_no_show_follow_up(
        self, scheduling_db, calendar_service
    ):
        import contextlib
        import io
        from datetime import date

        from agent.orchestrator import FollowUpAgentOrchestrator
        from core.clock import FixedClock
        from core.config import ClinicPolicyConfig
        from core.data_access import MockPatientDataStore
        from core.models import ContactChannel, PatientRecord
        from core.scheduling_calendar_adapter import SchedulingDatabaseCalendarAdapter

        # Seed a CONFIRMED appointment 2 hours in the past (well past the
        # default 30-minute grace period), and a patient record whose
        # last visit is old enough to be independently overdue too, so
        # the case is actionable regardless of which signal drives it.
        _book_confirmed_appointment_at(calendar_service, scheduling_db, APPOINTMENT_TIME)

        now = APPOINTMENT_TIME + timedelta(hours=2)
        clock = FixedClock(now)

        calendar = SchedulingDatabaseCalendarAdapter(scheduling_db, requested_by="patient_portal")
        data_store = MockPatientDataStore()
        data_store.add_patient(
            PatientRecord(
                patient_id="P1",
                name="Sarah",
                contact_info={
                    ContactChannel.SMS: "+15550000000",
                    ContactChannel.EMAIL: "p1@example.com",
                },
                preferred_channel=ContactChannel.EMAIL,
                last_visit_date=now.date() - timedelta(days=60),
                treatment_type="cleaning",
                recall_interval_days=30,
                no_show_history=0,
            )
        )

        orchestrator = FollowUpAgentOrchestrator.with_llm_decisions(
            data_store, calendar, ClinicPolicyConfig(), clock=clock
        )

        with contextlib.redirect_stdout(io.StringIO()):
            orchestrator.run_daily_cycle(clock.today())

        first_pending = orchestrator.get_pending_sends()
        assert len(first_pending) == 1
        assert first_pending[0]["patient_id"] == "P1"

        # The DB transition already happened during the first cycle,
        # so the appointment is now a persisted NO_SHOW row - not just
        # an overdue CONFIRMED one. Staff has NOT confirmed the queued
        # send yet (case.status is therefore still whatever it was
        # before confirm_pending_sends runs), so a second cycle call
        # exercises exactly the repeated-trigger scenario in question.
        with contextlib.redirect_stdout(io.StringIO()):
            orchestrator.run_daily_cycle(clock.today())

        second_pending = orchestrator.get_pending_sends()

        assert len(second_pending) == 1, (
            f"expected the no-show follow-up to be queued exactly once across "
            f"two daily-cycle runs, but got {len(second_pending)} pending sends: "
            f"{second_pending}"
        )
        assert second_pending[0]["send_id"] == first_pending[0]["send_id"]
