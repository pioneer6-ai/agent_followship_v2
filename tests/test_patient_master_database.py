"""
Tests for the persistent Patient Master Database:
SqlitePatientDataStore (core/data_access.py), backed by the SAME
scheduling.db file appointments already use (the `patients` table, added
via Migration003AddPatientsTable / SchedulingDatabase.init_schema - see
scheduling/migrations.py and scheduling/database.py).

Covers:
  - Idempotent, restart-surviving persistence of uploaded/imported patients
    (previously they only ever lived in MockPatientDataStore's in-memory
    dict - see core/data_access.py's MockPatientDataStore, which is
    UNCHANGED and still used by tests that want an isolated fake).
  - Duplicate-matching priority: same patient_id, otherwise normalized
    email, otherwise normalized phone - name alone never matches.
  - Merge-not-overwrite semantics: a blank/missing incoming field must
    never erase a non-blank value already on file.
  - Appointments continuing to reference patients by patient_id across
    this change (no FK, no coupling introduced).

None of these tests touch calendar/email/chatbot behavior - only the
patient store and the /api/import-patients route's use of it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.data_access import PatientDataStore, SqlitePatientDataStore
from core.models import ContactChannel, PatientRecord
from core.scheduling_calendar_adapter import SchedulingDatabaseCalendarAdapter
from scheduling.database import SchedulingDatabase


@pytest.fixture
def scheduling_db(tmp_path):
    db_path = str(tmp_path / "patient_master_test.db")
    return SchedulingDatabase(db_path)


@pytest.fixture
def store(scheduling_db):
    return SqlitePatientDataStore(scheduling_db)


def make_patient(
    patient_id="P1",
    name="Alex Patient",
    email="alex@example.com",
    phone="+1-555-000-0000",
    last_visit_date=None,
    treatment_type="cleaning",
    recall_interval_days=180,
    no_show_history=0,
    language="en",
    opted_out=False,
):
    contact_info = {}
    if phone:
        contact_info[ContactChannel.SMS] = phone
    if email:
        contact_info[ContactChannel.EMAIL] = email
    return PatientRecord(
        patient_id=patient_id,
        name=name,
        contact_info=contact_info,
        preferred_channel=ContactChannel.SMS if phone else ContactChannel.EMAIL,
        last_visit_date=last_visit_date or (date.today() - timedelta(days=200)),
        treatment_type=treatment_type,
        recall_interval_days=recall_interval_days,
        no_show_history=no_show_history,
        language=language,
        opted_out=opted_out,
    )


# ---------------------------------------------------------------------------
# Schema / interface sanity
# ---------------------------------------------------------------------------

class TestStoreImplementsThePatientDataStoreInterface:
    def test_is_a_real_patient_data_store(self, store):
        assert isinstance(store, PatientDataStore)

    def test_empty_store_returns_no_patients(self, store):
        assert store.get_all_active_patients() == []
        assert store.get_patient_by_id("does-not-exist") is None


# ---------------------------------------------------------------------------
# Duplicate matching priority 1: same patient_id.
# ---------------------------------------------------------------------------

class TestDuplicateMatchingByPatientId:
    def test_first_import_inserts(self, store):
        outcome = store.import_patient(make_patient(patient_id="P1"))
        assert outcome.action == "inserted"
        assert outcome.patient_id == "P1"
        assert len(store.get_all_active_patients()) == 1

    def test_same_patient_id_again_updates_not_inserts(self, store):
        store.import_patient(make_patient(patient_id="P1", name="Alex Patient"))
        outcome = store.import_patient(make_patient(patient_id="P1", name="Alex A. Patient"))

        assert outcome.action == "updated"
        assert outcome.matched_by == "patient_id"
        assert len(store.get_all_active_patients()) == 1
        assert store.get_patient_by_id("P1").name == "Alex A. Patient"


# ---------------------------------------------------------------------------
# Duplicate matching priority 2: otherwise, normalized email.
# ---------------------------------------------------------------------------

class TestDuplicateMatchingByEmail:
    def test_different_patient_id_same_email_merges(self, store):
        store.import_patient(make_patient(patient_id="P1", email="Alex@Example.com"))
        outcome = store.import_patient(
            make_patient(patient_id="P1-DUPLICATE-UPLOAD", email="alex@example.com  ")
        )

        assert outcome.action == "updated"
        assert outcome.matched_by == "email"
        # The ORIGINAL patient_id is kept - appointments already reference it.
        assert outcome.patient_id == "P1"
        assert len(store.get_all_active_patients()) == 1

    def test_email_comparison_is_case_and_whitespace_insensitive(self, store):
        store.import_patient(make_patient(patient_id="P1", email="alex@example.com"))
        outcome = store.import_patient(
            make_patient(patient_id="P2", email="  ALEX@EXAMPLE.COM  ")
        )
        assert outcome.action == "updated"
        assert outcome.matched_by == "email"

    def test_blank_emails_never_match_each_other(self, store):
        """Two different real patients who both happen to have no email on
        file must NOT be merged into one record."""
        store.import_patient(make_patient(patient_id="P1", email=None, phone="+15550001111"))
        outcome = store.import_patient(
            make_patient(patient_id="P2", email=None, phone="+15550002222")
        )
        assert outcome.action == "inserted"
        assert len(store.get_all_active_patients()) == 2


# ---------------------------------------------------------------------------
# Duplicate matching priority 3: otherwise, normalized phone.
# ---------------------------------------------------------------------------

class TestDuplicateMatchingByPhone:
    def test_different_patient_id_same_phone_merges(self, store):
        store.import_patient(make_patient(patient_id="P1", email=None, phone="+1 (555) 000-1234"))
        outcome = store.import_patient(
            make_patient(patient_id="P2", email=None, phone="15550001234")
        )
        assert outcome.action == "updated"
        assert outcome.matched_by == "phone"
        assert outcome.patient_id == "P1"
        assert len(store.get_all_active_patients()) == 1

    def test_phone_match_only_used_when_no_id_or_email_match(self, store):
        """Priority order: patient_id first, then email, then phone -
        confirmed by giving two records overlapping phone numbers but
        distinct patient_id/email, then checking a genuinely new record
        with a matching id is matched by id, not merged via phone into a
        third record."""
        store.import_patient(make_patient(patient_id="P1", email="one@example.com", phone="+15550009999"))
        store.import_patient(make_patient(patient_id="P2", email="two@example.com", phone="+15551112222"))

        # Same patient_id as P1, different email/phone - must match by id,
        # not accidentally cross-merge with P2's phone.
        outcome = store.import_patient(
            make_patient(patient_id="P1", email="one-new@example.com", phone="+15551112222")
        )
        assert outcome.action == "updated"
        assert outcome.matched_by == "patient_id"
        assert outcome.patient_id == "P1"
        assert len(store.get_all_active_patients()) == 2


# ---------------------------------------------------------------------------
# Name alone must NOT auto-merge.
# ---------------------------------------------------------------------------

class TestNameAloneNeverMerges:
    def test_same_name_different_id_email_phone_creates_a_second_record(self, store):
        store.import_patient(
            make_patient(patient_id="P1", name="Jordan Lee", email="jordan.a@example.com", phone="+15550001111")
        )
        outcome = store.import_patient(
            make_patient(patient_id="P2", name="Jordan Lee", email="jordan.b@example.com", phone="+15559998888")
        )
        assert outcome.action == "inserted"
        assert len(store.get_all_active_patients()) == 2


# ---------------------------------------------------------------------------
# Blank-field protection: incoming blanks must never erase existing data.
# ---------------------------------------------------------------------------

class TestBlankIncomingFieldsNeverOverwriteExistingData:
    def test_blank_incoming_email_does_not_erase_existing_email(self, store):
        store.import_patient(make_patient(patient_id="P1", email="alex@example.com", phone="+15550001111"))
        store.import_patient(make_patient(patient_id="P1", email=None, phone="+15550001111"))

        patient = store.get_patient_by_id("P1")
        assert patient.contact_info[ContactChannel.EMAIL] == "alex@example.com"

    def test_blank_incoming_phone_does_not_erase_existing_phone(self, store):
        store.import_patient(make_patient(patient_id="P1", email="alex@example.com", phone="+15550001111"))
        store.import_patient(make_patient(patient_id="P1", email="alex@example.com", phone=None))

        patient = store.get_patient_by_id("P1")
        assert patient.contact_info[ContactChannel.SMS] == "+15550001111"

    def test_a_genuinely_new_value_still_overwrites(self, store):
        """Blank-field protection must not become "never update" - a real
        new value for an already-populated field must still take effect."""
        store.import_patient(make_patient(patient_id="P1", email="old@example.com"))
        store.import_patient(make_patient(patient_id="P1", email="new@example.com"))

        assert store.get_patient_by_id("P1").contact_info[ContactChannel.EMAIL] == "new@example.com"

    def test_blank_name_does_not_erase_existing_name(self, store):
        store.import_patient(make_patient(patient_id="P1", name="Alex Patient"))
        store.import_patient(make_patient(patient_id="P1", name=""))
        assert store.get_patient_by_id("P1").name == "Alex Patient"


# ---------------------------------------------------------------------------
# Repeated import is idempotent.
# ---------------------------------------------------------------------------

class TestRepeatedImportIsIdempotent:
    def test_importing_the_identical_record_three_times_yields_one_row(self, store):
        patient = make_patient(patient_id="P1", email="alex@example.com")
        for _ in range(3):
            store.import_patient(patient)

        assert len(store.get_all_active_patients()) == 1

    def test_importing_the_identical_record_twice_reports_update_the_second_time(self, store):
        patient = make_patient(patient_id="P1")
        first = store.import_patient(patient)
        second = store.import_patient(patient)

        assert first.action == "inserted"
        assert second.action == "updated"


# ---------------------------------------------------------------------------
# Persistence across "restart" (reopening the database).
# ---------------------------------------------------------------------------

class TestPersistenceAcrossRestart:
    def test_imported_patient_survives_reopening_the_database(self, scheduling_db, store):
        store.import_patient(make_patient(patient_id="P1", name="Alex Patient"))

        db_path = scheduling_db.db_path
        del scheduling_db, store  # simulate dropping every in-memory reference

        reopened_db = SchedulingDatabase(db_path)
        reopened_store = SqlitePatientDataStore(reopened_db)

        patient = reopened_store.get_patient_by_id("P1")
        assert patient is not None
        assert patient.name == "Alex Patient"

    def test_updated_fields_also_survive_a_restart(self, scheduling_db, store):
        store.import_patient(make_patient(patient_id="P1", treatment_type="cleaning"))
        store.import_patient(make_patient(patient_id="P1", treatment_type="root_canal_followup"))

        db_path = scheduling_db.db_path
        del scheduling_db, store

        reopened_store = SqlitePatientDataStore(SchedulingDatabase(db_path))
        assert reopened_store.get_patient_by_id("P1").treatment_type == "root_canal_followup"

    def test_last_contacted_survives_a_restart(self, scheduling_db, store):
        store.import_patient(make_patient(patient_id="P1"))
        store.update_last_contacted("P1", date(2026, 1, 15))

        db_path = scheduling_db.db_path
        del scheduling_db, store

        reopened_store = SqlitePatientDataStore(SchedulingDatabase(db_path))
        assert reopened_store.get_last_contacted("P1") == date(2026, 1, 15)


# ---------------------------------------------------------------------------
# Existing appointments remain linked by patient_id.
# ---------------------------------------------------------------------------

class TestAppointmentsRemainLinkedByPatientId:
    def test_an_appointment_booked_before_the_patient_exists_in_the_master_db_still_works(
        self, scheduling_db, store
    ):
        """Appointments have no FK to patients.patient_id (matching the
        existing schema - see Migration003AddPatientsTable's docstring),
        so booking for a patient_id that predates/never gets a `patients`
        row must keep working exactly as it does today."""
        calendar = SchedulingDatabaseCalendarAdapter(scheduling_db, requested_by="patient_portal")
        today = date.today()
        target_date = calendar.find_available_slots(
            "cleaning", after=today, limit=1, to_date=today + timedelta(days=14)
        )[0]

        outcome = calendar.book_appointment_detailed(
            "WALK-IN-1", target_date, "cleaning", patient_name="Walk In Patient"
        )
        assert outcome.success

        # No row in `patients` for this id at all.
        assert store.get_patient_by_id("WALK-IN-1") is None

    def test_appointments_survive_a_patient_record_being_updated(self, scheduling_db, store):
        """Merging new data into a patient's record must not touch, move,
        or orphan that patient's existing appointment rows."""
        calendar = SchedulingDatabaseCalendarAdapter(scheduling_db, requested_by="patient_portal")
        today = date.today()
        target_date = calendar.find_available_slots(
            "cleaning", after=today, limit=1, to_date=today + timedelta(days=14)
        )[0]

        store.import_patient(make_patient(patient_id="P1", email="alex@example.com"))
        outcome = calendar.book_appointment_detailed(
            "P1", target_date, "cleaning", patient_name="Alex Patient"
        )
        assert outcome.success

        # Update the patient record (merge, not insert) - same patient_id.
        store.import_patient(make_patient(patient_id="P1", email="alex@example.com", treatment_type="checkup"))

        appointments = calendar.get_appointments_for_patient("P1")
        assert len(appointments) == 1
        assert appointments[0][0] == target_date

    def test_appointment_and_patient_row_both_survive_a_restart(self, scheduling_db, store):
        calendar = SchedulingDatabaseCalendarAdapter(scheduling_db, requested_by="patient_portal")
        today = date.today()
        target_date = calendar.find_available_slots(
            "cleaning", after=today, limit=1, to_date=today + timedelta(days=14)
        )[0]

        store.import_patient(make_patient(patient_id="P1", name="Alex Patient"))
        booking = calendar.book_appointment_detailed(
            "P1", target_date, "cleaning", patient_name="Alex Patient"
        )
        assert booking.success

        db_path = scheduling_db.db_path
        del scheduling_db, store, calendar

        reopened_db = SchedulingDatabase(db_path)
        reopened_store = SqlitePatientDataStore(reopened_db)
        reopened_calendar = SchedulingDatabaseCalendarAdapter(reopened_db, requested_by="patient_portal")

        assert reopened_store.get_patient_by_id("P1").name == "Alex Patient"
        assert len(reopened_calendar.get_appointments_for_patient("P1")) == 1


# ---------------------------------------------------------------------------
# add_patient (MockPatientDataStore-compatible convenience method).
# ---------------------------------------------------------------------------

class TestAddPatientMatchesMockContractForSimpleCallers:
    def test_add_patient_upserts_by_id_like_the_mock_store_does(self, store):
        store.add_patient(make_patient(patient_id="P1", name="First"))
        store.add_patient(make_patient(patient_id="P1", name="Second"))

        assert len(store.get_all_active_patients()) == 1
        assert store.get_patient_by_id("P1").name == "Second"
