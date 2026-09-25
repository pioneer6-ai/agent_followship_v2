import sqlite3
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any
from enum import Enum


class AppointmentStatus(str, Enum):
    PENDING = 'pending'
    CONFIRMED = 'confirmed'
    DECLINED = 'declined'
    CANCELLED = 'cancelled'
    EXPIRED = 'expired'
    COMPLETED = 'completed'
    NO_SHOW = 'no_show'


class SchedulingDatabase:
    def __init__(self, db_path: str = 'scheduling.db'):
        self.db_path = db_path
        self.init_schema()
    
    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def init_schema(self):
        conn = self.get_connection()
        try:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS clinic_config (
                    id INTEGER PRIMARY KEY,
                    timezone_name TEXT NOT NULL DEFAULT 'America/New_York',
                    working_days TEXT NOT NULL,
                    sessions TEXT NOT NULL,
                    slot_duration_minutes INTEGER NOT NULL DEFAULT 30,
                    slots_per_session INTEGER NOT NULL DEFAULT 8,
                    pending_expiry_minutes INTEGER NOT NULL DEFAULT 1440,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                
                CREATE TABLE IF NOT EXISTS blocked_periods (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_datetime TEXT NOT NULL,
                    end_datetime TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_by TEXT,
                    created_at TEXT NOT NULL
                );
                
                CREATE TABLE IF NOT EXISTS appointment_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    patient_name TEXT NOT NULL,
                    follow_up_case_id TEXT,
                    slot_date TEXT NOT NULL,
                    slot_session TEXT NOT NULL,
                    slot_time TEXT NOT NULL,
                    slot_datetime_utc TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    follow_up_reason TEXT,
                    requested_by TEXT NOT NULL,
                    approved_by TEXT,
                    approved_at TEXT,
                    declined_reason TEXT,
                    expires_at TEXT,
                    expired_at TEXT,
                    source TEXT NOT NULL DEFAULT 'staff',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                
                CREATE INDEX IF NOT EXISTS idx_appointments_status ON appointment_requests(status);
                CREATE INDEX IF NOT EXISTS idx_appointments_patient ON appointment_requests(patient_id);
                CREATE INDEX IF NOT EXISTS idx_appointments_slot ON appointment_requests(slot_datetime_utc);
                CREATE INDEX IF NOT EXISTS idx_appointments_expires ON appointment_requests(expires_at);
                
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    appointment_request_id INTEGER,
                    action TEXT NOT NULL,
                    actor TEXT,
                    details TEXT,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (appointment_request_id) REFERENCES appointment_requests(id)
                );
                
                CREATE INDEX IF NOT EXISTS idx_audit_appointment ON audit_log(appointment_request_id);

                CREATE TABLE IF NOT EXISTS patients (
                    patient_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    contact_sms TEXT,
                    contact_whatsapp TEXT,
                    contact_email TEXT,
                    contact_phone_call TEXT,
                    normalized_email TEXT,
                    normalized_phone TEXT,
                    preferred_channel TEXT NOT NULL,
                    last_visit_date TEXT NOT NULL,
                    treatment_type TEXT NOT NULL,
                    recall_interval_days INTEGER NOT NULL,
                    no_show_history INTEGER NOT NULL DEFAULT 0,
                    language TEXT NOT NULL DEFAULT 'en',
                    opted_out INTEGER NOT NULL DEFAULT 0,
                    last_contacted TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_patients_normalized_email ON patients(normalized_email);
                CREATE INDEX IF NOT EXISTS idx_patients_normalized_phone ON patients(normalized_phone);
            ''')

            # Idempotent guard for databases created before the `source`
            # column existed (CREATE TABLE IF NOT EXISTS above is a no-op
            # on an existing table, so a pre-existing scheduling.db needs
            # this explicit ALTER TABLE to pick it up). Safe to run on
            # every startup - it only acts when the column is missing.
            existing_columns = {
                row['name'] for row in conn.execute('PRAGMA table_info(appointment_requests)')
            }
            if 'source' not in existing_columns:
                conn.execute('''
                    ALTER TABLE appointment_requests
                    ADD COLUMN source TEXT NOT NULL DEFAULT 'staff'
                ''')
                conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_appointments_source
                    ON appointment_requests(source)
                ''')

            cursor = conn.execute('SELECT COUNT(*) as count FROM clinic_config')
            if cursor.fetchone()['count'] == 0:
                now = datetime.now(timezone.utc).isoformat()
                default_config = {
                    'working_days': [0, 1, 2, 3, 4],
                    'sessions': {
                        'morning': {'start': '09:00', 'end': '12:00'},
                        'afternoon': {'start': '13:00', 'end': '17:00'}
                    }
                }
                conn.execute('''
                    INSERT INTO clinic_config 
                    (timezone_name, working_days, sessions, slot_duration_minutes, 
                     slots_per_session, pending_expiry_minutes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', ('America/New_York', json.dumps(default_config['working_days']),
                      json.dumps(default_config['sessions']), 30, 8, 1440, now, now))
            conn.commit()
        finally:
            conn.close()
    
    def get_config(self) -> Dict[str, Any]:
        conn = self.get_connection()
        try:
            row = conn.execute('SELECT * FROM clinic_config LIMIT 1').fetchone()
            if row:
                return {
                    'id': row['id'],
                    'timezone_name': row['timezone_name'],
                    'working_days': json.loads(row['working_days']),
                    'sessions': json.loads(row['sessions']),
                    'slot_duration_minutes': row['slot_duration_minutes'],
                    'slots_per_session': row['slots_per_session'],
                    'pending_expiry_minutes': row['pending_expiry_minutes']
                }
            return {}
        finally:
            conn.close()
    
    def set_config(self, config: Dict[str, Any]) -> bool:
        """Update clinic configuration."""
        try:
            self.update_config(config)
            return True
        except Exception:
            return False
    
    def update_config(self, config: Dict[str, Any]):
        now = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        try:
            conn.execute('''
                UPDATE clinic_config SET
                    timezone_name = ?,
                    working_days = ?,
                    sessions = ?,
                    slot_duration_minutes = ?,
                    slots_per_session = ?,
                    pending_expiry_minutes = ?,
                    updated_at = ?
                WHERE id = 1
            ''', (config['timezone_name'], json.dumps(config['working_days']),
                  json.dumps(config['sessions']), config['slot_duration_minutes'],
                  config['slots_per_session'], config['pending_expiry_minutes'], now))
            conn.commit()
        finally:
            conn.close()
    
    def create_appointment_request(self, **kwargs) -> int:
        now = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        try:
            cursor = conn.execute('''
                INSERT INTO appointment_requests
                (patient_id, patient_name, follow_up_case_id, slot_date, slot_session,
                 slot_time, slot_datetime_utc, status, follow_up_reason, requested_by,
                 expires_at, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (kwargs['patient_id'], kwargs['patient_name'], kwargs.get('follow_up_case_id'),
                  kwargs['slot_date'], kwargs['slot_session'], kwargs['slot_time'],
                  kwargs['slot_datetime_utc'], AppointmentStatus.PENDING.value,
                  kwargs.get('follow_up_reason'), kwargs['requested_by'],
                  kwargs['expires_at'], kwargs.get('source', 'staff'), now, now))
            appt_id = cursor.lastrowid
            
            conn.execute('''
                INSERT INTO audit_log (appointment_request_id, action, actor, details, timestamp)
                VALUES (?, ?, ?, ?, ?)
            ''', (appt_id, 'created', kwargs['requested_by'], 
                  json.dumps({'status': 'pending'}), now))
            conn.commit()
            return appt_id
        finally:
            conn.close()
    
    def get_appointment(self, appt_id: int) -> Optional[Dict]:
        conn = self.get_connection()
        try:
            row = conn.execute('SELECT * FROM appointment_requests WHERE id = ?', (appt_id,)).fetchone()
            if row:
                return dict(row)
            return None
        finally:
            conn.close()
    
    def get_pending_requests(self) -> List[Dict]:
        conn = self.get_connection()
        try:
            rows = conn.execute('''
                SELECT * FROM appointment_requests 
                WHERE status = ? 
                ORDER BY created_at DESC
            ''', (AppointmentStatus.PENDING.value,)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    
    def approve_appointment(self, appt_id: int, actor: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        try:
            row = conn.execute('SELECT status FROM appointment_requests WHERE id = ?', (appt_id,)).fetchone()
            if not row or row['status'] != AppointmentStatus.PENDING.value:
                return False
            
            conn.execute('''
                UPDATE appointment_requests SET
                    status = ?,
                    approved_by = ?,
                    approved_at = ?,
                    expires_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status = ?
            ''', (AppointmentStatus.CONFIRMED.value, actor, now, now, appt_id, AppointmentStatus.PENDING.value))
            
            if conn.total_changes > 0:
                conn.execute('''
                    INSERT INTO audit_log (appointment_request_id, action, actor, timestamp)
                    VALUES (?, ?, ?, ?)
                ''', (appt_id, 'approved', actor, now))
                conn.commit()
                return True
            return False
        finally:
            conn.close()
    
    def decline_appointment(self, appt_id: int, actor: str, reason: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        try:
            conn.execute('''
                UPDATE appointment_requests SET
                    status = ?,
                    declined_reason = ?,
                    expires_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status = ?
            ''', (AppointmentStatus.DECLINED.value, reason, now, appt_id, AppointmentStatus.PENDING.value))
            
            if conn.total_changes > 0:
                conn.execute('''
                    INSERT INTO audit_log (appointment_request_id, action, actor, details, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                ''', (appt_id, 'declined', actor, json.dumps({'reason': reason}), now))
                conn.commit()
                return True
            return False
        finally:
            conn.close()
    
    def cancel_appointment(self, appt_id: int, actor: str, reason: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        try:
            conn.execute('''
                UPDATE appointment_requests SET
                    status = ?,
                    declined_reason = ?,
                    updated_at = ?
                WHERE id = ? AND status IN (?, ?)
            ''', (AppointmentStatus.CANCELLED.value, reason, now, appt_id,
                  AppointmentStatus.PENDING.value, AppointmentStatus.CONFIRMED.value))
            
            if conn.total_changes > 0:
                conn.execute('''
                    INSERT INTO audit_log (appointment_request_id, action, actor, details, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                ''', (appt_id, 'cancelled', actor, json.dumps({'reason': reason}), now))
                conn.commit()
                return True
            return False
        finally:
            conn.close()
    
    def complete_appointment(self, appt_id: int, actor: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        try:
            conn.execute('''
                UPDATE appointment_requests SET
                    status = ?,
                    updated_at = ?
                WHERE id = ? AND status = ?
            ''', (AppointmentStatus.COMPLETED.value, now, appt_id, AppointmentStatus.CONFIRMED.value))
            
            if conn.total_changes > 0:
                conn.execute('''
                    INSERT INTO audit_log (appointment_request_id, action, actor, timestamp)
                    VALUES (?, ?, ?, ?)
                ''', (appt_id, 'completed', actor, now))
                conn.commit()
                return True
            return False
        finally:
            conn.close()
    
    def expire_pending_appointments(self) -> int:
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()
        
        conn = self.get_connection()
        try:
            conn.execute('''
                UPDATE appointment_requests SET
                    status = ?,
                    expired_at = ?,
                    updated_at = ?
                WHERE status = ? AND expires_at < ?
            ''', (AppointmentStatus.EXPIRED.value, now_str, now_str,
                  AppointmentStatus.PENDING.value, now_str))
            
            count = conn.total_changes
            
            if count > 0:
                expired_ids = conn.execute('''
                    SELECT id FROM appointment_requests 
                    WHERE status = ? AND expired_at = ?
                ''', (AppointmentStatus.EXPIRED.value, now_str)).fetchall()
                
                for row in expired_ids:
                    conn.execute('''
                        INSERT INTO audit_log (appointment_request_id, action, actor, timestamp)
                        VALUES (?, ?, ?, ?)
                    ''', (row['id'], 'expired', 'system', now_str))
            
            conn.commit()
            return count
        finally:
            conn.close()
    
    def get_appointments_for_slot(self, slot_datetime_utc: str) -> List[Dict]:
        conn = self.get_connection()
        try:
            rows = conn.execute('''
                SELECT * FROM appointment_requests
                WHERE slot_datetime_utc = ? AND status IN (?, ?)
            ''', (slot_datetime_utc, AppointmentStatus.PENDING.value, AppointmentStatus.CONFIRMED.value)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    
    def get_appointments_by_date_range(self, start_date: str, end_date: str) -> List[Dict]:
        conn = self.get_connection()
        try:
            rows = conn.execute('''
                SELECT * FROM appointment_requests
                WHERE slot_date >= ? AND slot_date <= ?
                ORDER BY slot_datetime_utc
            ''', (start_date, end_date)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    
    def get_last_non_active_appointment_for_patient(self, patient_id: str) -> Optional[Dict]:
        """
        Most recent CANCELLED or EXPIRED appointment_requests row for this
        patient, if any - the closest proxy the current schema has for "a
        missed appointment", since there is no dedicated NO_SHOW status or
        appointment-reference field anywhere in this system (PatientRecord.
        no_show_history is a bare integer counter, not linked to any
        specific appointment).

        This is a heuristic, not a true no-show signal: a CANCELLED row can
        also result from a patient legitimately cancelling in advance, not
        only from missing an appointment. Callers using this for slot
        preference/ranking should treat it as "the patient's last known
        appointment session/time", not as a certainty that they no-showed.

        "Most recent" is determined by slot_datetime_utc (the appointment's
        own scheduled time), not created_at/updated_at - callers care about
        which appointment time to use as a preference signal, not which
        row was touched most recently.

        Returns:
            The full appointment_requests row (as a dict) with the latest
            slot_datetime_utc among CANCELLED/EXPIRED rows for this patient,
            or None if the patient has no such row.
        """
        conn = self.get_connection()
        try:
            row = conn.execute('''
                SELECT * FROM appointment_requests
                WHERE patient_id = ? AND status IN (?, ?)
                ORDER BY slot_datetime_utc DESC
                LIMIT 1
            ''', (patient_id, AppointmentStatus.CANCELLED.value,
                  AppointmentStatus.EXPIRED.value)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_overdue_confirmed_appointment_for_patient(
        self, patient_id: str, now_utc: str, grace_period_minutes: int
    ) -> Optional[Dict]:
        """
        The patient's most recent CONFIRMED appointment that is now past
        its no-show grace period - i.e. a REAL, current no-show: an
        appointment the clinic actually booked and confirmed, that the
        patient has still not attended by
        ``appointment_datetime + grace_period_minutes``, and that was
        never marked COMPLETED, CANCELLED, or rescheduled.

        This is a DATETIME comparison against the clinic's current
        instant (``now_utc``, from Clock.now() - never date.today() or a
        date-only comparison), per the required rule:
        ``now >= appointment_datetime + no_show_grace_period_minutes``.
        A 2:00 PM confirmed appointment with the default 30-minute grace
        period is NOT a no-show at 2:29 PM, and IS one at exactly 2:30 PM.

        This is intentionally distinct from two other things already in
        this system that are NOT reliable no-show signals:
          - PatientRecord.no_show_history (core/models.py) is a bare
            historical counter, unlinked to any specific appointment -
            it must never be used to decide TODAY's follow-up email
            content, only to weight urgency scoring over time.
          - get_last_non_active_appointment_for_patient (this class) looks
            at CANCELLED/EXPIRED rows as a heuristic for slot-ranking
            preference only (see its own docstring) - a cancelled booking
            is not necessarily a missed one.

        A CONFIRMED row past its grace period, by contrast, means the
        clinic held a slot for this patient and the patient's status was
        never updated away from CONFIRMED - the closest true signal this
        schema can express for "the patient did not attend."

        Args:
            patient_id: Patient to check.
            now_utc: The clinic's current instant, as an ISO 8601 UTC
                timestamp (matching slot_datetime_utc's own format).
            grace_period_minutes: Minutes of grace after the appointment's
                scheduled datetime before it counts as a no-show.

        Returns:
            The full appointment_requests row (as a dict) for the most
            recent such appointment, or None if the patient has none.
        """
        conn = self.get_connection()
        try:
            rows = conn.execute('''
                SELECT * FROM appointment_requests
                WHERE patient_id = ? AND status = ?
                ORDER BY slot_datetime_utc DESC
            ''', (patient_id, AppointmentStatus.CONFIRMED.value)).fetchall()

            now = datetime.fromisoformat(now_utc)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)

            for row in rows:
                appt_dt = datetime.fromisoformat(row['slot_datetime_utc'])
                if appt_dt.tzinfo is None:
                    appt_dt = appt_dt.replace(tzinfo=timezone.utc)
                grace_deadline = appt_dt + timedelta(minutes=grace_period_minutes)
                if now >= grace_deadline:
                    return dict(row)
            return None
        finally:
            conn.close()

    def mark_appointment_no_show(self, appt_id: int, actor: str = 'system') -> bool:
        """
        Transition a CONFIRMED appointment to NO_SHOW. This is the ONLY
        way an appointment's status becomes NO_SHOW, and it happens
        EXACTLY ONCE per appointment: the ``WHERE status = ?`` guard
        (matching the same idempotent pattern as complete_appointment/
        approve_appointment) means a second call against an
        already-NO_SHOW row is a silent no-op that returns False, so the
        same appointment can never repeatedly trigger duplicate no-show
        follow-ups from re-detecting "it's still overdue."

        Only a CONFIRMED row can transition here - COMPLETED, CANCELLED,
        DECLINED, EXPIRED, and already-NO_SHOW rows are never touched,
        satisfying "attended/rescheduled/cancelled states must never
        transition to NO_SHOW."

        Args:
            appt_id: The appointment_requests.id to transition.
            actor: Who/what triggered this - 'system' for the automatic
                orchestrator-driven detection, or a staff username for a
                manual override.

        Returns:
            True if this call performed the transition (i.e. the row was
            CONFIRMED); False if it was already something else (including
            already NO_SHOW) or does not exist.
        """
        now_str = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        try:
            conn.execute('''
                UPDATE appointment_requests SET
                    status = ?,
                    updated_at = ?
                WHERE id = ? AND status = ?
            ''', (AppointmentStatus.NO_SHOW.value, now_str, appt_id, AppointmentStatus.CONFIRMED.value))

            if conn.total_changes > 0:
                conn.execute('''
                    INSERT INTO audit_log (appointment_request_id, action, actor, timestamp)
                    VALUES (?, ?, ?, ?)
                ''', (appt_id, 'no_show', actor, now_str))
                conn.commit()
                return True
            return False
        finally:
            conn.close()

    def get_no_show_appointment_for_patient(self, patient_id: str) -> Optional[Dict]:
        """
        The patient's most recent NO_SHOW appointment, if any - used to
        confirm a transition has already been persisted (rather than
        re-deriving no-show status from an overdue CONFIRMED row forever).

        Returns:
            The full appointment_requests row (as a dict), or None.
        """
        conn = self.get_connection()
        try:
            row = conn.execute('''
                SELECT * FROM appointment_requests
                WHERE patient_id = ? AND status = ?
                ORDER BY slot_datetime_utc DESC
                LIMIT 1
            ''', (patient_id, AppointmentStatus.NO_SHOW.value)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def add_blocked_period(self, start: str, end: str, reason: str, created_by: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        try:
            cursor = conn.execute('''
                INSERT INTO blocked_periods (start_datetime, end_datetime, reason, created_by, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (start, end, reason, created_by, now))
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()
    
    def get_blocked_periods(self, start_date: str, end_date: str) -> List[Dict]:
        conn = self.get_connection()
        try:
            rows = conn.execute('''
                SELECT * FROM blocked_periods
                WHERE start_datetime <= ? AND end_datetime >= ?
                ORDER BY start_datetime
            ''', (end_date, start_date)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
