"""
Pytest tests for calendar authentication, authorization, and CSRF.
Uses normal login flow without manual cookie injection.
"""

import pytest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from flask import Flask

from web.auth import AuthDatabase
from web.calendar_routes import calendar_bp, init_calendar_routes
from scheduling.database import SchedulingDatabase


@pytest.fixture
def test_dir(tmp_path):
    """Create temporary directory for test databases."""
    return tmp_path


@pytest.fixture
def auth_db(test_dir):
    """Create auth database with test accounts."""
    db = AuthDatabase(str(test_dir / 'auth.db'))
    db.create_staff_account('staff1', 'password123', 'staff')
    db.create_staff_account('admin1', 'adminpass123', 'admin')
    yield db


@pytest.fixture
def sched_db(test_dir):
    """Create scheduling database with test appointments."""
    db = SchedulingDatabase(str(test_dir / 'sched.db'))
    now = datetime.now(timezone.utc)
    
    # Create test appointments
    appt1 = db.create_appointment_request(
        patient_id='P001', patient_name='Patient One',
        slot_date='2026-10-15', slot_session='morning', slot_time='09:00',
        slot_datetime_utc=(now + timedelta(days=21)).isoformat(),
        requested_by='agent', expires_at=(now + timedelta(hours=24)).isoformat()
    )
    appt2 = db.create_appointment_request(
        patient_id='P002', patient_name='Patient Two',
        slot_date='2026-10-16', slot_session='afternoon', slot_time='14:00',
        slot_datetime_utc=(now + timedelta(days=22)).isoformat(),
        requested_by='agent', expires_at=(now + timedelta(hours=24)).isoformat()
    )
    
    db._test_appointments = {'appt1': appt1, 'appt2': appt2}
    yield db


@pytest.fixture
def app(auth_db, sched_db):
    """Create Flask app with test configuration."""
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test-key-123'
    app.config['AUTH_DB'] = auth_db
    
    init_calendar_routes(auth_db, sched_db)
    app.register_blueprint(calendar_bp)
    
    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return app.test_client()


class TestAuthentication:
    """Test authentication flows."""
    
    def test_login_success(self, client):
        """Test successful login."""
        resp = client.post('/api/calendar/login', json={
            'username': 'staff1',
            'password': 'password123'
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['user']['username'] == 'staff1'
        assert data['user']['role'] == 'staff'
        assert 'csrf_token' in data
        assert 'Set-Cookie' in resp.headers
    
    def test_login_invalid_credentials(self, client):
        """Test login with invalid credentials."""
        resp = client.post('/api/calendar/login', json={
            'username': 'staff1',
            'password': 'wrongpassword'
        })
        assert resp.status_code == 401
        data = resp.get_json()
        assert data['success'] is False
    
    def test_session_persistence(self, client):
        """Test session persists across requests."""
        # Login
        resp = client.post('/api/calendar/login', json={
            'username': 'staff1',
            'password': 'password123'
        })
        assert resp.status_code == 200
        
        # Check session (cookie should be sent automatically)
        resp = client.get('/api/calendar/session')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['authenticated'] is True
        assert data['user']['username'] == 'staff1'
    
    def test_logout(self, client):
        """Test logout invalidates session."""
        # Login
        resp = client.post('/api/calendar/login', json={
            'username': 'staff1',
            'password': 'password123'
        })
        assert resp.status_code == 200
        
        # Logout
        resp = client.post('/api/calendar/logout')
        assert resp.status_code == 200
        
        # Session should be invalid
        resp = client.get('/api/calendar/session')
        assert resp.status_code == 401
    
    def test_expired_session_rejection(self, client, auth_db):
        """Test expired sessions are rejected."""
        # Login
        resp = client.post('/api/calendar/login', json={
            'username': 'staff1',
            'password': 'password123'
        })
        assert resp.status_code == 200
        
        # Manually expire all sessions
        import sqlite3
        conn = sqlite3.connect(auth_db.db_path)
        conn.execute("UPDATE sessions SET expires_at = datetime('now', '-1 hour')")
        conn.commit()
        conn.close()
        
        # Session should be rejected
        resp = client.get('/api/calendar/session')
        assert resp.status_code == 401


class TestCSRFProtection:
    """Test CSRF token validation."""
    
    def test_missing_csrf_rejected(self, client, sched_db):
        """Test request without CSRF token is rejected."""
        # Login
        client.post('/api/calendar/login', json={
            'username': 'staff1',
            'password': 'password123'
        })
        
        # Try to approve without CSRF token
        appt_id = sched_db._test_appointments['appt1']
        resp = client.post(f'/api/calendar/appointments/{appt_id}/approve')
        assert resp.status_code == 403
        data = resp.get_json()
        assert 'CSRF' in data['message']
    
    def test_invalid_csrf_rejected(self, client, sched_db):
        """Test request with invalid CSRF token is rejected."""
        # Login
        client.post('/api/calendar/login', json={
            'username': 'staff1',
            'password': 'password123'
        })
        
        # Try with invalid token
        appt_id = sched_db._test_appointments['appt1']
        resp = client.post(
            f'/api/calendar/appointments/{appt_id}/approve',
            headers={'X-CSRF-Token': 'invalid-token-123'}
        )
        assert resp.status_code == 403
    
    def test_valid_csrf_accepted(self, client, sched_db):
        """Test request with valid CSRF token is accepted."""
        # Login
        client.post('/api/calendar/login', json={
            'username': 'staff1',
            'password': 'password123'
        })
        
        # Get CSRF token
        resp = client.get('/api/calendar/session')
        csrf_token = resp.get_json()['csrf_token']
        
        # Use valid token
        appt_id = sched_db._test_appointments['appt1']
        resp = client.post(
            f'/api/calendar/appointments/{appt_id}/approve',
            headers={'X-CSRF-Token': csrf_token}
        )
        assert resp.status_code == 200
    
    def test_multiple_csrf_actions(self, client, sched_db):
        """Test multiple consecutive CSRF-protected actions."""
        # Login
        client.post('/api/calendar/login', json={
            'username': 'staff1',
            'password': 'password123'
        })
        
        # First action: approve
        resp = client.get('/api/calendar/session')
        csrf1 = resp.get_json()['csrf_token']
        
        appt1 = sched_db._test_appointments['appt1']
        resp = client.post(
            f'/api/calendar/appointments/{appt1}/approve',
            headers={'X-CSRF-Token': csrf1}
        )
        assert resp.status_code == 200
        
        # Second action: decline (need new CSRF token)
        resp = client.get('/api/calendar/session')
        csrf2 = resp.get_json()['csrf_token']
        
        appt2 = sched_db._test_appointments['appt2']
        resp = client.post(
            f'/api/calendar/appointments/{appt2}/decline',
            json={'reason': 'Test decline'},
            headers={'X-CSRF-Token': csrf2}
        )
        assert resp.status_code == 200


class TestRoleBasedAuthorization:
    """Test role-based access control."""
    
    def test_staff_can_approve(self, client, sched_db):
        """Test staff can approve appointments."""
        client.post('/api/calendar/login', json={
            'username': 'staff1',
            'password': 'password123'
        })
        
        resp = client.get('/api/calendar/session')
        csrf = resp.get_json()['csrf_token']
        
        appt_id = sched_db._test_appointments['appt1']
        resp = client.post(
            f'/api/calendar/appointments/{appt_id}/approve',
            headers={'X-CSRF-Token': csrf}
        )
        assert resp.status_code == 200
    
    def test_staff_blocked_from_admin_endpoint(self, client):
        """Test staff cannot access admin-only endpoints."""
        client.post('/api/calendar/login', json={
            'username': 'staff1',
            'password': 'password123'
        })
        
        resp = client.get('/api/calendar/session')
        csrf = resp.get_json()['csrf_token']
        
        # Try to create blocked period (admin only)
        resp = client.post('/api/calendar/blocked-periods', json={
            'start_datetime': '2026-10-15T09:00:00Z',
            'end_datetime': '2026-10-15T17:00:00Z',
            'reason': 'Test'
        }, headers={'X-CSRF-Token': csrf})
        
        assert resp.status_code == 403
        data = resp.get_json()
        assert 'admin' in data['message'].lower()
    
    def test_admin_can_access_admin_endpoint(self, client):
        """Test admin can access admin-only endpoints."""
        client.post('/api/calendar/login', json={
            'username': 'admin1',
            'password': 'adminpass123'
        })
        
        resp = client.get('/api/calendar/session')
        csrf = resp.get_json()['csrf_token']
        
        resp = client.post('/api/calendar/blocked-periods', json={
            'start_datetime': '2026-10-15T09:00:00Z',
            'end_datetime': '2026-10-15T17:00:00Z',
            'reason': 'Test'
        }, headers={'X-CSRF-Token': csrf})
        
        assert resp.status_code == 201  # Created, not 200


class TestDatabaseStateChanges:
    """Test actual database state changes and audit logging."""
    
    def test_approval_updates_database(self, client, sched_db):
        """Test approval updates appointment status and creates audit entry."""
        client.post('/api/calendar/login', json={
            'username': 'staff1',
            'password': 'password123'
        })
        
        resp = client.get('/api/calendar/session')
        csrf = resp.get_json()['csrf_token']
        
        appt_id = sched_db._test_appointments['appt1']
        resp = client.post(
            f'/api/calendar/appointments/{appt_id}/approve',
            headers={'X-CSRF-Token': csrf}
        )
        assert resp.status_code == 200
        
        # Check database state
        appt = sched_db.get_appointment(appt_id)
        assert appt['status'] == 'confirmed'
        assert appt['approved_by'] == 'staff1'
        
        # Check audit log
        conn = sched_db.get_connection()
        audit = conn.execute(
            'SELECT * FROM audit_log WHERE appointment_request_id=? AND action=?',
            (appt_id, 'approved')
        ).fetchone()
        conn.close()
        
        assert audit is not None
        assert audit['actor'] == 'staff1'
    
    def test_decline_updates_database(self, client, sched_db):
        """Test decline updates appointment status."""
        client.post('/api/calendar/login', json={
            'username': 'staff1',
            'password': 'password123'
        })
        
        resp = client.get('/api/calendar/session')
        csrf = resp.get_json()['csrf_token']
        
        appt_id = sched_db._test_appointments['appt2']
        resp = client.post(
            f'/api/calendar/appointments/{appt_id}/decline',
            json={'reason': 'Test decline'},
            headers={'X-CSRF-Token': csrf}
        )
        assert resp.status_code == 200
        
        # Check database state
        appt = sched_db.get_appointment(appt_id)
        assert appt['status'] == 'declined'
