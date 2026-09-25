"""
Data access layer for the Patient Follow-up Agent.

This module provides abstract interfaces for data storage and calendar integration,
along with concrete mock implementations for demonstration purposes.
In production, these would connect to actual clinic systems (PMS/EHR, scheduling software).
"""

from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from typing import Optional
import json
from pathlib import Path

from core.models import PatientRecord, ContactChannel


class PatientDataStore(ABC):
    """
    Abstract interface for patient data persistence.
    
    This interface abstracts away the underlying storage mechanism,
    allowing the agent to work with different clinic systems
    (SQL databases, REST APIs, file-based systems, etc.).
    """

    @abstractmethod
    def get_all_active_patients(self) -> list[PatientRecord]:
        """
        Retrieve all active patients who are eligible for follow-up.
        
        Returns:
            List of active patient records
        """
        pass

    @abstractmethod
    def get_patient_by_id(self, patient_id: str) -> Optional[PatientRecord]:
        """
        Retrieve a specific patient by their unique identifier.
        
        Args:
            patient_id: Unique patient identifier
            
        Returns:
            PatientRecord if found, None otherwise
        """
        pass

    @abstractmethod
    def update_last_contacted(self, patient_id: str, when: date) -> None:
        """
        Update the last contact date for a patient.
        
        Args:
            patient_id: Unique patient identifier
            when: Date when patient was last contacted
        """
        pass


class MockPatientDataStore(PatientDataStore):
    """
    Mock implementation of patient data storage using in-memory storage.
    
    This implementation stores patient records in memory and provides
    sample data for demonstration purposes. In production, this would
    be replaced with actual database connectivity.
    """

    def __init__(self):
        """Initialize with empty patient storage."""
        self._patients: dict[str, PatientRecord] = {}
        self._last_contacted: dict[str, date] = {}

    def add_patient(self, patient: PatientRecord) -> None:
        """
        Add a patient to the mock data store.
        
        Args:
            patient: Patient record to add
        """
        self._patients[patient.patient_id] = patient

    def get_all_active_patients(self) -> list[PatientRecord]:
        """Return all stored patients."""
        return list(self._patients.values())

    def get_patient_by_id(self, patient_id: str) -> Optional[PatientRecord]:
        """Retrieve patient by ID."""
        return self._patients.get(patient_id)

    def update_last_contacted(self, patient_id: str, when: date) -> None:
        """Record when a patient was last contacted."""
        self._last_contacted[patient_id] = when

    def get_last_contacted(self, patient_id: str) -> Optional[date]:
        """
        Retrieve the last contact date for a patient.
        
        Args:
            patient_id: Unique patient identifier
            
        Returns:
            Date of last contact, or None if never contacted
        """
        return self._last_contacted.get(patient_id)


def _normalize_email(value: Optional[str]) -> Optional[str]:
    """Lowercased, whitespace-stripped email for duplicate matching.
    Returns None for blank/missing input - a blank email must never match
    another blank email (that would incorrectly merge unrelated patients
    who both happen to have no email on file)."""
    if not value:
        return None
    normalized = value.strip().lower()
    return normalized or None


def _normalize_phone(value: Optional[str]) -> Optional[str]:
    """Digits-only phone number for duplicate matching (strips spaces,
    dashes, parentheses, a leading '+', etc., so "+1 (555) 000-0000" and
    "15550000000" are recognized as the same number). Returns None for
    blank/missing input, for the same reason as _normalize_email."""
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits or None


class PatientImportOutcome:
    """
    Result of one call to PatientDataStore.import_patient - which of the
    three duplicate-matching rules (if any) matched, and whether the
    patient was inserted, updated, or left unchanged.

    Attributes:
        action: One of "inserted", "updated", "skipped". "skipped" is
            currently unused by SqlitePatientDataStore (every call either
            inserts or updates) but is part of the contract so a future
            store implementation - or a caller's own dedup pre-check - can
            report a genuine no-op.
        patient_id: The patient_id actually written (the incoming record's
            id for an insert; the EXISTING record's id for an update
            matched by email/phone, which may differ from the incoming
            record's id).
        matched_by: How an existing record was found - "patient_id",
            "email", "phone", or None for a fresh insert.
    """
    __slots__ = ("action", "patient_id", "matched_by")

    def __init__(self, action: str, patient_id: str, matched_by: Optional[str]):
        self.action = action
        self.patient_id = patient_id
        self.matched_by = matched_by


#: Maps ContactChannel enum members to the `patients` table column that
#: stores that channel's contact detail. Shared by SqlitePatientDataStore's
#: read and write paths so the two can never drift out of sync.
_CONTACT_COLUMNS: dict[ContactChannel, str] = {
    ContactChannel.SMS: "contact_sms",
    ContactChannel.WHATSAPP: "contact_whatsapp",
    ContactChannel.EMAIL: "contact_email",
    ContactChannel.PHONE_CALL: "contact_phone_call",
}


class SqlitePatientDataStore(PatientDataStore):
    """
    SQLite-backed patient data persistence - the Patient Master Database.

    Persists into the SAME scheduling.db file (and the same SQLite
    connection pattern - see SchedulingDatabase.get_connection) that
    appointments already use, per the project's single-source-of-truth
    pattern (see core/scheduling_calendar_adapter.py's module docstring
    for the calendar-side precedent this mirrors). The `patients` table
    itself is created by SchedulingDatabase.init_schema (and documented as
    Migration003AddPatientsTable in scheduling/migrations.py) - this class
    only reads/writes rows, it does not create the table itself.

    MockPatientDataStore remains available and is still what tests use
    for an isolated, schema-free in-memory fake - this class is what
    web/app.py wires up in production so uploaded/imported patients
    survive a restart, exactly the same relationship
    SchedulingDatabaseCalendarAdapter has with MockCalendarIntegration.
    """

    def __init__(self, scheduling_db):
        """
        Args:
            scheduling_db: The shared SchedulingDatabase instance - the SAME
                one the staff calendar/appointments use, so `get_connection`
                always points at the one scheduling.db file.
        """
        self.scheduling_db = scheduling_db

    # -- PatientDataStore interface ---------------------------------------

    def get_all_active_patients(self) -> list[PatientRecord]:
        """
        Return every patient on file. Matches MockPatientDataStore's own
        behavior (see its docstring) of not filtering by opted_out or any
        other activity flag - "active" here means "on file", not
        "currently eligible for outreach" (PolicyGuard is what enforces
        opted_out downstream, not the store).
        """
        conn = self.scheduling_db.get_connection()
        try:
            rows = conn.execute('SELECT * FROM patients ORDER BY patient_id').fetchall()
            return [self._row_to_patient(row) for row in rows]
        finally:
            conn.close()

    def get_patient_by_id(self, patient_id: str) -> Optional[PatientRecord]:
        conn = self.scheduling_db.get_connection()
        try:
            row = conn.execute(
                'SELECT * FROM patients WHERE patient_id = ?', (patient_id,)
            ).fetchone()
            return self._row_to_patient(row) if row else None
        finally:
            conn.close()

    def update_last_contacted(self, patient_id: str, when: date) -> None:
        conn = self.scheduling_db.get_connection()
        try:
            conn.execute(
                'UPDATE patients SET last_contacted = ?, updated_at = ? WHERE patient_id = ?',
                (when.isoformat(), datetime.now(timezone.utc).isoformat(), patient_id),
            )
            conn.commit()
        finally:
            conn.close()

    # -- Extra methods mirroring MockPatientDataStore's concrete API ------

    def add_patient(self, patient: PatientRecord) -> None:
        """
        Insert-or-overwrite by patient_id, matching
        MockPatientDataStore.add_patient's own "last write wins, keyed by
        patient_id" contract exactly (no email/phone dedup, no blank-field
        protection) - for callers that already know they want a plain
        upsert-by-id (e.g. seeding sample/demo data). Upload/import flows
        that need the full duplicate-matching + merge behavior described
        in the class docstring should call `import_patient` instead.
        """
        self.import_patient(patient, match_by_id_only=True)

    def get_last_contacted(self, patient_id: str) -> Optional[date]:
        conn = self.scheduling_db.get_connection()
        try:
            row = conn.execute(
                'SELECT last_contacted FROM patients WHERE patient_id = ?', (patient_id,)
            ).fetchone()
            if row is None or row['last_contacted'] is None:
                return None
            return date.fromisoformat(row['last_contacted'])
        finally:
            conn.close()

    # -- Patient Master Database import/merge ------------------------------

    def import_patient(
        self, patient: PatientRecord, *, match_by_id_only: bool = False
    ) -> PatientImportOutcome:
        """
        Insert a new patient, or MERGE non-blank incoming fields onto an
        existing one - never INSERT a duplicate row for the same real
        patient. This is the single place duplicate-matching happens for
        the Patient Master Database; `/api/import-patients` (web/app.py)
        calls this once per uploaded record instead of doing its own
        id/name set-membership check.

        Duplicate matching priority (first match wins, checked in order):
          1. Same patient_id.
          2. Otherwise, same normalized email (case-insensitive, trimmed).
          3. Otherwise, same normalized phone (digits-only comparison,
             across contact_sms/contact_whatsapp/contact_phone_call - any
             of the incoming record's phone-shaped contact values may match
             any of the existing record's).
          Name alone is NEVER a match signal - two different real patients
          can share a name, and this must not silently merge them.

        On a match: every incoming field that is genuinely present
        (non-None, and non-blank for strings) overwrites the existing
        column; every incoming field that is None/blank leaves the
        existing value untouched - a blank incoming phone number, for
        example, never erases a phone number already on file. The
        existing patient_id is always kept (appointments already
        reference it - see class docstring), even if the incoming record
        carried a different id and matched by email/phone instead.

        On no match: inserts a new row using the incoming patient_id.

        Args:
            patient: The incoming record to import.
            match_by_id_only: When True, skips the email/phone lookup
                entirely and matches (or inserts) by patient_id alone -
                used by `add_patient` to reproduce
                MockPatientDataStore.add_patient's simpler contract.

        Returns:
            PatientImportOutcome describing what happened.
        """
        conn = self.scheduling_db.get_connection()
        try:
            existing_row = conn.execute(
                'SELECT * FROM patients WHERE patient_id = ?', (patient.patient_id,)
            ).fetchone()
            matched_by = "patient_id" if existing_row else None

            incoming_email = _normalize_email(
                patient.contact_info.get(ContactChannel.EMAIL)
            )
            incoming_phone = None
            for channel in (ContactChannel.SMS, ContactChannel.WHATSAPP, ContactChannel.PHONE_CALL):
                incoming_phone = incoming_phone or _normalize_phone(
                    patient.contact_info.get(channel)
                )

            if existing_row is None and not match_by_id_only and incoming_email:
                existing_row = conn.execute(
                    'SELECT * FROM patients WHERE normalized_email = ?', (incoming_email,)
                ).fetchone()
                if existing_row:
                    matched_by = "email"

            if existing_row is None and not match_by_id_only and incoming_phone:
                existing_row = conn.execute(
                    'SELECT * FROM patients WHERE normalized_phone = ?', (incoming_phone,)
                ).fetchone()
                if existing_row:
                    matched_by = "phone"

            now = datetime.now(timezone.utc).isoformat()

            if existing_row is None:
                conn.execute(
                    '''
                    INSERT INTO patients (
                        patient_id, name,
                        contact_sms, contact_whatsapp, contact_email, contact_phone_call,
                        normalized_email, normalized_phone,
                        preferred_channel, last_visit_date, treatment_type,
                        recall_interval_days, no_show_history, language, opted_out,
                        last_contacted, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        patient.patient_id,
                        patient.name,
                        patient.contact_info.get(ContactChannel.SMS),
                        patient.contact_info.get(ContactChannel.WHATSAPP),
                        patient.contact_info.get(ContactChannel.EMAIL),
                        patient.contact_info.get(ContactChannel.PHONE_CALL),
                        incoming_email,
                        incoming_phone,
                        patient.preferred_channel.value,
                        patient.last_visit_date.isoformat(),
                        patient.treatment_type,
                        patient.recall_interval_days,
                        patient.no_show_history,
                        patient.language,
                        int(patient.opted_out),
                        None,
                        now,
                        now,
                    ),
                )
                conn.commit()
                return PatientImportOutcome("inserted", patient.patient_id, None)

            # MERGE: only overwrite columns where the incoming value is
            # genuinely present. Every "existing_row[...]" fallback below
            # is what protects already-stored data from being blanked out.
            merged_name = patient.name.strip() if patient.name and patient.name.strip() else existing_row['name']

            def merged_contact(channel: ContactChannel, column: str) -> Optional[str]:
                incoming_value = patient.contact_info.get(channel)
                if incoming_value and str(incoming_value).strip():
                    return str(incoming_value).strip()
                return existing_row[column]

            new_contact_sms = merged_contact(ContactChannel.SMS, "contact_sms")
            new_contact_whatsapp = merged_contact(ContactChannel.WHATSAPP, "contact_whatsapp")
            new_contact_email = merged_contact(ContactChannel.EMAIL, "contact_email")
            new_contact_phone_call = merged_contact(ContactChannel.PHONE_CALL, "contact_phone_call")

            new_normalized_email = _normalize_email(new_contact_email)
            new_normalized_phone = (
                _normalize_phone(new_contact_sms)
                or _normalize_phone(new_contact_whatsapp)
                or _normalize_phone(new_contact_phone_call)
            )

            merged_preferred_channel = (
                patient.preferred_channel.value
                if patient.preferred_channel
                else existing_row['preferred_channel']
            )
            merged_last_visit_date = (
                patient.last_visit_date.isoformat()
                if patient.last_visit_date
                else existing_row['last_visit_date']
            )
            merged_treatment_type = (
                patient.treatment_type.strip()
                if patient.treatment_type and patient.treatment_type.strip()
                else existing_row['treatment_type']
            )
            merged_recall_interval = (
                patient.recall_interval_days
                if patient.recall_interval_days is not None
                else existing_row['recall_interval_days']
            )
            # no_show_history/language/opted_out: 0/"en"/False are all
            # legitimate, meaningful values (not "blank"), so these always
            # take the incoming value - there is no sentinel-vs-real-zero
            # ambiguity to protect against here, unlike the string fields
            # above where an empty string is unambiguously "no new data".
            merged_no_show_history = patient.no_show_history
            merged_language = patient.language or existing_row['language']
            merged_opted_out = int(patient.opted_out)

            existing_patient_id = existing_row['patient_id']
            conn.execute(
                '''
                UPDATE patients SET
                    name = ?,
                    contact_sms = ?, contact_whatsapp = ?, contact_email = ?, contact_phone_call = ?,
                    normalized_email = ?, normalized_phone = ?,
                    preferred_channel = ?, last_visit_date = ?, treatment_type = ?,
                    recall_interval_days = ?, no_show_history = ?, language = ?, opted_out = ?,
                    updated_at = ?
                WHERE patient_id = ?
                ''',
                (
                    merged_name,
                    new_contact_sms, new_contact_whatsapp, new_contact_email, new_contact_phone_call,
                    new_normalized_email, new_normalized_phone,
                    merged_preferred_channel, merged_last_visit_date, merged_treatment_type,
                    merged_recall_interval, merged_no_show_history, merged_language, merged_opted_out,
                    now,
                    existing_patient_id,
                ),
            )
            conn.commit()
            return PatientImportOutcome("updated", existing_patient_id, matched_by)
        finally:
            conn.close()

    # -- internal ------------------------------------------------------

    @staticmethod
    def _row_to_patient(row) -> PatientRecord:
        contact_info: dict[ContactChannel, str] = {}
        for channel, column in _CONTACT_COLUMNS.items():
            value = row[column]
            if value:
                contact_info[channel] = value

        return PatientRecord(
            patient_id=row['patient_id'],
            name=row['name'],
            contact_info=contact_info,
            preferred_channel=ContactChannel(row['preferred_channel']),
            last_visit_date=date.fromisoformat(row['last_visit_date']),
            treatment_type=row['treatment_type'],
            recall_interval_days=row['recall_interval_days'],
            no_show_history=row['no_show_history'],
            language=row['language'],
            opted_out=bool(row['opted_out']),
        )


class CalendarIntegration(ABC):
    """
    Abstract interface for appointment scheduling system integration.
    
    This interface allows the agent to interact with the clinic's
    scheduling system to find available slots and book appointments.
    """

    @abstractmethod
    def find_available_slots(
        self,
        treatment_type: str,
        after: date,
        limit: int = 5,
        to_date: Optional[date] = None,
    ) -> list[date]:
        """
        Find available appointment slots for a given treatment type.
        
        Args:
            treatment_type: Type of treatment requiring appointment
            after: Find slots after this date
            limit: Maximum number of slots to return
            to_date: Optional upper bound on the search window (e.g. the
                clinic's configured booking window). When given, no slot
                later than this date is returned, even if the 60-day safety
                cap would otherwise allow it. When omitted, only the 60-day
                safety cap applies (unchanged from before).
            
        Returns:
            List of available dates
        """
        pass

    @abstractmethod
    def book_appointment(
        self, patient_id: str, appointment_date: date, treatment_type: str
    ) -> bool:
        """
        Book an appointment for a patient.
        
        Args:
            patient_id: Unique patient identifier
            appointment_date: Requested appointment date
            treatment_type: Type of treatment
            
        Returns:
            True if booking successful, False otherwise
        """
        pass

    @abstractmethod
    def cancel_appointment(self, patient_id: str, appointment_date: date) -> bool:
        """
        Cancel an existing appointment.
        
        Args:
            patient_id: Unique patient identifier
            appointment_date: Date of appointment to cancel
            
        Returns:
            True if cancellation successful, False otherwise
        """
        pass


class MockCalendarIntegration(CalendarIntegration):
    """
    Mock implementation of calendar/scheduling system.
    
    Simulates appointment availability and booking for demonstration.
    In production, this would integrate with actual scheduling software
    (e.g., Dentrix, Open Dental, Google Calendar API).
    """

    def __init__(self):
        """Initialize with empty appointment storage."""
        # Dictionary mapping (patient_id, date) to treatment_type
        self._appointments: dict[tuple[str, date], str] = {}
        # Simulated blocked dates (weekends, holidays, etc.)
        self._blocked_dates: set[date] = set()

    def find_available_slots(
        self,
        treatment_type: str,
        after: date,
        limit: int = 5,
        to_date: Optional[date] = None,
    ) -> list[date]:
        """
        Generate mock available slots.
        
        Simulates availability by returning weekday dates that aren't
        already fully booked.
        """
        available_slots = []
        current_date = after + timedelta(days=1)

        # The 60-day safety cap always applies; `to_date` (e.g. the clinic's
        # configured booking window) may narrow it further, but never widen
        # it beyond 60 days.
        safety_cap = after + timedelta(days=60)
        hard_stop = min(safety_cap, to_date) if to_date is not None else safety_cap

        while len(available_slots) < limit:
            # Skip weekends (5=Saturday, 6=Sunday)
            if current_date.weekday() < 5 and current_date not in self._blocked_dates:
                # Check if date has capacity (simplified: max 10 appointments per day)
                daily_appointments = sum(
                    1 for (_, appt_date) in self._appointments.keys()
                    if appt_date == current_date
                )
                if daily_appointments < 10:
                    available_slots.append(current_date)
            
            current_date += timedelta(days=1)

            if current_date > hard_stop:
                break
        
        return available_slots

    def book_appointment(
        self, patient_id: str, appointment_date: date, treatment_type: str
    ) -> bool:
        """
        Book a mock appointment.
        
        Checks for conflicts and availability before booking.
        """
        # Check if slot is available
        key = (patient_id, appointment_date)
        
        # Check if patient already has appointment on this date
        if key in self._appointments:
            return False
        
        # Check if date is blocked
        if appointment_date in self._blocked_dates:
            return False
        
        # Check weekday
        if appointment_date.weekday() >= 5:
            return False
        
        # Book the appointment
        self._appointments[key] = treatment_type
        return True

    def cancel_appointment(self, patient_id: str, appointment_date: date) -> bool:
        """Cancel a mock appointment."""
        key = (patient_id, appointment_date)
        if key in self._appointments:
            del self._appointments[key]
            return True
        return False

    def get_appointments_for_patient(self, patient_id: str) -> list[tuple[date, str]]:
        """
        Get all appointments for a specific patient.
        
        Args:
            patient_id: Unique patient identifier
            
        Returns:
            List of (date, treatment_type) tuples
        """
        return [
            (appt_date, treatment)
            for (pid, appt_date), treatment in self._appointments.items()
            if pid == patient_id
        ]

    def block_date(self, block_date: date) -> None:
        """
        Mark a date as unavailable (e.g., holiday, clinic closed).
        
        Args:
            block_date: Date to block
        """
        self._blocked_dates.add(block_date)
