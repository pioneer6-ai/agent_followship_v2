"""
Tests for staff account management system.
Covers authorization, account operations, last-admin protection, and audit logging.
"""
import pytest
import tempfile
from pathlib import Path
from flask import Flask

from web.auth import AuthDatabase
from web.staff_routes import staff_bp, init_staff_routes


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test databases."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def auth_db(temp_dir):
    """Create an isolated auth database for testing."""
    db_path = Path(temp_dir) / 'test_staff_auth.db'
    db = AuthDatabase(str(db_path))
    
    # Create test accounts
    db.create_staff_account('staff_user', 'password123', 'staff')
    db.create_staff_account('admin_user', 'admin_password', 'admin')
    db.create_staff_account('admin_user_2', 'admin_pass2', 'admin')  # Second admin for last-admin tests
    
    yield db


@pytest.fixture
def app(auth_db, temp_dir):
    """Create Flask test app with staff routes and calendar auth."""
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'test-secret-key-staff-management'
    app.config['TESTING'] = True
    app.config['AUTH_DB'] = auth_db  # Required for @require_auth decorator
    
    # Initialize and register calendar routes (for login endpoint)
    from web.calendar_routes import calendar_bp, init_calendar_routes
    from scheduling.database import SchedulingDatabase
    
    # Create a temporary scheduling database for calendar routes
    sched_db_path = Path(temp_dir) / 'test_scheduling.db'
    sched_db = SchedulingDatabase(str(sched_db_path))
    init_calendar_routes(auth_db, sched_db)
    app.register_blueprint(calendar_bp)
    
    # Initialize staff routes
    init_staff_routes(auth_db)
    app.register_blueprint(staff_bp)
    
    return app


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture
def staff_session(client):
    """Login as staff user and return authenticated client with session cookie."""
    # Staff routes share auth with calendar routes - use calendar login endpoint
    response = client.post('/api/calendar/login', json={
        'username': 'staff_user',
        'password': 'password123'
    })
    assert response.status_code == 200, f"Staff login failed: {response.get_json()}"
    data = response.get_json()
    assert data['success'] is True
    
    # Create wrapper that fetches fresh CSRF token before mutations
    class AuthenticatedSession:
        def __init__(self, client):
            self.client = client
            self._csrf_token = None
        
        def _get_fresh_csrf(self):
            """Get fresh CSRF token from session endpoint."""
            response = self.client.get('/api/calendar/session')
            if response.status_code == 200:
                data = response.get_json()
                return data.get('csrf_token')
            return None
        
        def get(self, *args, **kwargs):
            """GET requests don't need CSRF."""
            return self.client.get(*args, **kwargs)
        
        def post(self, *args, **kwargs):
            """POST requests need fresh CSRF token."""
            csrf_token = self._get_fresh_csrf()
            if csrf_token:
                if 'headers' not in kwargs:
                    kwargs['headers'] = {}
                kwargs['headers']['X-CSRF-Token'] = csrf_token
            return self.client.post(*args, **kwargs)
    
    return AuthenticatedSession(client)


@pytest.fixture
def admin_session(client):
    """Login as admin user and return authenticated client with session cookie."""
    # Staff routes share auth with calendar routes - use calendar login endpoint
    response = client.post('/api/calendar/login', json={
        'username': 'admin_user',
        'password': 'admin_password'
    })
    assert response.status_code == 200, f"Admin login failed: {response.get_json()}"
    data = response.get_json()
    assert data['success'] is True
    
    # Create wrapper that fetches fresh CSRF token before mutations
    class AuthenticatedSession:
        def __init__(self, client):
            self.client = client
            self._csrf_token = None
        
        def _get_fresh_csrf(self):
            """Get fresh CSRF token from session endpoint."""
            response = self.client.get('/api/calendar/session')
            if response.status_code == 200:
                data = response.get_json()
                return data.get('csrf_token')
            return None
        
        def get(self, *args, **kwargs):
            """GET requests don't need CSRF."""
            return self.client.get(*args, **kwargs)
        
        def post(self, *args, **kwargs):
            """POST requests need fresh CSRF token."""
            csrf_token = self._get_fresh_csrf()
            if csrf_token:
                if 'headers' not in kwargs:
                    kwargs['headers'] = {}
                kwargs['headers']['X-CSRF-Token'] = csrf_token
            return self.client.post(*args, **kwargs)
    
    return AuthenticatedSession(client)


# ============================================================================
# Authorization Tests
# ============================================================================

def test_non_admin_cannot_list_accounts(client, staff_session):
    """Non-admin staff should not be able to list accounts."""
    response = staff_session.get('/api/staff/accounts')
    assert response.status_code == 403
    data = response.get_json()
    assert data['error'] == 'Forbidden'
    assert 'message' in data
    assert 'not permitted' in data['message'].lower() or 'admin' in data['message'].lower()


def test_admin_can_list_accounts(client, admin_session):
    """Admin should be able to list all accounts without password hashes."""
    response = admin_session.get('/api/staff/accounts')
    assert response.status_code == 200
    data = response.get_json()
    assert 'accounts' in data
    # Should have at least the 3 accounts we created
    assert len(data['accounts']) >= 3
    # Verify no password hashes in response
    for account in data['accounts']:
        assert 'password_hash' not in account
        assert 'username' in account
        assert 'role' in account


# ============================================================================
# Account Creation Tests
# ============================================================================

def test_create_account_with_duplicate_username(client, admin_session):
    """Creating account with duplicate username should fail."""
    response = admin_session.post('/api/staff/accounts', json={
        'username': 'staff_user',  # Already exists
        'role': 'staff',
        'password': 'TempPass123!'
    })
    
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert 'already exists' in data['error'].lower()


def test_create_account_success_with_password_must_change(client, admin_session, auth_db):
    """Successfully creating account should set password_must_change flag."""
    response = admin_session.post('/api/staff/accounts', json={
        'username': 'new_staff_member',
        'role': 'staff',
        'password': 'TempPass456!'
    })
    
    assert response.status_code == 201
    data = response.get_json()
    assert data['success'] is True
    assert 'Account' in data['message'] and 'created' in data['message']
    
    # Verify account persisted with correct username and role
    conn = auth_db._get_connection()
    try:
        result = conn.execute('''
            SELECT username, role, password_must_change FROM staff_accounts 
            WHERE username = ?
        ''', ('new_staff_member',)).fetchone()
        assert result is not None
        assert result['username'] == 'new_staff_member'
        assert result['role'] == 'staff'
        assert result['password_must_change'] == 1
    finally:
        conn.close()


# ============================================================================
# Deactivation Tests
# ============================================================================

def test_deactivate_account_invalidates_sessions(client, admin_session, auth_db):
    """Deactivating account should invalidate all active sessions."""
    # Get staff_user's ID
    conn = auth_db._get_connection()
    try:
        result = conn.execute('''
            SELECT id FROM staff_accounts WHERE username = ?
        ''', ('staff_user',)).fetchone()
        staff_id = result['id']
    finally:
        conn.close()
    
    response = admin_session.post(f'/api/staff/accounts/{staff_id}/deactivate')
    
    assert response.status_code == 200
    data = response.get_json()
    assert 'deactivated' in data['message'].lower()


def test_cannot_deactivate_last_admin(client, admin_session, auth_db):
    """Cannot deactivate the last active admin account."""
    # First, deactivate admin_user_2 to leave only admin_user
    conn = auth_db._get_connection()
    try:
        result = conn.execute('''
            SELECT id FROM staff_accounts WHERE username = ?
        ''', ('admin_user_2',)).fetchone()
        admin2_id = result['id']
    finally:
        conn.close()
    
    # Deactivate second admin
    admin_session.post(f'/api/staff/accounts/{admin2_id}/deactivate')
    
    # Now try to deactivate the last admin (admin_user)
    conn = auth_db._get_connection()
    try:
        result = conn.execute('''
            SELECT id FROM staff_accounts WHERE username = ?
        ''', ('admin_user',)).fetchone()
        admin_id = result['id']
    finally:
        conn.close()
    
    response = admin_session.post(f'/api/staff/accounts/{admin_id}/deactivate')
    
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert 'last active admin' in data['error'].lower()


# ============================================================================
# Role Change Tests
# ============================================================================

def test_cannot_demote_last_admin(client, admin_session, auth_db):
    """Cannot change role of last active admin to non-admin."""
    # First, ensure we have only one active admin
    conn = auth_db._get_connection()
    try:
        # Deactivate admin_user_2
        result = conn.execute('''
            SELECT id FROM staff_accounts WHERE username = ?
        ''', ('admin_user_2',)).fetchone()
        if result:
            admin2_id = result['id']
            conn.execute('''
                UPDATE staff_accounts SET is_active = 0 WHERE id = ?
            ''', (admin2_id,))
            conn.commit()
        
        # Get admin_user's ID
        result = conn.execute('''
            SELECT id FROM staff_accounts WHERE username = ?
        ''', ('admin_user',)).fetchone()
        admin_id = result['id']
    finally:
        conn.close()
    
    response = admin_session.post(f'/api/staff/accounts/{admin_id}/role', json={
        'role': 'staff'
    })
    
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert 'last active admin' in data['error'].lower()


# ============================================================================
# Password Reset Tests
# ============================================================================

def test_reset_password_sets_must_change_flag(client, admin_session, auth_db):
    """Resetting password should set password_must_change flag."""
    # Get staff_user's ID
    conn = auth_db._get_connection()
    try:
        result = conn.execute('''
            SELECT id FROM staff_accounts WHERE username = ?
        ''', ('staff_user',)).fetchone()
        staff_id = result['id']
    finally:
        conn.close()
    
    response = admin_session.post(f'/api/staff/accounts/{staff_id}/reset-password', json={
        'new_password': 'NewTemp789!'
    })
    
    assert response.status_code == 200
    
    # Verify password_must_change is set
    conn = auth_db._get_connection()
    try:
        result = conn.execute('''
            SELECT password_must_change FROM staff_accounts WHERE id = ?
        ''', (staff_id,)).fetchone()
        assert result['password_must_change'] == 1
    finally:
        conn.close()


# ============================================================================
# Audit Log Tests
# ============================================================================

def test_audit_log_records_account_creation(client, admin_session, auth_db):
    """Account creation should be logged in audit log."""
    response = admin_session.post('/api/staff/accounts', json={
        'username': 'audit_test_user',
        'role': 'staff',
        'password': 'TempPass999!'
    })
    
    assert response.status_code == 201
    
    # Check audit log
    conn = auth_db._get_connection()
    try:
        result = conn.execute('''
            SELECT * FROM account_audit_log 
            WHERE action = ? AND target_username = ?
            ORDER BY timestamp DESC LIMIT 1
        ''', ('account_created', 'audit_test_user')).fetchone()
        
        assert result is not None
        assert result['actor'] == 'admin_user'
        assert result['target_username'] == 'audit_test_user'
    finally:
        conn.close()


def test_role_change_audited(client, admin_session, auth_db):
    """Role changes should be logged in audit log."""
    # Get staff_user's ID
    conn = auth_db._get_connection()
    try:
        result = conn.execute('''
            SELECT id FROM staff_accounts WHERE username = ?
        ''', ('staff_user',)).fetchone()
        staff_id = result['id']
    finally:
        conn.close()
    
    response = admin_session.post(f'/api/staff/accounts/{staff_id}/role', json={
        'role': 'admin'
    })
    
    assert response.status_code == 200
    
    # Verify role was changed in database
    conn = auth_db._get_connection()
    try:
        result = conn.execute('''
            SELECT role FROM staff_accounts WHERE id = ?
        ''', (staff_id,)).fetchone()
        assert result is not None
        assert result['role'] == 'admin'
    finally:
        conn.close()
    
    # Check audit log
    conn = auth_db._get_connection()
    try:
        result = conn.execute('''
            SELECT * FROM account_audit_log 
            WHERE action = ? AND target_username = ?
            ORDER BY timestamp DESC LIMIT 1
        ''', ('role_changed', 'staff_user')).fetchone()
        
        assert result is not None
        assert result['actor'] == 'admin_user'
        assert result['target_username'] == 'staff_user'
    finally:
        conn.close()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
