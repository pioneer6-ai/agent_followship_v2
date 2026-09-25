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
