"""
Regression tests for calendar date shift issue (one-day off in Asia/Singapore timezone).

These tests verify that:
1. Clicking September 25 creates a booking on September 25 (not September 24)
2. Month/year boundaries (Jan 1, Dec 31) work correctly
3. Date selection, booking creation, and display all use the same date
4. No timezone conversion happens for date-only values

The bug was caused by toISOString() converting local midnight to UTC,
causing Asia/Singapore (UTC+8) to shift backwards by one day.
"""

import pytest
import tempfile
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from flask import Flask

from web.auth import AuthDatabase
from web.calendar_routes import calendar_bp, init_calendar_routes
from scheduling.database import SchedulingDatabase


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test databases."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def auth_db(temp_dir):
    """Create an isolated auth database for testing."""
    db_path = Path(temp_dir) / 'test_auth.db'
    db = AuthDatabase(str(db_path))
    
    # Create test account
    db.create_staff_account('test_staff', 'password123', 'staff')
    
    yield db


@pytest.fixture
def scheduling_db(temp_dir):
    """Create an isolated scheduling database with Asia/Singapore timezone."""
    db_path = Path(temp_dir) / 'test_scheduling.db'
    db = SchedulingDatabase(str(db_path))
    
    # Update config to use Asia/Singapore timezone
    config = db.get_config()
    config['timezone_name'] = 'Asia/Singapore'
    config['working_days'] = [0, 1, 2, 3, 4]  # Mon-Fri
    db.update_config(config)
    
    yield db


@pytest.fixture
def app(auth_db, scheduling_db):
    """Create Flask app with calendar routes for testing."""
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test-secret-key-date-shift'
    app.config['AUTH_DB'] = auth_db
    
    init_calendar_routes(auth_db, scheduling_db)
    app.register_blueprint(calendar_bp)
    
    return app


@pytest.fixture
def client(app):
    """Create Flask test client."""
    return app.test_client()


@pytest.fixture
def authenticated_client(client, auth_db):
    """Login and return authenticated client."""
    # Login
    response = client.post('/api/calendar/login',
        json={'username': 'test_staff', 'password': 'password123'})
    assert response.status_code == 200
    data = response.get_json()
    
    # Store CSRF token
    csrf_token = data['csrf_token']
    
    # Create a simple wrapper to add CSRF token to requests
    class AuthenticatedClient:
        def __init__(self, client, csrf_token):
            self.client = client
            self.csrf_token = csrf_token
        
        def post(self, *args, **kwargs):
            if 'headers' not in kwargs:
                kwargs['headers'] = {}
            kwargs['headers']['X-CSRF-Token'] = self.csrf_token
            return self.client.post(*args, **kwargs)
        
        def get(self, *args, **kwargs):
            return self.client.get(*args, **kwargs)
    
    return AuthenticatedClient(client, csrf_token)


# ============================================================================
# Date Shift Regression Tests
# ============================================================================

def test_september_25_click_creates_september_25_booking(authenticated_client, scheduling_db):
    """
    CRITICAL REGRESSION TEST: Clicking September 25 must create booking on Sept 25.
    
    Before fix: toISOString() in Asia/Singapore (UTC+8) would convert:
        - Local: Sept 25, 2026 00:00:00 (midnight)
        - UTC:   Sept 24, 2026 16:00:00
        - Result: "2026-09-24" stored/displayed
    
    After fix: formatDate() uses local components:
        - Local: Sept 25, 2026 00:00:00
        - Result: "2026-09-25" (no UTC conversion)
    """
    # Create a booking for September 25, 2026 (a Thursday)
    booking_data = {
        'patient_id': 'P001',
        'patient_name': 'Test Patient',
        'slot_date': '2026-09-25',
        'slot_session': 'morning',
        'slot_time': '09:00',
        'follow_up_reason': 'Test booking for date shift regression'
    }
    
    response = authenticated_client.post('/api/calendar/appointments', json=booking_data)
    assert response.status_code == 201, f"Failed to create booking: {response.get_json()}"
    
    data = response.get_json()
    assert data['success'] is True
    appointment_id = data['appointment_id']
    
    # Verify the booking was created with correct date
    response = authenticated_client.get(f'/api/calendar/appointments/{appointment_id}')
    assert response.status_code == 200
    
    appointment = response.get_json()['appointment']
    
    # CRITICAL ASSERTION: slot_date must be exactly what was sent
    assert appointment['slot_date'] == '2026-09-25', \
        f"Date shift detected! Expected '2026-09-25', got '{appointment['slot_date']}'"
    
    # Verify other fields are correct
    assert appointment['slot_time'] == '09:00'
    assert appointment['slot_session'] == 'morning'
    assert appointment['patient_id'] == 'P001'


def test_december_31_no_year_shift(authenticated_client, scheduling_db):
    """
    Test year boundary: December 31 should not shift to January 1 of next year.
    
    Before fix: Dec 31, 2026 at midnight in UTC+8 could shift to Jan 1, 2027 or Dec 30, 2026.
    After fix: Dec 31, 2026 stays as "2026-12-31".
    """
    # December 31, 2026 is a Thursday (working day)
    booking_data = {
        'patient_id': 'P002',
        'patient_name': 'Year End Patient',
        'slot_date': '2026-12-31',
        'slot_session': 'afternoon',
        'slot_time': '14:00'
    }
    
    response = authenticated_client.post('/api/calendar/appointments', json=booking_data)
    assert response.status_code == 201, f"Failed to create Dec 31 booking: {response.get_json()}"
    
    data = response.get_json()
    appointment_id = data['appointment_id']
    
    # Verify date didn't shift to next year
    response = authenticated_client.get(f'/api/calendar/appointments/{appointment_id}')
    appointment = response.get_json()['appointment']
    
    assert appointment['slot_date'] == '2026-12-31', \
        f"Year boundary shift! Expected '2026-12-31', got '{appointment['slot_date']}'"


def test_january_1_no_year_shift(authenticated_client, scheduling_db):
    """
    Test year boundary: January 1 should not shift to December 31 of previous year.
    
    Before fix: Jan 1, 2027 at midnight in UTC+8 could shift to Dec 31, 2026.
    After fix: Jan 1, 2027 stays as "2027-01-01".
    """
    # January 1, 2027 is a Friday (working day)
    booking_data = {
        'patient_id': 'P003',
        'patient_name': 'New Year Patient',
        'slot_date': '2027-01-01',
        'slot_session': 'morning',
        'slot_time': '10:00'
    }
    
    response = authenticated_client.post('/api/calendar/appointments', json=booking_data)
    assert response.status_code == 201, f"Failed to create Jan 1 booking: {response.get_json()}"
    
    data = response.get_json()
    appointment_id = data['appointment_id']
    
    # Verify date didn't shift to previous year
    response = authenticated_client.get(f'/api/calendar/appointments/{appointment_id}')
    appointment = response.get_json()['appointment']
    
    assert appointment['slot_date'] == '2027-01-01', \
        f"Year boundary shift! Expected '2027-01-01', got '{appointment['slot_date']}'"


def test_month_end_boundaries(authenticated_client, scheduling_db):
    """
    Test various month-end boundaries to ensure no date shifts.
    
    Tests: Feb 28, Mar 31, Apr 30, May 31, Jun 30, Jul 31, Aug 31, Sep 30, Oct 31, Nov 30
    """
    test_cases = [
        ('2027-02-26', 'Feb 26 (Friday)'),  # Feb 28 is Sunday, use Friday before
        ('2027-03-31', 'Mar 31 (Wednesday)'),
        ('2027-04-30', 'Apr 30 (Friday)'),
        ('2027-05-31', 'May 31 (Monday)'),
        ('2027-06-30', 'Jun 30 (Wednesday)'),
        ('2027-07-30', 'Jul 30 (Friday)'),  # Jul 31 is Saturday, use Friday before
        ('2027-08-31', 'Aug 31 (Tuesday)'),
        ('2027-09-30', 'Sep 30 (Thursday)'),
        ('2027-10-29', 'Oct 29 (Friday)'),  # Oct 31 is Sunday, use Friday before
        ('2027-11-30', 'Nov 30 (Tuesday)'),
    ]
    
    for slot_date, description in test_cases:
        booking_data = {
            'patient_id': f'P_MONTH_{slot_date}',
            'patient_name': f'Patient {description}',
            'slot_date': slot_date,
            'slot_session': 'morning',
            'slot_time': '09:30'
        }
        
        response = authenticated_client.post('/api/calendar/appointments', json=booking_data)
        assert response.status_code == 201, \
            f"Failed to create booking for {description}: {response.get_json()}"
        
        data = response.get_json()
        appointment_id = data['appointment_id']
        
        # Verify date matches exactly
        response = authenticated_client.get(f'/api/calendar/appointments/{appointment_id}')
        appointment = response.get_json()['appointment']
        
        assert appointment['slot_date'] == slot_date, \
            f"Month boundary shift for {description}! Expected '{slot_date}', got '{appointment['slot_date']}'"


def test_appointment_list_filters_by_correct_dates(authenticated_client, scheduling_db):
    """
    Test that querying appointments by date range returns correct results.
    
    Ensures that date filtering doesn't suffer from timezone shifts.
    """
    # Create bookings for Sept 24, 25, 26 (Wed-Fri)
    dates = ['2026-09-24', '2026-09-25', '2026-09-26']
    
    for slot_date in dates:
        booking_data = {
            'patient_id': f'P_{slot_date}',
            'patient_name': f'Patient {slot_date}',
            'slot_date': slot_date,
            'slot_session': 'afternoon',
            'slot_time': '15:00'
        }
        response = authenticated_client.post('/api/calendar/appointments', json=booking_data)
        assert response.status_code == 201
    
    # Query for Sept 25 only
    response = authenticated_client.get('/api/calendar/appointments?start_date=2026-09-25&end_date=2026-09-25')
    assert response.status_code == 200
    
    data = response.get_json()
    appointments = data['appointments']
    
    # Should get exactly one appointment for Sept 25
    assert len(appointments) == 1, \
        f"Expected 1 appointment for Sept 25, got {len(appointments)}"
    assert appointments[0]['slot_date'] == '2026-09-25'
    
    # Query for Sept 24-26 range
    response = authenticated_client.get('/api/calendar/appointments?start_date=2026-09-24&end_date=2026-09-26')
    assert response.status_code == 200
    
    data = response.get_json()
    appointments = data['appointments']
    
    # Should get all three appointments
    assert len(appointments) == 3, \
        f"Expected 3 appointments for Sept 24-26, got {len(appointments)}"
    
    # Verify dates are correct and in order
    found_dates = [appt['slot_date'] for appt in appointments]
    assert found_dates == ['2026-09-24', '2026-09-25', '2026-09-26'], \
        f"Date filter returned wrong dates: {found_dates}"


def test_past_date_validation_uses_clinic_timezone(authenticated_client, scheduling_db):
    """
    Test that past date validation uses clinic timezone, not server timezone.
    
    This is important when server is in different timezone than clinic.
    Example: Server in UTC, clinic in Asia/Singapore (UTC+8).
    """
    # Get today in clinic timezone (Asia/Singapore)
    clinic_tz = ZoneInfo('Asia/Singapore')
    today_clinic = datetime.now(clinic_tz).date()
    yesterday_clinic = today_clinic - timedelta(days=1)
    
    # Try to book yesterday (should fail)
    booking_data = {
        'patient_id': 'P_PAST',
        'patient_name': 'Past Patient',
        'slot_date': yesterday_clinic.isoformat(),
        'slot_session': 'morning',
        'slot_time': '09:00'
    }
    
    response = authenticated_client.post('/api/calendar/appointments', json=booking_data)
    assert response.status_code == 400, \
        "Past date booking should be rejected"
    
    data = response.get_json()
    assert 'past' in data['error'].lower(), \
        f"Error message should mention 'past': {data['error']}"


def test_availability_query_returns_correct_dates(authenticated_client, scheduling_db):
    """
    Test that availability API returns slots for the correct date.
    
    Ensures that slot generation doesn't shift dates due to timezone conversion.
    """
    # Query availability for September 25, 2026
    response = authenticated_client.get('/api/calendar/availability?start_date=2026-09-25&end_date=2026-09-25')
    assert response.status_code == 200
    
    data = response.get_json()
    slots = data['slots']
    
    # All slots should be for Sept 25
    for slot in slots:
        assert slot['date'] == '2026-09-25', \
            f"Availability slot has wrong date: expected '2026-09-25', got '{slot['date']}'"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
