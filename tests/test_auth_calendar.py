"""
Comprehensive authentication and authorization tests for calendar routes.

Tests:
- Login with valid/invalid credentials
- Session management and expiration
- CSRF token validation
- Role-based access control (staff vs admin)
- Unauthorized access rejection (401, 403)
- Actual state changes with proper authorization
- Audit trail verification
"""

import pytest
import json
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from flask import Flask

from web.auth import AuthDatabase, generate_csrf_token
from web.calendar_routes import calendar_bp, init_calendar_routes
from scheduling.database import SchedulingDatabase, AppointmentStatus


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
    
    # Create test accounts
    db.create_staff_account('staff_user', 'password123', 'staff')
    db.create_staff_account('admin_user', 'admin_password', 'admin')
    
    yield db
    
    # No explicit cleanup needed - connections are closed after each operation


@pytest.fixture
def scheduling_db(temp_dir):
    """Create an isolated scheduling database for testing."""
    db_path = Path(temp_dir) / 'test_scheduling.db'
    db = SchedulingDatabase(str(db_path))
    
    # Create test appointment
    now = datetime.now(timezone.utc)
    db.create_appointment_request(
        patient_id='TEST001',
        patient_name='Test Patient',
        slot_date='2026-10-15',
        slot_session='morning',
        slot_time='09:00',
        slot_datetime_utc=(now + timedelta(days=21)).isoformat(),
        requested_by='agent',
        expires_at=(now + timedelta(hours=24)).isoformat()
    )
    
    yield db
    
    # No explicit cleanup needed - connections are closed after each operation


@pytest.fixture
def app(auth_db, scheduling_db):
    """Create Flask app with calendar routes for testing."""
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test-secret-key-do-not-use-in-production'
    app.config['AUTH_DB'] = auth_db  # Required for @require_auth decorator
    
    # Initialize calendar routes
    init_calendar_routes(auth_db, scheduling_db)
    app.register_blueprint(calendar_bp)
    
    return app


@pytest.fixture
def client(app):
    """Create Flask test client."""
    return app.test_client()


# ============================================================================
# Authentication Tests
# ============================================================================

def test_login_with_valid_credentials(client):
    """Test successful login with valid credentials."""
    response = client.post('/api/calendar/login', 
        json={'username': 'staff_user', 'password': 'password123'}
    )
    
    assert response.status_code == 200
    data = json.loads(response.data)
    
    assert data['success'] is True
    assert 'user' in data
    assert data['user']['username'] == 'staff_user'
    assert data['user']['role'] == 'staff'
    assert 'csrf_token' in data
    
    # Verify session cookie is set
    cookies = response.headers.getlist('Set-Cookie')
    assert any('session_id=' in cookie for cookie in cookies)


def test_login_with_invalid_credentials(client):
    """Test login fails with invalid credentials."""
    response = client.post('/api/calendar/login',
        json={'username': 'staff_user', 'password': 'wrong_password'}
    )
    
    assert response.status_code == 401
    data = json.loads(response.data)
    
    assert data['success'] is False
    assert 'error' in data
    
    # Verify no session cookie is set
    cookies = response.headers.getlist('Set-Cookie')
    assert not any('session_id=' in cookie and cookie.split('session_id=')[1].split(';')[0] for cookie in cookies)


def test_login_with_missing_fields(client):
    """Test login fails with missing fields."""
    response = client.post('/api/calendar/login',
        json={'username': 'staff_user'}
    )
    
    assert response.status_code == 400
    data = json.loads(response.data)
    assert data['success'] is False


def test_logout_clears_session(client):
    """Test logout clears session cookie."""
    # First login
    login_response = client.post('/api/calendar/login',
        json={'username': 'staff_user', 'password': 'password123'}
    )
    assert login_response.status_code == 200
    
    # Then logout
    logout_response = client.post('/api/calendar/logout')
    assert logout_response.status_code == 200
    
    data = json.loads(logout_response.data)
    assert data['success'] is True
    
    # Session cookie should be cleared (empty value)
    cookies = logout_response.headers.getlist('Set-Cookie')
    session_cookies = [c for c in cookies if 'session_id=' in c]
    if session_cookies:
        # Cookie value should be empty or expired
        assert any('session_id=;' in c or 'Expires=' in c for c in session_cookies)


def test_session_endpoint_requires_auth(client):
    """Test session endpoint requires authentication."""
    response = client.get('/api/calendar/session')
    
    assert response.status_code == 401
    data = json.loads(response.data)
    assert data['error'] == 'Unauthorized'


def test_session_endpoint_with_valid_session(client):
    """Test session endpoint returns user info when authenticated."""
    # Login first
    client.post('/api/calendar/login',
        json={'username': 'admin_user', 'password': 'admin_password'}
    )
    
    # Get session
    response = client.get('/api/calendar/session')
    
    assert response.status_code == 200
    data = json.loads(response.data)
    
    assert data['authenticated'] is True
    assert data['user']['username'] == 'admin_user'
    assert data['user']['role'] == 'admin'
    assert 'csrf_token' in data


# ============================================================================
# Authorization Tests
# ============================================================================

def test_unauthorized_access_returns_401(client):
    """Test accessing protected endpoints without authentication returns 401."""
    response = client.get('/api/calendar/availability?start_date=2026-10-01&end_date=2026-10-31')
    
    assert response.status_code == 401
    data = json.loads(response.data)
    assert data['error'] == 'Unauthorized'


def test_staff_can_access_staff_endpoints(client):
    """Test staff role can access staff-level endpoints."""
    # Login as staff
    client.post('/api/calendar/login',
        json={'username': 'staff_user', 'password': 'password123'}
    )
    
    # Access staff endpoint
    response = client.get('/api/calendar/availability?start_date=2026-10-01&end_date=2026-10-31')
    
    assert response.status_code == 200, response.get_json()
    data = json.loads(response.data)
    assert data['success'] is True


def test_admin_can_access_staff_endpoints(client):
    """Test admin role can access staff-level endpoints."""
    # Login as admin
    client.post('/api/calendar/login',
        json={'username': 'admin_user', 'password': 'admin_password'}
    )
    
    # Access staff endpoint
    response = client.get('/api/calendar/availability?start_date=2026-10-01&end_date=2026-10-31')
    
    assert response.status_code == 200, response.get_json()
    data = json.loads(response.data)
    assert data['success'] is True


def test_staff_cannot_access_admin_endpoints(client):
    """Test staff role cannot access admin-only endpoints."""
    # Login as staff
    login_response = client.post('/api/calendar/login',
        json={'username': 'staff_user', 'password': 'password123'}
    )
    csrf_token = json.loads(login_response.data)['csrf_token']
    
    # Try to access admin endpoint
    response = client.post('/api/calendar/blocked-periods',
        json={
            'start_datetime': '2026-10-15T09:00:00Z',
            'end_datetime': '2026-10-15T17:00:00Z',
            'reason': 'Test block'
        },
        headers={'X-CSRF-Token': csrf_token}
    )
    
    assert response.status_code == 403
    data = json.loads(response.data)
    assert data['error'] == 'Forbidden'
    assert 'admin' in data['message'].lower()


def test_admin_can_access_admin_endpoints(client):
    """Test admin role can access admin-only endpoints."""
    # Login as admin
    login_response = client.post('/api/calendar/login',
        json={'username': 'admin_user', 'password': 'admin_password'}
    )
    csrf_token = json.loads(login_response.data)['csrf_token']
    
    # Access admin endpoint
    response = client.post('/api/calendar/blocked-periods',
        json={
            'start_datetime': '2026-10-15T09:00:00Z',
            'end_datetime': '2026-10-15T17:00:00Z',
            'reason': 'Test block'
        },
        headers={'X-CSRF-Token': csrf_token}
    )
    
    assert response.status_code == 201
    data = json.loads(response.data)
    assert data['success'] is True


# ============================================================================
# CSRF Protection Tests
# ============================================================================

def test_state_changing_request_without_csrf_fails(client):
    """Test state-changing requests fail without CSRF token."""
    # Login first
    client.post('/api/calendar/login',
        json={'username': 'staff_user', 'password': 'password123'}
    )
    
    # Try to approve without CSRF token
    response = client.post('/api/calendar/appointments/1/approve')
    
    assert response.status_code == 403
    data = json.loads(response.data)
    assert data['error'] == 'Forbidden'
    assert 'CSRF' in data['message']


def test_state_changing_request_with_invalid_csrf_fails(client):
    """Test state-changing requests fail with invalid CSRF token."""
    # Login first
    client.post('/api/calendar/login',
        json={'username': 'staff_user', 'password': 'password123'}
    )
    
    # Try to approve with fake CSRF token
    response = client.post('/api/calendar/appointments/1/approve',
        headers={'X-CSRF-Token': 'fake-token-12345'}
    )
    
    assert response.status_code == 403
    data = json.loads(response.data)
    assert data['error'] == 'Forbidden'


def test_get_requests_do_not_require_csrf(client):
    """Test GET requests don't require CSRF token."""
    # Login first
    client.post('/api/calendar/login',
        json={'username': 'staff_user', 'password': 'password123'}
    )
    
    # GET requests should work without CSRF
    response = client.get('/api/calendar/appointments/pending')
    
    assert response.status_code == 200


# ============================================================================
# Appointment Action Tests (With Real State Changes)
# ============================================================================

def test_approve_appointment_with_valid_auth(client, scheduling_db):
    """Test approving an appointment actually changes its state."""
    # Login and get CSRF token
    login_response = client.post('/api/calendar/login',
        json={'username': 'staff_user', 'password': 'password123'}
    )
    csrf_token = json.loads(login_response.data)['csrf_token']
    
    # Verify appointment is pending
    appt_before = scheduling_db.get_appointment(1)
    assert appt_before is not None
    assert appt_before['status'] == 'pending'
    
    # Approve the appointment
    response = client.post('/api/calendar/appointments/1/approve',
        headers={'X-CSRF-Token': csrf_token}
    )
    
    assert response.status_code == 200
    data = json.loads(response.data)
    
    # Verify response
    assert data['success'] is True
    assert data['appointment']['status'] == 'confirmed'
    assert data['appointment']['approved_by'] == 'staff_user'
    assert data['appointment']['approved_at'] is not None
    
    # Verify actual database state changed
    appt_after = scheduling_db.get_appointment(1)
    assert appt_after['status'] == 'confirmed'
    assert appt_after['approved_by'] == 'staff_user'
    
    # Verify audit log entry created
    conn = scheduling_db.get_connection()
    try:
        audit_entries = conn.execute('''
            SELECT * FROM audit_log 
            WHERE appointment_request_id = 1 AND action = 'approved'
        ''').fetchall()
        
        assert len(audit_entries) > 0
        assert audit_entries[0]['actor'] == 'staff_user'
    finally:
        conn.close()


def test_decline_appointment_with_valid_auth(client, scheduling_db):
    """Test declining an appointment actually changes its state."""
    # Login and get CSRF token
    login_response = client.post('/api/calendar/login',
        json={'username': 'admin_user', 'password': 'admin_password'}
    )
    csrf_token = json.loads(login_response.data)['csrf_token']
    
    # Verify appointment is pending
    appt_before = scheduling_db.get_appointment(1)
    assert appt_before['status'] == 'pending'
    
    # Decline the appointment
    response = client.post('/api/calendar/appointments/1/decline',
        json={'reason': 'Slot no longer available'},
        headers={'X-CSRF-Token': csrf_token}
    )
    
    assert response.status_code == 200
    data = json.loads(response.data)
    
    # Verify response
    assert data['success'] is True
    assert data['appointment']['status'] == 'declined'
    assert data['appointment']['declined_reason'] == 'Slot no longer available'
    
    # Verify actual database state changed
    appt_after = scheduling_db.get_appointment(1)
    assert appt_after['status'] == 'declined'
    assert appt_after['declined_reason'] == 'Slot no longer available'


def test_cannot_approve_non_pending_appointment(client, scheduling_db):
    """Test cannot approve an appointment that's not pending."""
    # Login and get CSRF token
    login_response = client.post('/api/calendar/login',
        json={'username': 'staff_user', 'password': 'password123'}
    )
    csrf_token = json.loads(login_response.data)['csrf_token']
    
    # First approve it
    client.post('/api/calendar/appointments/1/approve',
        headers={'X-CSRF-Token': csrf_token}
    )
    
    # Get new CSRF token (old one was consumed)
    session_response = client.get('/api/calendar/session')
    csrf_token = json.loads(session_response.data)['csrf_token']
    
    # Try to approve again
    response = client.post('/api/calendar/appointments/1/approve',
        headers={'X-CSRF-Token': csrf_token}
    )
    
    assert response.status_code == 400
    data = json.loads(response.data)
    assert data['success'] is False
    assert 'Cannot approve' in data['error']


def test_approve_nonexistent_appointment_returns_404(client):
    """Test approving non-existent appointment returns 404."""
    # Login and get CSRF token
    login_response = client.post('/api/calendar/login',
        json={'username': 'staff_user', 'password': 'password123'}
    )
    csrf_token = json.loads(login_response.data)['csrf_token']
    
    # Try to approve non-existent appointment
    response = client.post('/api/calendar/appointments/999/approve',
        headers={'X-CSRF-Token': csrf_token}
    )
    
    assert response.status_code == 404
    data = json.loads(response.data)
    assert data['success'] is False
    assert 'not found' in data['error'].lower()


# ============================================================================
# Integration Tests
# ============================================================================

def test_full_approval_workflow(client, scheduling_db):
    """Test complete workflow: login → get pending → approve → verify audit."""
    # Step 1: Login
    login_response = client.post('/api/calendar/login',
        json={'username': 'staff_user', 'password': 'password123'}
    )
    assert login_response.status_code == 200
    csrf_token = json.loads(login_response.data)['csrf_token']
    
    # Step 2: Get pending appointments
    pending_response = client.get('/api/calendar/appointments/pending')
    assert pending_response.status_code == 200
    pending_data = json.loads(pending_response.data)
    assert pending_data['success'] is True
    assert pending_data['count'] > 0
    
    appointment_id = pending_data['appointments'][0]['id']
    
    # Step 3: Get appointment details
    detail_response = client.get(f'/api/calendar/appointments/{appointment_id}')
    assert detail_response.status_code == 200
    detail_data = json.loads(detail_response.data)
    assert detail_data['appointment']['status'] == 'pending'
    
    # Step 4: Approve appointment
    approve_response = client.post(f'/api/calendar/appointments/{appointment_id}/approve',
        headers={'X-CSRF-Token': csrf_token}
    )
    assert approve_response.status_code == 200
    approve_data = json.loads(approve_response.data)
    assert approve_data['success'] is True
    assert approve_data['appointment']['status'] == 'confirmed'
    
    # Step 5: Verify audit log
    audit_response = client.get(f'/api/calendar/audit?appointment_id={appointment_id}')
    assert audit_response.status_code == 200
    audit_data = json.loads(audit_response.data)
    
    # Should have 'created' and 'approved' entries
    actions = [log['action'] for log in audit_data['logs']]
    assert 'created' in actions
    assert 'approved' in actions
    
    # Verify actor
    approved_log = [log for log in audit_data['logs'] if log['action'] == 'approved'][0]
    assert approved_log['actor'] == 'staff_user'


def test_csrf_token_is_single_use(client):
    """Test CSRF tokens are consumed after one use."""
    # Login and get CSRF token
    login_response = client.post('/api/calendar/login',
        json={'username': 'admin_user', 'password': 'admin_password'}
    )
    csrf_token = json.loads(login_response.data)['csrf_token']
    
    # Use the token once
    response1 = client.post('/api/calendar/blocked-periods',
        json={
            'start_datetime': '2026-10-15T09:00:00Z',
            'end_datetime': '2026-10-15T17:00:00Z',
            'reason': 'Test block 1'
        },
        headers={'X-CSRF-Token': csrf_token}
    )
    assert response1.status_code == 201
    
    # Try to use the same token again
    response2 = client.post('/api/calendar/blocked-periods',
        json={
            'start_datetime': '2026-10-16T09:00:00Z',
            'end_datetime': '2026-10-16T17:00:00Z',
            'reason': 'Test block 2'
        },
        headers={'X-CSRF-Token': csrf_token}
    )
    assert response2.status_code == 403


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
