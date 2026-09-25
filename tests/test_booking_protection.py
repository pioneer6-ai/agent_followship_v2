"""
Tests for double-booking protection and atomic capacity enforcement.

Tests cover:
- Concurrent bookings for the final slot
- Capacity limits with multiple slots
- Duplicate submission prevention
- Expired hold handling on approval
- Blocked slot validation
- Safe rescheduling with transactional safety
"""

import pytest
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from scheduling.database import SchedulingDatabase, AppointmentStatus
from scheduling.calendar_service import CalendarService


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary database for testing."""
    db_path = tmp_path / "test_booking.db"
    db = SchedulingDatabase(str(db_path))
    
    # Configure for testing: capacity of 2 slots, 5 minute expiry
    config = db.get_config()
    config['slots_per_session'] = 2
    config['pending_expiry_minutes'] = 5
    config['working_days'] = [0, 1, 2, 3, 4]  # Mon-Fri
    config['sessions'] = {
        'morning': {'start': '09:00', 'end': '12:00'},
        'afternoon': {'start': '13:00', 'end': '17:00'}
    }
    config['slot_duration_minutes'] = 30
    db.update_config(config)
    
    yield db
    
    # Cleanup
    if Path(db_path).exists():
        Path(db_path).unlink()


@pytest.fixture
def service(temp_db):
    """Create calendar service with test database."""
    return CalendarService(temp_db)


def test_concurrent_booking_for_final_slot(service, temp_db):
    """Test two concurrent requests trying to book the final available slot."""
    config = temp_db.get_config()
    assert config['slots_per_session'] == 2, "Test requires capacity of 2"
    
    # Create a slot timestamp
    slot_date = '2026-10-15'
    slot_time = '09:00'
    slot_session = 'morning'
    
    # Book the first slot normally
    result1 = service.check_capacity_and_book(
        patient_id='P001',
        patient_name='First Patient',
        slot_datetime_utc='',  # Will be normalized
        slot_date=slot_date,
        slot_session=slot_session,
        slot_time=slot_time,
        requested_by='staff1'
    )
    assert result1['success'], f"First booking should succeed: {result1}"
    
    # Now try two concurrent requests for the final slot
    results = []
    errors = []
    
    def book_slot(patient_id, patient_name):
        try:
            result = service.check_capacity_and_book(
                patient_id=patient_id,
                patient_name=patient_name,
                slot_datetime_utc='',
                slot_date=slot_date,
                slot_session=slot_session,
                slot_time=slot_time,
                requested_by='staff2'
            )
            results.append((patient_id, result))
        except Exception as e:
            errors.append((patient_id, str(e)))
    
    # Launch concurrent threads
    thread1 = threading.Thread(target=book_slot, args=('P002', 'Second Patient'))
    thread2 = threading.Thread(target=book_slot, args=('P003', 'Third Patient'))
    
    thread1.start()
    thread2.start()
    
    thread1.join()
    thread2.join()
    
    assert len(errors) == 0, f"No exceptions should occur: {errors}"
    assert len(results) == 2, "Both threads should return results"
    
    # Exactly one should succeed, one should fail
    successes = [r for r in results if r[1]['success']]
    failures = [r for r in results if not r[1]['success']]
    
    assert len(successes) == 1, f"Exactly one booking should succeed, got {len(successes)}"
    assert len(failures) == 1, f"Exactly one booking should fail, got {len(failures)}"
    assert 'full' in failures[0][1]['error'].lower(), "Failure should indicate slot is full"
    
    # Verify database state
    appointments = temp_db.get_appointments_for_slot(result1['slot_datetime_utc'])
    active = [a for a in appointments if a['status'] in (AppointmentStatus.PENDING.value, AppointmentStatus.CONFIRMED.value)]
    assert len(active) == 2, f"Should have exactly 2 active bookings, got {len(active)}"


def test_capacity_greater_than_one(service, temp_db):
    """Test capacity enforcement with multiple slots available."""
    config = temp_db.get_config()
    capacity = config['slots_per_session']
    
    slot_date = '2026-10-16'
    slot_time = '10:00'
    slot_session = 'morning'
    
    # Book up to capacity
    for i in range(capacity):
        result = service.check_capacity_and_book(
            patient_id=f'P{i:03d}',
            patient_name=f'Patient {i}',
            slot_datetime_utc='',
            slot_date=slot_date,
            slot_session=slot_session,
            slot_time=slot_time,
            requested_by='staff'
        )
        assert result['success'], f"Booking {i+1}/{capacity} should succeed"
    
    # Next booking should fail
    result_over = service.check_capacity_and_book(
        patient_id='P999',
        patient_name='Overflow Patient',
        slot_datetime_utc='',
        slot_date=slot_date,
        slot_session=slot_session,
        slot_time=slot_time,
        requested_by='staff'
    )
    assert not result_over['success'], "Booking over capacity should fail"
    assert 'full' in result_over['error'].lower()


def test_duplicate_submission_prevention(service, temp_db):
    """Test that duplicate active requests from same patient for same slot are rejected."""
    slot_date = '2026-10-15'  # Thursday
    slot_time = '14:00'
    slot_session = 'afternoon'
    
    # First booking
    result1 = service.check_capacity_and_book(
        patient_id='P001',
        patient_name='John Doe',
        slot_datetime_utc='',
        slot_date=slot_date,
        slot_session=slot_session,
        slot_time=slot_time,
        requested_by='staff'
    )
    assert result1['success'], "First booking should succeed"
    
    # Duplicate booking (same patient, same slot)
    result2 = service.check_capacity_and_book(
        patient_id='P001',
        patient_name='John Doe',
        slot_datetime_utc='',
        slot_date=slot_date,
        slot_session=slot_session,
        slot_time=slot_time,
        requested_by='staff'
    )
    assert not result2['success'], "Duplicate booking should fail"
    assert 'duplicate' in result2['error'].lower(), f"Error should mention duplicate: {result2['error']}"


def test_equivalent_timestamp_normalization(service, temp_db):
    """Test that equivalent timestamps are normalized to the same UTC value."""
    slot_date = '2026-10-16'  # Friday
    slot_time = '09:30'
    slot_session = 'morning'
    
    # First booking
    result1 = service.check_capacity_and_book(
        patient_id='P001',
        patient_name='Patient One',
        slot_datetime_utc='',  # Will be normalized
        slot_date=slot_date,
        slot_session=slot_session,
        slot_time=slot_time,
        requested_by='staff'
    )
    assert result1['success']
    
    # Get the normalized timestamp
    normalized_utc = result1['slot_datetime_utc']
    
    # Second booking with same slot - should be recognized as duplicate for same patient
    result2 = service.check_capacity_and_book(
        patient_id='P001',
        patient_name='Patient One',
        slot_datetime_utc='different-value',  # Ignored, will be normalized
        slot_date=slot_date,
        slot_session=slot_session,
        slot_time=slot_time,
        requested_by='staff'
    )
    assert not result2['success']
    assert 'duplicate' in result2['error'].lower()
    
    # Different patient should work (capacity allows)
    result3 = service.check_capacity_and_book(
        patient_id='P002',
        patient_name='Patient Two',
        slot_datetime_utc='',
        slot_date=slot_date,
        slot_session=slot_session,
        slot_time=slot_time,
        requested_by='staff'
    )
    assert result3['success']
    assert result3['slot_datetime_utc'] == normalized_utc, "Should normalize to same UTC time"


def test_expired_hold_rejected_on_approval(service, temp_db):
    """Test that expired holds are rejected when approval is attempted."""
    slot_date = '2026-10-19'
    slot_time = '15:00'
    slot_session = 'afternoon'
    
    # Create booking
    result = service.check_capacity_and_book(
        patient_id='P001',
        patient_name='Test Patient',
        slot_datetime_utc='',
        slot_date=slot_date,
        slot_session=slot_session,
        slot_time=slot_time,
        requested_by='staff'
    )
    assert result['success']
    appt_id = result['appointment_id']
    
    # Manually expire the hold by setting expires_at to the past
    conn = temp_db.get_connection()
    try:
        past_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        conn.execute('''
            UPDATE appointment_requests SET expires_at = ? WHERE id = ?
        ''', (past_time, appt_id))
        conn.commit()
    finally:
        conn.close()
    
    # Attempt to approve
    approval_result = service.approve_with_validation(appt_id, 'admin')
    
    assert not approval_result['success'], "Approval of expired hold should fail"
    assert 'expired' in approval_result['error'].lower(), f"Error should mention expiry: {approval_result['error']}"
    
    # Verify status changed to expired
    appt = temp_db.get_appointment(appt_id)
    assert appt['status'] == AppointmentStatus.EXPIRED.value


def test_approval_rechecks_capacity(service, temp_db):
    """Test that approval rechecks capacity before confirming."""
    config = temp_db.get_config()
    assert config['slots_per_session'] == 2
    
    slot_date = '2026-10-20'
    slot_time = '11:00'
    slot_session = 'morning'
    
    # Create two pending bookings
    result1 = service.check_capacity_and_book(
        patient_id='P001',
        patient_name='Patient One',
        slot_datetime_utc='',
        slot_date=slot_date,
        slot_session=slot_session,
        slot_time=slot_time,
        requested_by='staff'
    )
    assert result1['success']
    appt_id1 = result1['appointment_id']
    
    result2 = service.check_capacity_and_book(
        patient_id='P002',
        patient_name='Patient Two',
        slot_datetime_utc='',
        slot_date=slot_date,
        slot_session=slot_session,
        slot_time=slot_time,
        requested_by='staff'
    )
    assert result2['success']
    appt_id2 = result2['appointment_id']
    
    # Approve first one
    approval1 = service.approve_with_validation(appt_id1, 'admin')
    assert approval1['success']
    
    # Approve second one should also succeed (capacity is 2)
    approval2 = service.approve_with_validation(appt_id2, 'admin')
    assert approval2['success']
    
    # Try to create and approve a third booking
    result3 = service.check_capacity_and_book(
        patient_id='P003',
        patient_name='Patient Three',
        slot_datetime_utc='',
        slot_date=slot_date,
        slot_session=slot_session,
        slot_time=slot_time,
        requested_by='staff'
    )
    assert not result3['success'], "Third booking should fail (capacity full)"


def test_blocked_slot_rejection(service, temp_db):
    """Test that bookings for blocked slots are rejected."""
    slot_date = '2026-10-21'
    slot_time = '09:00'
    slot_session = 'morning'
    
    # Normalize the slot time to get UTC
    config = temp_db.get_config()
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(config['timezone_name'])
    date_obj = datetime.strptime(slot_date, '%Y-%m-%d').date()
    time_obj = datetime.strptime(slot_time, '%H:%M').time()
    dt_local = datetime.combine(date_obj, time_obj).replace(tzinfo=tz)
    dt_utc = dt_local.astimezone(ZoneInfo('UTC'))
    
    # Block the slot
    block_start = dt_utc.isoformat()
    block_end = (dt_utc + timedelta(hours=1)).isoformat()
    temp_db.add_blocked_period(block_start, block_end, 'Testing', 'admin')
    
    # Try to book
    result = service.check_capacity_and_book(
        patient_id='P001',
        patient_name='Test Patient',
        slot_datetime_utc='',
        slot_date=slot_date,
        slot_session=slot_session,
        slot_time=slot_time,
        requested_by='staff'
    )
    
    assert not result['success'], "Booking blocked slot should fail"
    assert 'blocked' in result['error'].lower(), f"Error should mention blocked: {result['error']}"


def test_invalid_slot_configuration(service, temp_db):
    """Test that slots not matching clinic configuration are rejected."""
    # Try to book on a weekend (not a working day)
    result1 = service.check_capacity_and_book(
        patient_id='P001',
        patient_name='Test Patient',
        slot_datetime_utc='',
        slot_date='2026-10-17',  # Saturday
        slot_session='morning',
        slot_time='09:00',
        requested_by='staff'
    )
    assert not result1['success']
    assert 'working day' in result1['error'].lower()
    
    # Try to book outside session hours
    result2 = service.check_capacity_and_book(
        patient_id='P001',
        patient_name='Test Patient',
        slot_datetime_utc='',
        slot_date='2026-10-15',  # Thursday
        slot_session='morning',
        slot_time='08:00',  # Before 09:00 start
        requested_by='staff'
    )
    assert not result2['success']
    assert 'outside session' in result2['error'].lower()
    
    # Try to book with non-aligned time slot
    result3 = service.check_capacity_and_book(
        patient_id='P001',
        patient_name='Test Patient',
        slot_datetime_utc='',
        slot_date='2026-10-15',
        slot_session='morning',
        slot_time='09:15',  # Not aligned with 30-minute slots
        requested_by='staff'
    )
    assert not result3['success']
    assert 'align' in result3['error'].lower()


def test_safe_rescheduling_success(service, temp_db):
    """Test successful rescheduling preserves original until new slot confirmed."""
    # Create initial booking
    result = service.check_capacity_and_book(
        patient_id='P001',
        patient_name='Test Patient',
        slot_datetime_utc='',
        slot_date='2026-10-22',
        slot_session='morning',
        slot_time='09:00',
        requested_by='staff'
    )
    assert result['success']
    original_id = result['appointment_id']
    
    # Approve it
    approval = service.approve_with_validation(original_id, 'admin')
    assert approval['success']
    
    # Reschedule to different slot
    reschedule_result = service.reschedule_appointment(
        appt_id=original_id,
        new_slot_date='2026-10-23',
        new_slot_session='afternoon',
        new_slot_time='14:00',
        actor='admin'
    )
    
    assert reschedule_result['success'], f"Rescheduling should succeed: {reschedule_result}"
    assert 'new_appointment_id' in reschedule_result
    assert 'cancelled_appointment_id' in reschedule_result
    
    # Verify original is cancelled
    original = temp_db.get_appointment(original_id)
    assert original['status'] == AppointmentStatus.CANCELLED.value
    
    # Verify new appointment exists and is pending
    new_id = reschedule_result['new_appointment_id']
    new_appt = temp_db.get_appointment(new_id)
    assert new_appt is not None
    assert new_appt['status'] == AppointmentStatus.PENDING.value
    assert new_appt['patient_id'] == 'P001'


def test_failed_rescheduling_preserves_original(service, temp_db):
    """Test that failed rescheduling doesn't lose the original booking."""
    # Create and approve original booking
    result = service.check_capacity_and_book(
        patient_id='P001',
        patient_name='Test Patient',
        slot_datetime_utc='',
        slot_date='2026-10-22',  # Thursday
        slot_session='morning',
        slot_time='09:00',
        requested_by='staff'
    )
    assert result['success']
    original_id = result['appointment_id']
    
    approval = service.approve_with_validation(original_id, 'admin')
    assert approval['success']
    
    # Try to reschedule to invalid slot (weekend)
    reschedule_result = service.reschedule_appointment(
        appt_id=original_id,
        new_slot_date='2026-10-24',  # Saturday - invalid
        new_slot_session='morning',
        new_slot_time='09:00',
        actor='admin'
    )
    
    assert not reschedule_result['success'], "Rescheduling to invalid slot should fail"
    
    # Verify original is still confirmed
    original = temp_db.get_appointment(original_id)
    assert original['status'] == AppointmentStatus.CONFIRMED.value, "Original should remain confirmed"


def test_rescheduling_to_full_slot_fails(service, temp_db):
    """Test that rescheduling to a full slot fails and preserves original."""
    config = temp_db.get_config()
    capacity = config['slots_per_session']
    
    # Create and approve original booking
    result = service.check_capacity_and_book(
        patient_id='P001',
        patient_name='Patient One',
        slot_datetime_utc='',
        slot_date='2026-10-23',  # Friday
        slot_session='morning',
        slot_time='09:00',
        requested_by='staff'
    )
    assert result['success']
    original_id = result['appointment_id']
    
    approval = service.approve_with_validation(original_id, 'admin')
    assert approval['success']
    
    # Fill up target slot completely
    target_date = '2026-10-26'  # Monday
    target_time = '10:00'
    target_session = 'morning'
    
    for i in range(capacity):
        filler_result = service.check_capacity_and_book(
            patient_id=f'P{i+10:03d}',
            patient_name=f'Filler {i}',
            slot_datetime_utc='',
            slot_date=target_date,
            slot_session=target_session,
            slot_time=target_time,
            requested_by='staff'
        )
        assert filler_result['success']
    
    # Try to reschedule to full slot
    reschedule_result = service.reschedule_appointment(
        appt_id=original_id,
        new_slot_date=target_date,
        new_slot_session=target_session,
        new_slot_time=target_time,
        actor='admin'
    )
    
    assert not reschedule_result['success'], "Rescheduling to full slot should fail"
    assert 'full' in reschedule_result['error'].lower()
    
    # Verify original is still confirmed
    original = temp_db.get_appointment(original_id)
    assert original['status'] == AppointmentStatus.CONFIRMED.value


def test_declined_and_cancelled_dont_consume_capacity(service, temp_db):
    """Test that declined and cancelled appointments don't count against capacity."""
    config = temp_db.get_config()
    capacity = config['slots_per_session']
    
    slot_date = '2026-10-27'
    slot_time = '11:00'
    slot_session = 'morning'
    
    # Create and decline a booking
    result1 = service.check_capacity_and_book(
        patient_id='P001',
        patient_name='Patient One',
        slot_datetime_utc='',
        slot_date=slot_date,
        slot_session=slot_session,
        slot_time=slot_time,
        requested_by='staff'
    )
    assert result1['success']
    temp_db.decline_appointment(result1['appointment_id'], 'admin', 'Test decline')
    
    # Create and cancel a booking
    result2 = service.check_capacity_and_book(
        patient_id='P002',
        patient_name='Patient Two',
        slot_datetime_utc='',
        slot_date=slot_date,
        slot_session=slot_session,
        slot_time=slot_time,
        requested_by='staff'
    )
    assert result2['success']
    temp_db.cancel_appointment(result2['appointment_id'], 'admin', 'Test cancel')
    
    # Should still be able to book up to full capacity
    for i in range(capacity):
        result = service.check_capacity_and_book(
            patient_id=f'P{i+10:03d}',
            patient_name=f'Active Patient {i}',
            slot_datetime_utc='',
            slot_date=slot_date,
            slot_session=slot_session,
            slot_time=slot_time,
            requested_by='staff'
        )
        assert result['success'], f"Booking {i+1}/{capacity} should succeed (declined/cancelled don't count)"


def test_expired_holds_dont_block_new_bookings(service, temp_db):
    """Test that expired holds release capacity for new bookings."""
    slot_date = '2026-10-28'
    slot_time = '14:00'
    slot_session = 'afternoon'
    
    # Create a pending booking
    result = service.check_capacity_and_book(
        patient_id='P001',
        patient_name='Test Patient',
        slot_datetime_utc='',
        slot_date=slot_date,
        slot_session=slot_session,
        slot_time=slot_time,
        requested_by='staff'
    )
    assert result['success']
    appt_id = result['appointment_id']
    
    # Manually expire it
    conn = temp_db.get_connection()
    try:
        past_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        now_str = datetime.now(timezone.utc).isoformat()
        conn.execute('''
            UPDATE appointment_requests SET 
                status = ?,
                expires_at = ?,
                expired_at = ?,
                updated_at = ?
            WHERE id = ?
        ''', (AppointmentStatus.EXPIRED.value, past_time, now_str, now_str, appt_id))
        conn.commit()
    finally:
        conn.close()
    
    # Should be able to book the same slot with different patient
    result2 = service.check_capacity_and_book(
        patient_id='P002',
        patient_name='New Patient',
        slot_datetime_utc='',
        slot_date=slot_date,
        slot_session=slot_session,
        slot_time=slot_time,
        requested_by='staff'
    )
    assert result2['success'], "Should be able to book after hold expired"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
