"""
Staff account management routes (admin-only).

Provides:
- Account listing (no password hashes)
- Account creation with temporary passwords
- Role management
- Account activation/deactivation
- Password reset
- Last-admin protection
- Audit logging for all account operations
"""

from flask import Blueprint, request, jsonify, render_template
from datetime import datetime, timezone
from typing import Optional

from web.auth import (
    AuthDatabase,
    require_auth,
    require_role,
    require_csrf,
    generate_csrf_token
)

# Create Blueprint
staff_bp = Blueprint('staff', __name__, url_prefix='/api/staff')

# Initialize auth database (will be configured by app)
_auth_db: Optional[AuthDatabase] = None


def init_staff_routes(auth_db: AuthDatabase):
    """Initialize the staff routes with database instance."""
    global _auth_db
    _auth_db = auth_db


def log_account_action(action: str, actor: str, target_username: str, details: str = ''):
    """Log account management actions to audit table."""
    conn = _auth_db._get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute('''
            INSERT INTO account_audit_log (timestamp, action, actor, target_username, details)
            VALUES (?, ?, ?, ?, ?)
        ''', (now, action, actor, target_username, details))
        conn.commit()
    finally:
        conn.close()


def count_active_admins() -> int:
    """Count number of active admin accounts."""
    conn = _auth_db._get_connection()
    try:
        row = conn.execute('''
            SELECT COUNT(*) as count FROM staff_accounts
            WHERE role = 'admin' AND is_active = 1
        ''').fetchone()
        return row['count'] if row else 0
    finally:
        conn.close()


def is_last_active_admin(user_id: int) -> bool:
    """Check if user is the last active admin."""
    conn = _auth_db._get_connection()
    try:
        # Check if this user is an admin
        user_row = conn.execute('''
            SELECT role FROM staff_accounts WHERE id = ? AND is_active = 1
        ''', (user_id,)).fetchone()
        
        if not user_row or user_row['role'] != 'admin':
            return False
        
        # Count active admins
        count = count_active_admins()
        return count == 1
    finally:
        conn.close()


# ============================================================================
# Page Route
# ============================================================================

@staff_bp.route('/accounts-page', methods=['GET'])
@require_auth
@require_role('admin')
def staff_accounts_page(**kwargs):
    """Render the staff accounts management page (admin only)."""
    from flask import render_template
    return render_template('staff_accounts.html')


# ============================================================================
# Account Management Endpoints
# ============================================================================

@staff_bp.route('/session', methods=['GET'])
@require_auth
def get_session(**kwargs):
    """Get current session (reuses calendar session endpoint logic)."""
    user_info = kwargs.get('_auth_user')
    csrf_token = generate_csrf_token()
    
    return jsonify({
        'authenticated': True,
        'user': {
            'username': user_info['username'],
            'role': user_info['role']
        },
        'csrf_token': csrf_token
    })


@staff_bp.route('/accounts', methods=['GET'])
@require_auth
@require_role('admin')
def list_accounts(**kwargs):
    """
    List all staff accounts (admin only).
    
    Returns account information WITHOUT password hashes.
    """
    conn = _auth_db._get_connection()
    try:
        rows = conn.execute('''
            SELECT id, username, role, created_at, last_login, is_active
            FROM staff_accounts
            ORDER BY username
        ''').fetchall()
        
        accounts = [dict(row) for row in rows]
        
        return jsonify({
            'success': True,
            'accounts': accounts
        })
    finally:
        conn.close()


@staff_bp.route('/accounts', methods=['POST'])
@require_auth
@require_role('admin')
@require_csrf
def create_account(**kwargs):
    """
    Create a new staff account (admin only).
    
    Request body:
        {
            "username": "newuser",
            "password": "temporary_password",
            "role": "staff" or "admin"
        }
    
    Password is temporary - user must change on first login.
    """
    user_info = kwargs.get('_auth_user')
    actor = user_info['username']
    
    data = request.get_json()
    
    if not data or 'username' not in data or 'password' not in data or 'role' not in data:
        return jsonify({
            'success': False,
            'error': 'Missing required fields: username, password, role'
        }), 400
    
    username = data['username'].strip()
    password = data['password']
    role = data['role']
    
    if not username:
        return jsonify({
            'success': False,
            'error': 'Username cannot be empty'
        }), 400
    
    if len(password) < 8:
        return jsonify({
            'success': False,
            'error': 'Password must be at least 8 characters'
        }), 400
    
    if role not in ('staff', 'admin'):
        return jsonify({
            'success': False,
            'error': 'Role must be "staff" or "admin"'
        }), 400
    
    # Create account
    success, error = _auth_db.create_staff_account(username, password, role)
    
    if success:
        # Mark account as requiring password change
        conn = _auth_db._get_connection()
        try:
            conn.execute('''
                UPDATE staff_accounts SET password_must_change = 1
                WHERE username = ?
            ''', (username,))
            conn.commit()
        finally:
            conn.close()
        
        # Log action (no password in log)
        log_account_action('account_created', actor, username, f'Role: {role}, must change password')
        
        return jsonify({
            'success': True,
            'message': f'Account "{username}" created successfully'
        }), 201
    else:
        return jsonify({
            'success': False,
            'error': error
        }), 400


@staff_bp.route('/accounts/<int:account_id>/role', methods=['POST'])
@require_auth
@require_role('admin')
@require_csrf
def change_role(**kwargs):
    """
    Change account role (admin only).
    
    Protects against demoting the last active admin.
    Role changes take effect on next request.
    """
    user_info = kwargs.get('_auth_user')
    actor = user_info['username']
    account_id = int(request.view_args['account_id'])
    
    data = request.get_json()
    if not data or 'role' not in data:
        return jsonify({
            'success': False,
            'error': 'Missing required field: role'
        }), 400
    
    new_role = data['role']
    if new_role not in ('staff', 'admin'):
        return jsonify({
            'success': False,
            'error': 'Role must be "staff" or "admin"'
        }), 400
    
    conn = _auth_db._get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        
        # Get current account info
        row = conn.execute('''
            SELECT username, role, is_active FROM staff_accounts WHERE id = ?
        ''', (account_id,)).fetchone()
        
        if not row:
            conn.rollback()
            return jsonify({
                'success': False,
                'error': 'Account not found'
            }), 404
        
        old_role = row['role']
        username = row['username']
        is_active = row['is_active']
        
        # Check if demoting last admin
        if old_role == 'admin' and new_role != 'admin' and is_active:
            if is_last_active_admin(account_id):
                conn.rollback()
                return jsonify({
                    'success': False,
                    'error': 'Cannot demote the last active admin account'
                }), 400
        
        # Update role
        conn.execute('''
            UPDATE staff_accounts SET role = ? WHERE id = ?
        ''', (new_role, account_id))
        conn.commit()
        
        # Log action
        log_account_action('role_changed', actor, username, f'From {old_role} to {new_role}')
        
        return jsonify({
            'success': True,
            'message': f'Role changed to {new_role}'
        })
        
    except Exception as e:
        conn.rollback()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
    finally:
        conn.close()


@staff_bp.route('/accounts/<int:account_id>/deactivate', methods=['POST'])
@require_auth
@require_role('admin')
@require_csrf
def deactivate_account(**kwargs):
    """
    Deactivate account (admin only).
    
    Protects against deactivating the last active admin.
    Invalidates all sessions immediately.
    Preserves booking and audit history.
    """
    user_info = kwargs.get('_auth_user')
    actor = user_info['username']
    account_id = int(request.view_args['account_id'])
    
    conn = _auth_db._get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        
        # Get account info
        row = conn.execute('''
            SELECT username, role, is_active FROM staff_accounts WHERE id = ?
        ''', (account_id,)).fetchone()
        
        if not row:
            conn.rollback()
            return jsonify({
                'success': False,
                'error': 'Account not found'
            }), 404
        
        username = row['username']
        role = row['role']
        is_active = row['is_active']
        
        if not is_active:
            conn.rollback()
            return jsonify({
                'success': False,
                'error': 'Account is already inactive'
            }), 400
        
        # Check if last admin
        if role == 'admin' and is_last_active_admin(account_id):
            conn.rollback()
            return jsonify({
                'success': False,
                'error': 'Cannot deactivate the last active admin account'
            }), 400
        
        # Deactivate account
        conn.execute('''
            UPDATE staff_accounts SET is_active = 0 WHERE id = ?
        ''', (account_id,))
        
        # Invalidate all sessions
        conn.execute('''
            DELETE FROM sessions WHERE user_id = ?
        ''', (account_id,))
        
        conn.commit()
        
        # Log action
        log_account_action('account_deactivated', actor, username, 'All sessions terminated')
        
        return jsonify({
            'success': True,
            'message': f'Account "{username}" deactivated'
        })
        
    except Exception as e:
        conn.rollback()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
    finally:
        conn.close()


@staff_bp.route('/accounts/<int:account_id>/reactivate', methods=['POST'])
@require_auth
@require_role('admin')
@require_csrf
def reactivate_account(**kwargs):
    """Reactivate a deactivated account (admin only)."""
    user_info = kwargs.get('_auth_user')
    actor = user_info['username']
    account_id = int(request.view_args['account_id'])
    
    conn = _auth_db._get_connection()
    try:
        # Get account info
        row = conn.execute('''
            SELECT username, is_active FROM staff_accounts WHERE id = ?
        ''', (account_id,)).fetchone()
        
        if not row:
            return jsonify({
                'success': False,
                'error': 'Account not found'
            }), 404
        
        username = row['username']
        is_active = row['is_active']
        
        if is_active:
            return jsonify({
                'success': False,
                'error': 'Account is already active'
            }), 400
        
        # Reactivate
        conn.execute('''
            UPDATE staff_accounts SET is_active = 1 WHERE id = ?
        ''', (account_id,))
        conn.commit()
        
        # Log action
        log_account_action('account_reactivated', actor, username, '')
        
        return jsonify({
            'success': True,
            'message': f'Account "{username}" reactivated'
        })
        
    finally:
        conn.close()


@staff_bp.route('/accounts/<int:account_id>/reset-password', methods=['POST'])
@require_auth
@require_role('admin')
@require_csrf
def reset_password(**kwargs):
    """
    Reset account password (admin only).
    
    Invalidates all existing sessions.
    User must change password on next login.
    Password is never logged.
    """
    user_info = kwargs.get('_auth_user')
    actor = user_info['username']
    account_id = int(request.view_args['account_id'])
    
    data = request.get_json()
    if not data or 'new_password' not in data:
        return jsonify({
            'success': False,
            'error': 'Missing required field: new_password'
        }), 400
    
    new_password = data['new_password']
    
    if len(new_password) < 8:
        return jsonify({
            'success': False,
            'error': 'Password must be at least 8 characters'
        }), 400
    
    conn = _auth_db._get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        
        # Get account info
        row = conn.execute('''
            SELECT username FROM staff_accounts WHERE id = ?
        ''', (account_id,)).fetchone()
        
        if not row:
            conn.rollback()
            return jsonify({
                'success': False,
                'error': 'Account not found'
            }), 404
        
        username = row['username']
        
        # Hash new password
        from web.auth import hash_password
        password_hash = hash_password(new_password)
        
        # Update password and set must_change flag
        conn.execute('''
            UPDATE staff_accounts 
            SET password_hash = ?, password_must_change = 1
            WHERE id = ?
        ''', (password_hash, account_id))
        
        # Invalidate all sessions
        conn.execute('''
            DELETE FROM sessions WHERE user_id = ?
        ''', (account_id,))
        
        conn.commit()
        
        # Log action (NO PASSWORD IN LOG)
        log_account_action('password_reset', actor, username, 'Sessions terminated, must change on login')
        
        return jsonify({
            'success': True,
            'message': f'Password reset for "{username}". User must change on next login.'
        })
        
    except Exception as e:
        conn.rollback()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
    finally:
        conn.close()


@staff_bp.route('/change-password', methods=['POST'])
@require_auth
@require_csrf
def change_own_password(**kwargs):
    """
    Change own password (any authenticated user).
    
    Requires current password verification.
    """
    user_info = kwargs.get('_auth_user')
    user_id = user_info['user_id']
    username = user_info['username']
    
    data = request.get_json()
    if not data or 'current_password' not in data or 'new_password' not in data:
        return jsonify({
            'success': False,
            'error': 'Missing required fields: current_password, new_password'
        }), 400
    
    current_password = data['current_password']
    new_password = data['new_password']
    
    if len(new_password) < 8:
        return jsonify({
            'success': False,
            'error': 'New password must be at least 8 characters'
        }), 400
    
    # Verify current password
    verified = _auth_db.verify_credentials(username, current_password)
    if not verified:
        return jsonify({
            'success': False,
            'error': 'Current password is incorrect'
        }), 401
    
    conn = _auth_db._get_connection()
    try:
        # Hash new password
        from web.auth import hash_password
        password_hash = hash_password(new_password)
        
        # Update password and clear must_change flag
        conn.execute('''
            UPDATE staff_accounts 
            SET password_hash = ?, password_must_change = 0
            WHERE id = ?
        ''', (password_hash, user_id))
        conn.commit()
        
        # Log action (NO PASSWORD IN LOG)
        log_account_action('password_changed', username, username, 'Self-service password change')
        
        return jsonify({
            'success': True,
            'message': 'Password changed successfully'
        })
        
    except Exception as e:
        conn.rollback()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
    finally:
        conn.close()
