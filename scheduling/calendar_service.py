from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional, Tuple
from scheduling.database import SchedulingDatabase, AppointmentStatus
import sqlite3


class CalendarService:
    def __init__(self, db: SchedulingDatabase):
        self.db = db
    
    def normalize_slot_timestamp(self, date_str: str, time_str: str, tz_name: str) -> Tuple[str, bool]:
        """
        Normalize a slot timestamp to UTC and validate it aligns with clinic sessions.
        
        Returns:
            (normalized_utc_timestamp, is_valid)
        """
        try:
            tz = ZoneInfo(tz_name)
            date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
            time_obj = datetime.strptime(time_str, '%H:%M').time()
            
            # Combine and localize
            dt_local = datetime.combine(date_obj, time_obj)
            dt_local = dt_local.replace(tzinfo=tz)
            
            # Convert to UTC
            dt_utc = dt_local.astimezone(ZoneInfo('UTC'))
            
            return dt_utc.isoformat(), True
        except Exception:
            return '', False
    
    def validate_slot_against_config(self, date_str: str, session_name: str, 
                                    time_str: str, config: Dict) -> Tuple[bool, str]:
        """
        Validate that a slot matches clinic configuration.
        
        Returns:
            (is_valid, error_message)
        """
        try:
            date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
            
            # Check working day
            if date_obj.weekday() not in config['working_days']:
                return False, f"Date {date_str} is not a working day"
            
            # Check session exists
            if session_name not in config['sessions']:
                return False, f"Session '{session_name}' does not exist"
            
            session = config['sessions'][session_name]
            time_obj = datetime.strptime(time_str, '%H:%M').time()
            start_time = datetime.strptime(session['start'], '%H:%M').time()
            end_time = datetime.strptime(session['end'], '%H:%M').time()
            
            # Check time is within session bounds
            if not (start_time <= time_obj < end_time):
                return False, f"Time {time_str} is outside session {session_name} hours"
            
            # Check time aligns with slot duration
            session_start = datetime.combine(date_obj, start_time)
            slot_time = datetime.combine(date_obj, time_obj)
            minutes_diff = int((slot_time - session_start).total_seconds() / 60)
            
            if minutes_diff % config['slot_duration_minutes'] != 0:
                return False, f"Time {time_str} does not align with {config['slot_duration_minutes']}-minute slots"
            
            return True, ""
        except Exception as e:
            return False, f"Validation error: {str(e)}"
    
    def is_slot_blocked(self, slot_datetime_utc: str, conn: sqlite3.Connection) -> bool:
        """Check if a slot falls within any blocked period."""
        row = conn.execute('''
            SELECT COUNT(*) as count FROM blocked_periods
            WHERE start_datetime <= ? AND end_datetime >= ?
        ''', (slot_datetime_utc, slot_datetime_utc)).fetchone()
        
        return row['count'] > 0 if row else False
    
    def generate_slots_for_date(self, date_str: str) -> List[Dict]:
        config = self.db.get_config()
        tz = ZoneInfo(config['timezone_name'])
        date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
        
        # Check if working day
        if date_obj.weekday() not in config['working_days']:
            return []
        
        slots = []
        for session_name, session_times in config['sessions'].items():
            start_time = datetime.strptime(session_times['start'], '%H:%M').time()
            end_time = datetime.strptime(session_times['end'], '%H:%M').time()
            
            current = datetime.combine(date_obj, start_time)
            end = datetime.combine(date_obj, end_time)
            
            while current < end:
                slot_time_local = current.replace(tzinfo=tz)
                slot_time_utc = slot_time_local.astimezone(ZoneInfo('UTC'))
                
                slots.append({
                    'date': date_str,
                    'session': session_name,
                    'time': current.strftime('%H:%M'),
                    'datetime_utc': slot_time_utc.isoformat(),
                    'datetime_local': slot_time_local.isoformat(),
                    'capacity': config['slots_per_session']
                })
                
                current += timedelta(minutes=config['slot_duration_minutes'])
        
        return slots
    
    def get_availability(self, start_date: str, end_date: str) -> List[Dict]:
        config = self.db.get_config()
        appointments = self.db.get_appointments_by_date_range(start_date, end_date)
        blocked = self.db.get_blocked_periods(
            start_date + 'T00:00:00Z',
            end_date + 'T23:59:59Z'
        )
        
        # Generate all slots
        current = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
        all_slots = []
        
        while current <= end:
            date_str = current.strftime('%Y-%m-%d')
            day_slots = self.generate_slots_for_date(date_str)
            all_slots.extend(day_slots)
            current += timedelta(days=1)
        
        # Count bookings per slot
        booking_counts = {}
        for appt in appointments:
            if appt['status'] in (AppointmentStatus.PENDING.value, AppointmentStatus.CONFIRMED.value):
                slot_key = appt['slot_datetime_utc']
                booking_counts[slot_key] = booking_counts.get(slot_key, 0) + 1
        
        # Mark blocked slots
        blocked_times = set()
        for block in blocked:
            blocked_times.add((block['start_datetime'], block['end_datetime']))
        
        # Add availability info
        for slot in all_slots:
            slot_utc = slot['datetime_utc']
            slot['booked'] = booking_counts.get(slot_utc, 0)
            slot['available'] = slot['capacity'] - slot['booked']
            slot['is_blocked'] = any(
                start <= slot_utc <= end for start, end in blocked_times
            )
            slot['status'] = 'blocked' if slot['is_blocked'] else (
                'full' if slot['available'] <= 0 else 'available'
            )
        
        return all_slots
    
    def check_capacity_and_book(self, patient_id: str, patient_name: str,
                                slot_datetime_utc: str, slot_date: str,
                                slot_session: str, slot_time: str,
                                requested_by: str, follow_up_case_id: Optional[str] = None,
                                follow_up_reason: Optional[str] = None,
                                source: str = 'staff') -> Dict:
        """
        Atomically check capacity and create booking with comprehensive validation.
        
        Protections:
        - Validates slot against clinic configuration
        - Checks for blocked periods
        - Prevents duplicate active requests from same patient for same slot
        - Enforces capacity limits atomically
        - Normalizes timestamps

        Args:
            source: Who/what initiated this booking - 'staff' (default,
                via the staff calendar) or 'patient_portal' (via
                core.scheduling_calendar_adapter.SchedulingDatabaseCalendarAdapter).
                Recorded on the appointment row for reporting/audit only;
                does not change validation behavior.

        Returns:
            {'success': bool, 'appointment_id': int, 'expires_at': str} on success
            {'success': False, 'error': str} on failure
        """
        config = self.db.get_config()
        
        # Validate slot configuration
        is_valid, error = self.validate_slot_against_config(slot_date, slot_session, slot_time, config)
        if not is_valid:
            return {'success': False, 'error': error}
        
        # Normalize timestamp
        normalized_utc, is_valid = self.normalize_slot_timestamp(slot_date, slot_time, config['timezone_name'])
        if not is_valid:
            return {'success': False, 'error': 'Invalid timestamp'}
        
        # Use normalized timestamp (ignore provided one if different)
        slot_datetime_utc = normalized_utc
        
        # Atomic check and book within transaction
        conn = self.db.get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            
            # Check if slot is blocked
            if self.is_slot_blocked(slot_datetime_utc, conn):
                conn.rollback()
                return {'success': False, 'error': 'Slot is blocked'}
            
            # Check for duplicate active request from same patient for same slot
            row = conn.execute('''
                SELECT id FROM appointment_requests
                WHERE patient_id = ? AND slot_datetime_utc = ? 
                AND status IN (?, ?)
            ''', (patient_id, slot_datetime_utc, AppointmentStatus.PENDING.value,
                  AppointmentStatus.CONFIRMED.value)).fetchone()
            
            if row:
                conn.rollback()
                return {'success': False, 'error': 'Duplicate request: patient already has an active request for this slot'}
            
            # Count current bookings (pending + confirmed)
            row = conn.execute('''
                SELECT COUNT(*) as count FROM appointment_requests
                WHERE slot_datetime_utc = ? AND status IN (?, ?)
            ''', (slot_datetime_utc, AppointmentStatus.PENDING.value,
                  AppointmentStatus.CONFIRMED.value)).fetchone()
            
            current_bookings = row['count'] if row else 0
            
            if current_bookings >= config['slots_per_session']:
                conn.rollback()
                return {'success': False, 'error': 'Slot is full'}
            
            # Calculate expiry with timezone awareness
            now_utc = datetime.now(timezone.utc)
            expires_at = now_utc + timedelta(minutes=config['pending_expiry_minutes'])
            
            # Create booking
            now_str = now_utc.isoformat()
            cursor = conn.execute('''
                INSERT INTO appointment_requests
                (patient_id, patient_name, follow_up_case_id, slot_date, slot_session,
                 slot_time, slot_datetime_utc, status, follow_up_reason, requested_by,
                 expires_at, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (patient_id, patient_name, follow_up_case_id, slot_date, slot_session,
                  slot_time, slot_datetime_utc, AppointmentStatus.PENDING.value,
                  follow_up_reason, requested_by, expires_at.isoformat(), source, now_str, now_str))
            
            appt_id = cursor.lastrowid
            
            # Audit
            conn.execute('''
                INSERT INTO audit_log (appointment_request_id, action, actor, timestamp)
                VALUES (?, ?, ?, ?)
            ''', (appt_id, 'created', requested_by, now_str))
            
            conn.commit()
            return {
                'success': True, 
                'appointment_id': appt_id, 
                'expires_at': expires_at.isoformat(),
                'slot_datetime_utc': slot_datetime_utc
            }
            
        except Exception as e:
            conn.rollback()
            return {'success': False, 'error': f'Booking failed: {str(e)}'}
        finally:
            conn.close()

    
    def complete_appointment(
        self,
        appt_id: int,
        actor: str,
        notes: str = "",
    ) -> Dict:
        """Complete a confirmed appointment through the database state guard."""
        appointment = self.db.get_appointment(appt_id)
        if not appointment:
            return {'success': False, 'error': 'Appointment not found'}
        if appointment['status'] != AppointmentStatus.CONFIRMED.value:
            return {
                'success': False,
                'error': f"Cannot complete appointment with status: {appointment['status']}",
            }

        completed = self.db.complete_appointment(
            appt_id,
            actor,
            details=notes or 'Appointment marked as completed',
        )
        if not completed:
            return {
                'success': False,
                'error': 'Appointment could not be completed; it may have changed state.',
            }
        return {'success': True}

    def approve_with_validation(self, appt_id: int, actor: str) -> Dict:
        """
        Approve a pending appointment with validation.
        
        Checks:
        - Appointment exists and is pending
        - Hold has not expired
        - Capacity is still available
        - Slot is not blocked
        
        Returns:
            {'success': bool, 'error': str (optional)}
        """
        conn = self.db.get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            
            # Get appointment
            row = conn.execute('''
                SELECT * FROM appointment_requests WHERE id = ?
            ''', (appt_id,)).fetchone()
            
            if not row:
                conn.rollback()
                return {'success': False, 'error': 'Appointment not found'}
            
            appt = dict(row)
            
            if appt['status'] != AppointmentStatus.PENDING.value:
                conn.rollback()
                return {'success': False, 'error': f"Cannot approve appointment with status: {appt['status']}"}
            
            # Check if hold expired
            now_utc = datetime.now(timezone.utc)
            if appt['expires_at']:
                expires_at = datetime.fromisoformat(appt['expires_at'])
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                
                if now_utc >= expires_at:
                    # Mark as expired
                    now_str = now_utc.isoformat()
                    conn.execute('''
                        UPDATE appointment_requests SET
                            status = ?,
                            expired_at = ?,
                            updated_at = ?
                        WHERE id = ?
                    ''', (AppointmentStatus.EXPIRED.value, now_str, now_str, appt_id))
                    
                    conn.execute('''
                        INSERT INTO audit_log (appointment_request_id, action, actor, timestamp)
                        VALUES (?, ?, ?, ?)
                    ''', (appt_id, 'expired', 'system', now_str))
                    
                    conn.commit()
                    return {'success': False, 'error': 'Hold has expired'}
            
            # Check slot is not blocked
            if self.is_slot_blocked(appt['slot_datetime_utc'], conn):
                conn.rollback()
                return {'success': False, 'error': 'Slot is now blocked'}
            
            # Recheck capacity (another booking might have been confirmed)
            config = self.db.get_config()
            row = conn.execute('''
                SELECT COUNT(*) as count FROM appointment_requests
                WHERE slot_datetime_utc = ? AND status IN (?, ?) AND id != ?
            ''', (appt['slot_datetime_utc'], AppointmentStatus.PENDING.value,
                  AppointmentStatus.CONFIRMED.value, appt_id)).fetchone()
            
            current_bookings = row['count'] if row else 0
            
            # This appointment will become confirmed, so check if adding it exceeds capacity
            if current_bookings >= config['slots_per_session']:
                conn.rollback()
                return {'success': False, 'error': 'Slot is now full'}
            
            # Approve
            now_str = now_utc.isoformat()
            conn.execute('''
                UPDATE appointment_requests SET
                    status = ?,
                    approved_by = ?,
                    approved_at = ?,
                    expires_at = NULL,
                    updated_at = ?
                WHERE id = ?
            ''', (AppointmentStatus.CONFIRMED.value, actor, now_str, now_str, appt_id))
            
            conn.execute('''
                INSERT INTO audit_log (appointment_request_id, action, actor, timestamp)
                VALUES (?, ?, ?, ?)
            ''', (appt_id, 'approved', actor, now_str))
            
            conn.commit()
            return {'success': True}
            
        except Exception as e:
            conn.rollback()
            return {'success': False, 'error': f'Approval failed: {str(e)}'}
        finally:
            conn.close()
    
    def reschedule_appointment(self, appt_id: int, new_slot_date: str, 
                              new_slot_session: str, new_slot_time: str,
                              actor: str) -> Dict:
        """
        Safely reschedule an appointment.
        
        Creates new pending request for new slot, then cancels original only if
        new booking succeeds. This prevents losing the booking if rescheduling fails.
        
        Returns:
            {'success': bool, 'new_appointment_id': int (optional), 'error': str (optional)}
        """
        conn = self.db.get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            
            # Get original appointment
            row = conn.execute('''
                SELECT * FROM appointment_requests WHERE id = ?
            ''', (appt_id,)).fetchone()
            
            if not row:
                conn.rollback()
                return {'success': False, 'error': 'Appointment not found'}
            
            original = dict(row)
            
            if original['status'] not in (AppointmentStatus.PENDING.value, AppointmentStatus.CONFIRMED.value):
                conn.rollback()
                return {'success': False, 'error': f"Cannot reschedule appointment with status: {original['status']}"}
            
            conn.commit()  # Release lock before trying new booking
            
        except Exception as e:
            conn.rollback()
            return {'success': False, 'error': f'Failed to validate original appointment: {str(e)}'}
        finally:
            conn.close()
        
        # Try to book new slot (this uses its own transaction)
        config = self.db.get_config()
        normalized_utc, is_valid = self.normalize_slot_timestamp(new_slot_date, new_slot_time, config['timezone_name'])
        if not is_valid:
            return {'success': False, 'error': 'Invalid new slot timestamp'}
        
        new_booking_result = self.check_capacity_and_book(
            patient_id=original['patient_id'],
            patient_name=original['patient_name'],
            slot_datetime_utc=normalized_utc,
            slot_date=new_slot_date,
            slot_session=new_slot_session,
            slot_time=new_slot_time,
            requested_by=actor,
            follow_up_case_id=original['follow_up_case_id'],
            follow_up_reason=f"Rescheduled from {original['slot_date']} {original['slot_time']}"
        )
        
        if not new_booking_result['success']:
            return {'success': False, 'error': f"Failed to book new slot: {new_booking_result['error']}"}
        
        # New booking succeeded, now cancel original
        conn = self.db.get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            
            now_str = datetime.now(timezone.utc).isoformat()
            conn.execute('''
                UPDATE appointment_requests SET
                    status = ?,
                    declined_reason = ?,
                    updated_at = ?
                WHERE id = ?
            ''', (AppointmentStatus.CANCELLED.value, f"Rescheduled to appointment #{new_booking_result['appointment_id']}", now_str, appt_id))
            
            conn.execute('''
                INSERT INTO audit_log (appointment_request_id, action, actor, details, timestamp)
                VALUES (?, ?, ?, ?, ?)
            ''', (appt_id, 'rescheduled', actor, f"New appointment: {new_booking_result['appointment_id']}", now_str))
            
            conn.commit()
            
            return {
                'success': True,
                'new_appointment_id': new_booking_result['appointment_id'],
                'cancelled_appointment_id': appt_id
            }
            
        except Exception as e:
            conn.rollback()
            # New booking was created but we couldn't cancel old one - log this
            # In production, this would need manual intervention
            return {
                'success': False,
                'error': f'New booking created but failed to cancel original: {str(e)}',
                'new_appointment_id': new_booking_result['appointment_id'],
                'requires_manual_cleanup': True
            }
        finally:
            conn.close()
