"""
Authentication and authorization for staff-only calendar management endpoints.

Implements:
- Password hashing with bcrypt
- Session-based authentication with server-side storage
- CSRF token validation for state-changing requests
- Role-based access control (staff, admin)
"""

import secrets
from functools import wraps
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Tuple
from flask import request, jsonify, session
import sqlite3
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash


class AuthDatabase:
    """Manages staff accounts and sessions in SQLite."""
    
    def __init__(self, db_path: str = 'auth.db'):
        self.db_path = db_path
        self._init_schema()
    
    def _get_connection(self):
        """Get a database connection. Caller must close it."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def close_all_connections(self):
        """Close all connections. Called explicitly in tests."""
        # No-op for now; individual methods manage their connections
        pass
    
    def _init_schema(self):
        """Initialize auth database schema."""
        conn = self._get_connection()
        try:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS staff_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('staff', 'admin')),
                    created_at TEXT NOT NULL,
                    last_login TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    password_must_change INTEGER NOT NULL DEFAULT 0
                );
                
                CREATE INDEX IF NOT EXISTS idx_staff_username ON staff_accounts(username);
                
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_activity TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES staff_accounts(id)
                );
                
                CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                
                CREATE TABLE IF NOT EXISTS account_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    target_username TEXT NOT NULL,
                    details TEXT
                );
                
                CREATE INDEX IF NOT EXISTS idx_account_audit_timestamp ON account_audit_log(timestamp);
                CREATE INDEX IF NOT EXISTS idx_account_audit_target ON account_audit_log(target_username);
            ''')
            conn.commit()
        finally:
            conn.close()
    
    def create_staff_account(self, username: str, password: str, role: str = 'staff') -> Tuple[bool, str]:
        """
        Create a new staff account with hashed password.
        
        Args:
            username: Unique username
            password: Plain-text password (will be hashed)
            role: 'staff' or 'admin'
        
        Returns:
            (success, error_message)
        """
        if not username or not password:
            return False, "Username and password are required"
        
        if role not in ('staff', 'admin'):
            return False, "Role must be 'staff' or 'admin'"
        
        if len(password) < 8:
            return False, "Password must be at least 8 characters"
        
        password_hash = hash_password(password)
        now = datetime.now(timezone.utc).isoformat()
        
        conn = self._get_connection()
        try:
            conn.execute('''
                INSERT INTO staff_accounts (username, password_hash, role, created_at, is_active)
                VALUES (?, ?, ?, ?, 1)
            ''', (username, password_hash, role, now))
            conn.commit()
            return True, ""
        except sqlite3.IntegrityError:
            return False, f"Username '{username}' already exists"
        except Exception as e:
            return False, str(e)
        finally:
            conn.close()
    
    def verify_credentials(self, username: str, password: str) -> Optional[Dict]:
        """
        Verify username and password.
        
        Returns:
            User dict if valid, None otherwise
        """
        conn = self._get_connection()
        try:
            row = conn.execute('''
                SELECT id, username, password_hash, role, is_active
                FROM staff_accounts
                WHERE username = ?
            ''', (username,)).fetchone()
            
            if not row or not row['is_active']:
                return None
            
            if verify_password(password, row['password_hash']):
                # Update last login
                now = datetime.now(timezone.utc).isoformat()
                conn.execute('''
                    UPDATE staff_accounts SET last_login = ? WHERE id = ?
                ''', (now, row['id']))
                conn.commit()
                
                return {
                    'id': row['id'],
                    'username': row['username'],
                    'role': row['role']
                }
            
            return None
        finally:
            conn.close()
    
    def create_session(self, user_id: int, username: str, role: str, 
                      session_duration_hours: int = 8) -> str:
        """Create a new session and return session_id."""
        session_id = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=session_duration_hours)
        
        conn = self._get_connection()
        try:
            conn.execute('''
                INSERT INTO sessions (session_id, user_id, username, role, 
                                     created_at, expires_at, last_activity)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (session_id, user_id, username, role, 
                  now.isoformat(), expires_at.isoformat(), now.isoformat()))
            conn.commit()
        finally:
            conn.close()
        
        return session_id
    
    def validate_session(self, session_id: str) -> Optional[Dict]:
        """
        Validate session and return user info if valid.
        Updates last_activity timestamp.
        """
        if not session_id:
            return None
        
        now = datetime.now(timezone.utc)
        
        conn = self._get_connection()
        try:
            row = conn.execute('''
                SELECT s.user_id, s.username, s.role, s.expires_at, a.is_active
                FROM sessions s
                JOIN staff_accounts a ON s.user_id = a.id
                WHERE s.session_id = ?
            ''', (session_id,)).fetchone()
            
            if not row or not row['is_active']:
                return None
            
            # Check expiration
            expires_at = datetime.fromisoformat(row['expires_at'])
            # Ensure timezone aware comparison
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at < now:
                # Clean up expired session
                conn.execute('DELETE FROM sessions WHERE session_id = ?', (session_id,))
                conn.commit()
                return None
            
            # Update last activity
            conn.execute('''
                UPDATE sessions SET last_activity = ? WHERE session_id = ?
            ''', (now.isoformat(), session_id))
            conn.commit()
            
            return {
                'user_id': row['user_id'],
                'username': row['username'],
                'role': row['role']
            }
        finally:
            conn.close()
    
    def delete_session(self, session_id: str):
        """Delete a session (logout)."""
        conn = self._get_connection()
        try:
            conn.execute('DELETE FROM sessions WHERE session_id = ?', (session_id,))
            conn.commit()
        finally:
            conn.close()
    
    def cleanup_expired_sessions(self):
        """Remove expired sessions from database."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_connection()
        try:
            cursor = conn.execute('DELETE FROM sessions WHERE expires_at < ?', (now,))
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()


# Password hashing using Werkzeug's secure methods
from werkzeug.security import generate_password_hash, check_password_hash


def hash_password(password: str) -> str:
    """Hash password using Werkzeug's secure method (pbkdf2:sha256)."""
    return generate_password_hash(password, method='pbkdf2:sha256')


def verify_password(password: str, password_hash: str) -> bool:
    """Verify password against stored hash."""
    return check_password_hash(password_hash, password)


# CSRF token management
_csrf_tokens: Dict[str, datetime] = {}

def generate_csrf_token() -> str:
    """Generate a new CSRF token."""
    token = secrets.token_urlsafe(32)
    _csrf_tokens[token] = datetime.now(timezone.utc) + timedelta(hours=1)
    return token


def validate_csrf_token(token: str) -> bool:
    """Validate CSRF token and clean up expired tokens."""
    # Clean expired tokens
    now = datetime.now(timezone.utc)
    expired = [t for t, exp in _csrf_tokens.items() if exp < now]
    for t in expired:
        del _csrf_tokens[t]
    
    # Validate token
    if token in _csrf_tokens:
        if _csrf_tokens[token] >= now:
            # Single-use token - delete after validation
            del _csrf_tokens[token]
            return True
    
    return False


# Authentication decorators
def require_auth(f):
    """
    Decorator to require valid session authentication.
    Injects user info into kwargs.
    
    For API requests (Accept: application/json or URL starts with /api/):
        Returns 401 JSON response if not authenticated
    For HTML page requests:
        Redirects to login page with return URL
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        session_id = request.cookies.get('session_id')
        
        # Determine if this is an API request
        is_api_request = (
            request.path.startswith('/api/') or
            'application/json' in request.headers.get('Accept', '')
        )
        
        if not session_id:
            if is_api_request:
                return jsonify({
                    'error': 'Unauthorized',
                    'message': 'Authentication required'
                }), 401
            else:
                # HTML page request - redirect to login
                from flask import redirect, url_for
                return_url = request.path
                if request.query_string:
                    return_url += '?' + request.query_string.decode('utf-8')
                
                # Validate return URL
                if not return_url.startswith('/'):
                    return_url = '/'
                if return_url.startswith('//'):
                    return_url = '/'
                
                return redirect(url_for('staff_login_page') + f'?next={return_url}')
        
        # Get auth_db from Flask app config
        from flask import current_app
        auth_db = current_app.config.get('AUTH_DB')
        if not auth_db:
            if is_api_request:
                return jsonify({
                    'error': 'Configuration Error',
                    'message': 'Authentication not configured'
                }), 500
            else:
                from flask import redirect, url_for
                return redirect(url_for('staff_login_page'))
        
        user_info = auth_db.validate_session(session_id)
        
        if not user_info:
            if is_api_request:
                return jsonify({
                    'error': 'Unauthorized',
                    'message': 'Invalid or expired session'
                }), 401
            else:
                # HTML page request - redirect to login
                from flask import redirect, url_for
                return_url = request.path
                if request.query_string:
                    return_url += '?' + request.query_string.decode('utf-8')
                
                # Validate return URL
                if not return_url.startswith('/'):
                    return_url = '/'
                if return_url.startswith('//'):
                    return_url = '/'
                
                return redirect(url_for('staff_login_page') + f'?next={return_url}')
        
        # Inject auth context
        kwargs['_auth_user'] = user_info
        return f(*args, **kwargs)
    
    return decorated_function


def require_role(*allowed_roles):
    """
    Decorator to require specific role(s).
    Must be used with @require_auth.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user_info = kwargs.get('_auth_user')
            
            if not user_info:
                return jsonify({
                    'error': 'Unauthorized',
                    'message': 'Authentication required'
                }), 401
            
            if user_info['role'] not in allowed_roles:
                return jsonify({
                    'error': 'Forbidden',
                    'message': f'Role {user_info["role"]} not permitted. Required: {", ".join(allowed_roles)}'
                }), 403
            
            return f(*args, **kwargs)
        
        return decorated_function
    return decorator


def require_csrf(f):
    """
    Decorator to validate CSRF token on state-changing requests.
    Expects X-CSRF-Token header.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            csrf_token = request.headers.get('X-CSRF-Token')
            
            if not csrf_token or not validate_csrf_token(csrf_token):
                return jsonify({
                    'error': 'Forbidden',
                    'message': 'Invalid or missing CSRF token'
                }), 403
        
        return f(*args, **kwargs)
    
    return decorated_function


def get_current_user() -> Optional[Dict]:
    """
    Get current authenticated user from request context.
    Returns None if not authenticated.
    """
    session_id = request.cookies.get('session_id')
    if not session_id:
        return None
    
    auth_db = AuthDatabase()
    return auth_db.validate_session(session_id)
