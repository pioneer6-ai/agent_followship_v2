#!/usr/bin/env python3
"""
Deterministic NO_SHOW demo setup script.

Prepares ONE existing demo patient with a CONFIRMED appointment exactly
45 minutes before the current clinic time, in the REAL local
scheduling.db (the same file web/app.py opens) - so the manual demo flow
works end to end:

    setup script -> start Flask -> Run Daily Cycle
    -> Staff Outreach shows NO_SHOW + Patient Portal link
    -> click link -> Patient Portal -> rebook

This script does NOT modify Agent logic, and does NOT mark the
appointment NO_SHOW itself - that transition is only ever performed by
the real Agent (agent/orchestrator.py's SEND_REMINDER handling, via
SchedulingDatabaseCalendarAdapter.has_missed_appointment /
SchedulingDatabase.mark_appointment_no_show) when Run Daily Cycle is
clicked in the dashboard. This script only prepares the CONFIRMED
appointment the Agent is supposed to act on.

Uses only existing, already-audited SchedulingDatabase/CalendarService
APIs - no raw duplicate booking/validation logic:
    - SchedulingDatabase.get_config            (clinic timezone)
    - SchedulingDatabase.get_appointments_by_date_range (idempotency check)
    - SchedulingDatabase.cancel_appointment    (clears a stale demo slot
                                                 from a previous run - an
                                                 audited status change,
                                                 never a raw DELETE)
    - SchedulingDatabase.create_appointment_request + approve_appointment
                                                (the same two calls
                                                 check_capacity_and_book
                                                 makes internally)
    - SqlitePatientDataStore.get_patient_by_id / update_last_contacted

Idempotent: running this repeatedly before a demo cancels only THIS
demo patient's own previous demo appointment (if still CONFIRMED/PENDING)
and creates a fresh one 45 minutes in the past relative to "now" -
never touches any other patient's data, and never runs a DELETE/TRUNCATE
against any table.

Usage:
    python scripts/setup_no_show_demo.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Allow running from the project root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.data_access import SqlitePatientDataStore  # noqa: E402
from scheduling.database import AppointmentStatus, SchedulingDatabase  # noqa: E402

#: The demo patient this script prepares. Must already exist in the real
#: patients table (seeded by utils/sample_data.py) - this script never
#: creates a new patient record, it only ensures the existing one is in
#: a state the Agent will actually act on (not opted out).
DEMO_PATIENT_ID = "P001"

#: How long before "now" the demo appointment is scheduled - well past
#: the default 30-minute no_show_grace_period_minutes, so a single
#: Run Daily Cycle deterministically finds it overdue.
MINUTES_BEFORE_NOW = 45

#: Marker recorded in follow_up_reason so this script can find (and only
#: ever touch) appointments IT created on a later run, never a real
#: appointment for this patient that happens to fall on the same day.
DEMO_MARKER = "NO_SHOW_DEMO_SETUP_SCRIPT"


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _clinic_now(scheduling_db: SchedulingDatabase) -> datetime:
    """The current instant, expressed in the clinic's configured timezone -
    exactly what core.clock.SystemClock.now() would return in production,
    read from the SAME clinic_config table (never hardcoded)."""
    config = scheduling_db.get_config()
    tz_name = config.get("timezone_name", "America/New_York")
    return datetime.now(ZoneInfo(tz_name))


def _clear_previous_demo_appointment(scheduling_db: SchedulingDatabase, today: date) -> int:
    """
    Idempotency + cleanup: cancel (never delete) any appointment THIS
    script previously created for the demo patient that is still
    PENDING/CONFIRMED, so repeated runs before a demo do not pile up
    duplicate rows. Only rows for DEMO_PATIENT_ID carrying this script's
    own DEMO_MARKER are touched - every other appointment, for this
    patient or any other, is left exactly as-is. A row this script
    created on an earlier run that the real Agent already transitioned
    to NO_SHOW is also left alone: cancel_appointment only ever applies
    to PENDING/CONFIRMED rows, so a persisted NO_SHOW demo appointment
    from a previous run stays a harmless, inert audit record.

    Returns the number of appointments cancelled.
    """
    # A 2-day window comfortably covers "yesterday" (in case the previous
    # run happened right before local midnight) through "today".
    window_start = (today - timedelta(days=1)).isoformat()
    window_end = today.isoformat()
    candidates = scheduling_db.get_appointments_by_date_range(window_start, window_end)

    cancelled = 0
    for appt in candidates:
        if appt["patient_id"] != DEMO_PATIENT_ID:
            continue
        if appt.get("follow_up_reason") != DEMO_MARKER:
            continue
        if appt["status"] not in (AppointmentStatus.PENDING.value, AppointmentStatus.CONFIRMED.value):
            continue
        if scheduling_db.cancel_appointment(
            appt["id"], actor="setup_no_show_demo_script", reason="Superseded by a fresh demo run"
        ):
            cancelled += 1
    return cancelled


def setup_no_show_demo() -> None:
    scheduling_db = SchedulingDatabase(str(_project_root() / "scheduling.db"))
    data_store = SqlitePatientDataStore(scheduling_db)

    patient = data_store.get_patient_by_id(DEMO_PATIENT_ID)
    if patient is None:
        raise SystemExit(
            f"Demo patient {DEMO_PATIENT_ID} was not found in the real "
            f"scheduling.db patients table. Seed sample data first "
            f"(see utils/sample_data.py) before running this script."
        )
    if patient.opted_out:
        raise SystemExit(
            f"Demo patient {DEMO_PATIENT_ID} ({patient.name}) is opted out, "
            f"so the Agent will never contact them. Fix the patient record "
            f"before running this demo."
        )

    clinic_now = _clinic_now(scheduling_db)
    appointment_local = clinic_now - timedelta(minutes=MINUTES_BEFORE_NOW)
    appointment_utc = appointment_local.astimezone(timezone.utc)

    cancelled = _clear_previous_demo_appointment(scheduling_db, clinic_now.date())

    session = "morning" if appointment_local.hour < 12 else "afternoon"
    # Comfortably in the future relative to "now", so nothing expires this
    # PENDING row before approve_appointment runs a moment later.
    expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    appt_id = scheduling_db.create_appointment_request(
        patient_id=patient.patient_id,
        patient_name=patient.name,
        follow_up_case_id=None,
        slot_date=appointment_local.date().isoformat(),
        slot_session=session,
        slot_time=appointment_local.strftime("%H:%M"),
        slot_datetime_utc=appointment_utc.isoformat(),
        follow_up_reason=DEMO_MARKER,
        requested_by="setup_no_show_demo_script",
        expires_at=expires_at,
        source="staff",
    )
    approved = scheduling_db.approve_appointment(appt_id, actor="setup_no_show_demo_script")
    if not approved:
        raise SystemExit(f"Could not approve appointment {appt_id} to CONFIRMED.")

    appt = scheduling_db.get_appointment(appt_id)

    print("=" * 70)
    print("NO_SHOW demo setup complete")
    print("=" * 70)
    print(f"Patient ID:            {patient.patient_id}")
    print(f"Patient name:          {patient.name}")
    print(f"Appointment (clinic):  {appointment_local.isoformat()}")
    print(f"Appointment (UTC):     {appointment_utc.isoformat()}")
    print(f"Current clinic time:   {clinic_now.isoformat()}")
    print(f"Minutes in the past:   {MINUTES_BEFORE_NOW} (grace period is 30 by default)")
    print(f"Appointment DB status: {appt['status']} (id={appt_id})")
    if cancelled:
        print(f"Cleared {cancelled} stale demo appointment(s) from a previous run.")
    print()
    print("Expected transition on the NEXT 'Run Daily Cycle' in the dashboard:")
    print(f"  CONFIRMED -> NO_SHOW (performed by the real Agent, not this script)")
    print()
    print("Next steps:")
    print("  1. Start Flask:            python web/app.py")
    print("  2. In the dashboard, click Run Daily Cycle")
    print("  3. Open Staff Outreach:    it should show a NO_SHOW message")
    print("     for this patient with a clickable Patient Portal link")
    print("  4. Click the link, then rebook through the Patient Portal")
    print("=" * 70)


if __name__ == "__main__":
    setup_no_show_demo()
