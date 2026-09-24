"""
Regression tests for two Patient Portal bugs found during manual testing
after Calendar Unification:

  Issue 1: the portal only let a patient pick a DATE, never a specific
  SESSION/TIME - `find_available_slots` deduped to one bookable unit per
  date, and booking always silently grabbed the earliest slot on that
  date. Fixed by adding `SchedulingDatabaseCalendarAdapter.
  find_available_slot_options`/`book_specific_slot`, a `dates`/
  `preferred_dates`/`other_dates` shape on the `/available-slots` route,
  and a `session`/`time` payload on `/select-slot` that is independently
  re-validated server-side (see `_validate_selected_slot_option`).

  Issue 2: no-show/rescheduling context did not exist anywhere in the data
  model - `PatientRecord.no_show_history` is a bare integer counter, never
  linked to a specific appointment, and there is no NO_SHOW status. The
  closest real, non-invented signal is the patient's most recent
  CANCELLED/EXPIRED appointment_requests row (see
  `SchedulingDatabase.get_last_non_active_appointment_for_patient` and
  `SchedulingDatabaseCalendarAdapter.get_last_missed_slot_hint`), used only
  to rank/prefer slots near that session - never to restrict the patient
  to only that time, and never invented when no such row exists.

This feature does not touch email/SMTP/SES/provider code, the chatbot
classifier, or any unrelated staff feature - only the slot-detail/booking
path and its ranking.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.models import CaseStatus, ContactChannel, PatientRecord

from web.app import agent, app, calendar, data_store, patient_portal_tokens, scheduling_db


def reset_state():
    data_store._patients.clear()
    data_store._last_contacted.clear()
    agent.active_cases.clear()
    agent.undelivered.clear()
    patient_portal_tokens._tokens.clear()

    conn = scheduling_db.get_connection()
    try:
        conn.execute("DELETE FROM appointment_requests")
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM blocked_periods")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client():
    reset_state()
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client
    reset_state()


def make_patient(patient_id="T100", name="Taylor Patient", days_since_visit=60):
    return PatientRecord(
        patient_id=patient_id,
        name=name,
        contact_info={
            ContactChannel.SMS: "+15550000000",
            ContactChannel.EMAIL: "taylor@example.com",
        },
        preferred_channel=ContactChannel.SMS,
        last_visit_date=date.today() - timedelta(days=days_since_visit),
        treatment_type="cleaning",
        recall_interval_days=30,
    )


def seed_patient_with_case(patient_id="T100", name="Taylor Patient"):
    data_store.add_patient(make_patient(patient_id, name))
    agent.run_daily_cycle(date.today())
    return agent.get_case_by_patient_id(patient_id)


def issue_token(client, patient_id):
    resp = client.post(f"/api/patients/{patient_id}/portal-link")
    assert resp.status_code == 200
    return resp.get_json()["access_token"]


def get_available_slots_payload(client, token):
    resp = client.get(f"/api/patient-portal/{token}/available-slots")
    assert resp.status_code == 200
    return resp.get_json()


def first_date_and_time(payload):
    """Pull the first (date, session, time) triple out of a detailed
    available-slots payload's `dates` list."""
    entry = payload["dates"][0]
    time_entry = entry["times"][0]
    return entry["date"], time_entry["session"], time_entry["time"]


# ---------------------------------------------------------------------------
# Issue 1: the portal must render specific sessions/times, not only dates.
# ---------------------------------------------------------------------------

class TestPortalRendersSpecificSessionsAndTimes:
    def test_available_slots_response_includes_session_and_time_detail(self, client):
        seed_patient_with_case()
        token = issue_token(client, "T100")

        payload = get_available_slots_payload(client, token)
        assert payload["success"] is True
        assert "dates" in payload
        assert len(payload["dates"]) > 0

        first_date_entry = payload["dates"][0]
        assert "date" in first_date_entry
        assert len(first_date_entry["times"]) > 0
        for time_entry in first_date_entry["times"]:
            assert "session" in time_entry
            assert "time" in time_entry
            assert "label" in time_entry

    def test_multiple_sessions_on_the_same_date_are_all_offered(self, client):
        """If the clinic config offers more than one session per day
        (e.g. morning + afternoon), the portal must surface all of them
        for at least one date in the window, not just the first."""
        seed_patient_with_case()
        token = issue_token(client, "T100")
        payload = get_available_slots_payload(client, token)

        config = scheduling_db.get_config()
        if len(config["sessions"]) < 2:
            pytest.skip("clinic config only defines one session; nothing to prove here")

        sessions_seen = set()
        for entry in payload["dates"]:
            for time_entry in entry["times"]:
                sessions_seen.add(time_entry["session"])
        assert len(sessions_seen) >= 2


# ---------------------------------------------------------------------------
# Issue 1: selecting a displayed time books that EXACT time/session.
# ---------------------------------------------------------------------------

class TestSelectingADisplayedTimeBooksThatExactSlot:
    def test_selecting_a_specific_time_books_exactly_that_session_and_time(self, client):
        seed_patient_with_case()
        token = issue_token(client, "T100")
        payload = get_available_slots_payload(client, token)
        chosen_date, chosen_session, chosen_time = first_date_and_time(payload)

        resp = client.post(
            f"/api/patient-portal/{token}/select-slot",
            json={"appointment_date": chosen_date, "session": chosen_session, "time": chosen_time},
        )
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"] == "booked"
        assert data["booked_date"] == chosen_date

        conn = scheduling_db.get_connection()
        try:
            row = conn.execute(
                "SELECT slot_date, slot_session, slot_time, status FROM appointment_requests "
                "WHERE patient_id = ?",
                ("T100",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["slot_date"] == chosen_date
        assert row["slot_session"] == chosen_session
        assert row["slot_time"] == chosen_time
        assert row["status"] == "confirmed"

    def test_booking_a_later_time_on_a_date_does_not_book_the_earliest_slot_instead(self, client):
        """Direct regression for the original bug: booking must respect
        the specific time chosen, not silently fall back to the earliest
        available slot on that date."""
        seed_patient_with_case()
        token = issue_token(client, "T100")
        payload = get_available_slots_payload(client, token)

        # Find a date offering more than one time so "not the earliest" is
        # a meaningful assertion; otherwise fall back to any date's only time.
        multi_time_entry = next(
            (e for e in payload["dates"] if len(e["times"]) > 1), payload["dates"][0]
        )
        chosen_date = multi_time_entry["date"]
        later_time_entry = multi_time_entry["times"][-1]

        resp = client.post(
            f"/api/patient-portal/{token}/select-slot",
            json={
                "appointment_date": chosen_date,
                "session": later_time_entry["session"],
                "time": later_time_entry["time"],
            },
        )
        assert resp.get_json()["success"] is True

        conn = scheduling_db.get_connection()
        try:
            row = conn.execute(
                "SELECT slot_time FROM appointment_requests WHERE patient_id = ?",
                ("T100",),
            ).fetchone()
        finally:
            conn.close()
        assert row["slot_time"] == later_time_entry["time"]


# ---------------------------------------------------------------------------
# Staff Calendar must see the same date/session/time.
# ---------------------------------------------------------------------------

class TestStaffCalendarSeesTheExactBookedSlot:
    def test_staff_calendar_availability_reflects_the_specific_booked_slot(self, client):
        seed_patient_with_case()
        token = issue_token(client, "T100")
        payload = get_available_slots_payload(client, token)
        chosen_date, chosen_session, chosen_time = first_date_and_time(payload)

        client.post(
            f"/api/patient-portal/{token}/select-slot",
            json={"appointment_date": chosen_date, "session": chosen_session, "time": chosen_time},
        )

        availability = calendar.calendar_service.get_availability(chosen_date, chosen_date)
        matching = [
            s for s in availability
            if s["date"] == chosen_date and s["session"] == chosen_session and s["time"] == chosen_time
        ]
        assert len(matching) == 1
        assert matching[0]["booked"] >= 1


# ---------------------------------------------------------------------------
# A full/unavailable session must not be selectable / bookable.
# ---------------------------------------------------------------------------

class TestUnavailableSessionCannotBeBooked:
    def test_a_fully_booked_session_is_absent_from_available_slots_response(self, client):
        seed_patient_with_case()
        token = issue_token(client, "T100")
        payload = get_available_slots_payload(client, token)
        target_date, target_session, target_time = first_date_and_time(payload)

        config = scheduling_db.get_config()
        capacity = config["slots_per_session"]
        slot = next(
            s for s in calendar.calendar_service.generate_slots_for_date(target_date)
            if s["session"] == target_session and s["time"] == target_time
        )
        for i in range(capacity):
            result = calendar.calendar_service.check_capacity_and_book(
                patient_id=f"FILL-{i}",
                patient_name="Filler",
                slot_datetime_utc=slot["datetime_utc"],
                slot_date=slot["date"],
                slot_session=slot["session"],
                slot_time=slot["time"],
                requested_by="staff",
                source="staff",
            )
            assert result["success"]

        refreshed_payload = get_available_slots_payload(client, token)
        for entry in refreshed_payload["dates"]:
            if entry["date"] == target_date:
                for time_entry in entry["times"]:
                    assert not (
                        time_entry["session"] == target_session and time_entry["time"] == target_time
                    )

    def test_booking_a_now_full_session_is_rejected_server_side(self, client):
        seed_patient_with_case()
        token = issue_token(client, "T100")
        payload = get_available_slots_payload(client, token)
        target_date, target_session, target_time = first_date_and_time(payload)

        config = scheduling_db.get_config()
        capacity = config["slots_per_session"]
        slot = next(
            s for s in calendar.calendar_service.generate_slots_for_date(target_date)
            if s["session"] == target_session and s["time"] == target_time
        )
        for i in range(capacity):
            calendar.calendar_service.check_capacity_and_book(
                patient_id=f"FILL2-{i}",
                patient_name="Filler",
                slot_datetime_utc=slot["datetime_utc"],
                slot_date=slot["date"],
                slot_session=slot["session"],
                slot_time=slot["time"],
                requested_by="staff",
                source="staff",
            )

        resp = client.post(
            f"/api/patient-portal/{token}/select-slot",
            json={"appointment_date": target_date, "session": target_session, "time": target_time},
        )
        data = resp.get_json()
        assert data["success"] is False
        case = agent.get_case_by_patient_id("T100")
        assert case.status != CaseStatus.BOOKED


# ---------------------------------------------------------------------------
# A tampered time/session must be rejected server-side.
# ---------------------------------------------------------------------------

class TestTamperedSlotIsRejected:
    def test_a_session_name_that_does_not_exist_is_rejected(self, client):
        seed_patient_with_case()
        token = issue_token(client, "T100")
        payload = get_available_slots_payload(client, token)
        chosen_date, _, chosen_time = first_date_and_time(payload)

        resp = client.post(
            f"/api/patient-portal/{token}/select-slot",
            json={"appointment_date": chosen_date, "session": "midnight-heist", "time": chosen_time},
        )
        data = resp.get_json()
        assert data["success"] is False
        case = agent.get_case_by_patient_id("T100")
        assert case.status != CaseStatus.BOOKED

    def test_a_time_that_was_never_offered_on_that_date_is_rejected(self, client):
        seed_patient_with_case()
        token = issue_token(client, "T100")
        payload = get_available_slots_payload(client, token)
        chosen_date, chosen_session, _ = first_date_and_time(payload)

        resp = client.post(
            f"/api/patient-portal/{token}/select-slot",
            json={"appointment_date": chosen_date, "session": chosen_session, "time": "03:07"},
        )
        data = resp.get_json()
        assert data["success"] is False
        case = agent.get_case_by_patient_id("T100")
        assert case.status != CaseStatus.BOOKED

    def test_mismatched_session_and_time_pairing_is_rejected(self, client):
        """A session/time combination that individually exist elsewhere in
        the window, but were never paired together on this date, must
        still be rejected - the whole (date, session, time) tuple is
        re-validated, not each field independently."""
        seed_patient_with_case()
        token = issue_token(client, "T100")
        payload = get_available_slots_payload(client, token)

        config = scheduling_db.get_config()
        if len(config["sessions"]) < 2:
            pytest.skip("clinic config only defines one session; nothing to prove here")

        chosen_date, chosen_session, chosen_time = first_date_and_time(payload)
        other_session = next(s for s in config["sessions"] if s != chosen_session)

        resp = client.post(
            f"/api/patient-portal/{token}/select-slot",
            json={"appointment_date": chosen_date, "session": other_session, "time": chosen_time},
        )
        data = resp.get_json()
        # Only assert failure if that pairing genuinely wasn't offered.
        was_offered = any(
            e["date"] == chosen_date and any(
                t["session"] == other_session and t["time"] == chosen_time for t in e["times"]
            )
            for e in payload["dates"]
        )
        if not was_offered:
            assert data["success"] is False


# ---------------------------------------------------------------------------
# Issue 2: no-show-aware ranking prioritizes the missed session/time.
# ---------------------------------------------------------------------------

def _book_and_cancel(client, token, patient_id, session=None, time=None):
    """Book a slot (optionally a specific session/time) then cancel it
    directly in the DB, producing a CANCELLED row - the proxy this system
    uses for 'missed appointment' (see get_last_non_active_appointment_for_patient)."""
    payload = get_available_slots_payload(client, token)
    if session is not None:
        entry = next(
            e for e in payload["dates"]
            if any(t["session"] == session for t in e["times"])
        )
        time_entry = next(t for t in entry["times"] if t["session"] == session)
        chosen_date, chosen_session, chosen_time = entry["date"], time_entry["session"], time_entry["time"]
    else:
        chosen_date, chosen_session, chosen_time = first_date_and_time(payload)

    resp = client.post(
        f"/api/patient-portal/{token}/select-slot",
        json={"appointment_date": chosen_date, "session": chosen_session, "time": chosen_time},
    )
    assert resp.get_json()["success"] is True

    conn = scheduling_db.get_connection()
    try:
        row = conn.execute(
            "SELECT id, slot_datetime_utc FROM appointment_requests WHERE patient_id = ?",
            (patient_id,),
        ).fetchone()
        conn.execute(
            "UPDATE appointment_requests SET status = 'cancelled' WHERE id = ?",
            (row["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    return chosen_date, chosen_session, chosen_time


class TestNoShowAwareRanking:
    def test_afternoon_missed_appointment_prioritizes_afternoon_slots(self, client):
        seed_patient_with_case()
        token = issue_token(client, "T100")

        config = scheduling_db.get_config()
        if "afternoon" not in config["sessions"] or "morning" not in config["sessions"]:
            pytest.skip("clinic config does not define morning+afternoon sessions")

        _book_and_cancel(client, token, "T100", session="afternoon")

        # Re-run the daily cycle isn't needed; the case persists. Refresh
        # availability now that the patient has a CANCELLED history row.
        payload = get_available_slots_payload(client, token)
        assert payload["used_no_show_preference"] is True
        assert len(payload["preferred_dates"]) > 0
        for entry in payload["preferred_dates"]:
            for time_entry in entry["times"]:
                assert time_entry["session"] == "afternoon"

    def test_morning_missed_appointment_prioritizes_morning_slots(self, client):
        seed_patient_with_case()
        token = issue_token(client, "T100")

        config = scheduling_db.get_config()
        if "afternoon" not in config["sessions"] or "morning" not in config["sessions"]:
            pytest.skip("clinic config does not define morning+afternoon sessions")

        _book_and_cancel(client, token, "T100", session="morning")

        payload = get_available_slots_payload(client, token)
        assert payload["used_no_show_preference"] is True
        for entry in payload["preferred_dates"]:
            for time_entry in entry["times"]:
                assert time_entry["session"] == "morning"

    def test_other_available_times_remain_present_and_selectable(self, client):
        """Preference ranking must never hide or remove non-matching
        slots - they must still appear (in other_dates) and still be
        bookable."""
        seed_patient_with_case()
        token = issue_token(client, "T100")

        config = scheduling_db.get_config()
        if "afternoon" not in config["sessions"] or "morning" not in config["sessions"]:
            pytest.skip("clinic config does not define morning+afternoon sessions")

        _book_and_cancel(client, token, "T100", session="afternoon")

        payload = get_available_slots_payload(client, token)
        assert len(payload["other_dates"]) > 0
        morning_entry = next(
            (e for e in payload["other_dates"] if any(t["session"] == "morning" for t in e["times"])),
            None,
        )
        assert morning_entry is not None
        morning_time = next(t for t in morning_entry["times"] if t["session"] == "morning")

        resp = client.post(
            f"/api/patient-portal/{token}/select-slot",
            json={
                "appointment_date": morning_entry["date"],
                "session": "morning",
                "time": morning_time["time"],
            },
        )
        assert resp.get_json()["success"] is True

    def test_no_history_does_not_invent_a_preference(self, client):
        """A patient with no CANCELLED/EXPIRED appointment on file must
        get plain, unranked availability - the system must not invent a
        missed-appointment time that never happened."""
        seed_patient_with_case()
        token = issue_token(client, "T100")

        payload = get_available_slots_payload(client, token)
        assert payload["used_no_show_preference"] is False
        assert payload["preferred_dates"] == []
        assert len(payload["other_dates"]) > 0


# ---------------------------------------------------------------------------
# Booking persists after a simulated restart (reopening the database).
# ---------------------------------------------------------------------------

class TestSpecificSlotBookingPersistsAcrossRestart:
    def test_specific_session_and_time_survive_reopening_the_database(self, client):
        seed_patient_with_case()
        token = issue_token(client, "T100")
        payload = get_available_slots_payload(client, token)
        chosen_date, chosen_session, chosen_time = first_date_and_time(payload)

        client.post(
            f"/api/patient-portal/{token}/select-slot",
            json={"appointment_date": chosen_date, "session": chosen_session, "time": chosen_time},
        )

        from scheduling.database import SchedulingDatabase

        db_path = scheduling_db.db_path
        reopened = SchedulingDatabase(db_path)
        conn = reopened.get_connection()
        try:
            row = conn.execute(
                "SELECT slot_date, slot_session, slot_time, status FROM appointment_requests "
                "WHERE patient_id = ?",
                ("T100",),
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row["slot_date"] == chosen_date
        assert row["slot_session"] == chosen_session
        assert row["slot_time"] == chosen_time
        assert row["status"] == "confirmed"
