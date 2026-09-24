"""
Calendar management routes with staff authentication and CSRF protection.

All state-changing endpoints require:
- Valid session authentication
- Staff or admin role
- CSRF token validation

Provides:
- Login/logout endpoints
- CSRF token generation
- Appointment approval/decline with authorization
- Calendar availability queries
- Booking creation (staff-initiated)
- Audit trail for all actions
"""

from flask import Blueprint, request, jsonify, make_response
from datetime import datetime, timedelta, date, timezone
from typing import Optional, Dict

from web.auth import (
    AuthDatabase, 
    require_auth, 
    require_role, 
    require_csrf,
    generate_csrf_token,
    get_current_user
)
from scheduling.database import SchedulingDatabase, AppointmentStatus
from scheduling.calendar_service import CalendarService


# Create Blueprint
calendar_bp = Blueprint('calendar', __name__, url_prefix='/api/calendar')


# Initialize services (will be configured by app)
_auth_db: Optional[AuthDatabase] = None
_scheduling_db: Optional[SchedulingDatabase] = None
_calendar_service: Optional[CalendarService] = None


def init_calendar_routes(auth_db: AuthDatabase, scheduling_db: SchedulingDatabase):
    """Initialize the calendar routes with database instances."""
    global _auth_db, _scheduling_db, _calendar_service
    _auth_db = auth_db
    _scheduling_db = scheduling_db
    _calendar_service = CalendarService(scheduling_db)
    
    # Store in Flask's current_app.config for decorator access
    # This will be set when the blueprint is registered with an app
    from flask import current_app
    try:
        current_app.config['AUTH_DB'] = auth_db
    except RuntimeError:
        # No app context yet - will be set when app runs
        pass


# ============================================================================
# Authentication Endpoints
# ============================================================================

@calendar_bp.route('/login', methods=['POST'])
def login():
    """
    Staff login endpoint.
    
    Request body:
        {
            "username": "staff_username",
            "password": "staff_password"
        }
    
    Returns:
        Session cookie and CSRF token on success
    """
    data = request.get_json()
    
    if not data or 'username' not in data or 'password' not in data:
        return jsonify({
            'success': False,
            'error': 'Username and password required'
        }), 400
    
    # Verify credentials
    user_info = _auth_db.verify_credentials(data['username'], data['password'])
    
    if not user_info:
        return jsonify({
            'success': False,
            'error': 'Invalid credentials'
        }), 401
    
    # Create session
    session_id = _auth_db.create_session(
        user_info['id'],
        user_info['username'],
        user_info['role'],
        session_duration_hours=8
    )
    
    # Generate CSRF token
    csrf_token = generate_csrf_token()
    
    # Set session cookie
    response = make_response(jsonify({
        'success': True,
        'user': {
            'username': user_info['username'],
            'role': user_info['role']
        },
        'csrf_token': csrf_token
    }))
    
    # Set secure cookie (httponly, secure in production)
    response.set_cookie(
        'session_id',
        session_id,
        httponly=True,
        secure=False,  # Set to True in production with HTTPS
        samesite='Strict',
        max_age=8*60*60  # 8 hours
    )
    
    return response


@calendar_bp.route('/logout', methods=['POST'])
@require_auth
def logout(**kwargs):
    """
    Staff logout endpoint.
    Requires valid session.
    """
    session_id = request.cookies.get('session_id')
    
    if session_id:
        _auth_db.delete_session(session_id)
    
    response = make_response(jsonify({
        'success': True,
        'message': 'Logged out successfully'
    }))
    
    # Clear session cookie
    response.set_cookie('session_id', '', expires=0)
    
    return response


@calendar_bp.route('/session', methods=['GET'])
@require_auth
def get_session(**kwargs):
    """
    Get current session information.
    Returns user info if authenticated.
    """
    user_info = kwargs.get('_auth_user')
    
    # Generate a new CSRF token for this session
    csrf_token = generate_csrf_token()
    
    return jsonify({
        'authenticated': True,
        'user': {
            'username': user_info['username'],
            'role': user_info['role']
        },
        'csrf_token': csrf_token
    })


@calendar_bp.route('/csrf-token', methods=['GET'])
@require_auth
def get_csrf_token(**kwargs):
    """
    Get a new CSRF token.
    Requires authentication.
    """
    csrf_token = generate_csrf_token()
    return jsonify({
        'csrf_token': csrf_token
    })


# ============================================================================
# Calendar Availability Endpoints
# ============================================================================

@calendar_bp.route('/availability', methods=['GET'])
@require_auth
@require_role('staff', 'admin')
def get_availability(**kwargs):
    """
    Get calendar availability for a date range.
    
    Query parameters:
        - start_date: YYYY-MM-DD (required)
        - end_date: YYYY-MM-DD (required)
    
    Returns:
        List of slots with availability status
    """
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    if not start_date or not end_date:
        return jsonify({
            'success': False,
            'error': 'start_date and end_date required'
        }), 400
    
    try:
        # Validate date format
        datetime.strptime(start_date, '%Y-%m-%d')
        datetime.strptime(end_date, '%Y-%m-%d')
        
        slots = _calendar_service.get_availability(start_date, end_date)
        
        return jsonify({
            'success': True,
            'slots': slots,
            'count': len(slots)
        })
        
    except ValueError as e:
        return jsonify({
            'success': False,
            'error': f'Invalid date format: {e}'
        }), 400
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@calendar_bp.route('/config', methods=['GET'])
@require_auth
@require_role('staff', 'admin')
def get_config(**kwargs):
    """
    Get clinic configuration (timezone, hours, etc).
    """
    config = _scheduling_db.get_config()
    
    return jsonify({
        'success': True,
        'config': config
    })


@calendar_bp.route('/config', methods=['POST'])
@require_auth
@require_role('admin')  # Only admins can update configuration
@require_csrf
def update_config(**kwargs):
    """
    Update clinic configuration (admin only).
    
    Request body: partial or complete configuration object
        {
            "timezone_name": "America/New_York",
            "working_days": [0, 1, 2, 3, 4],
            "sessions": {
                "morning": {"start": "08:00", "end": "12:00"},
                "afternoon": {"start": "13:00", "end": "17:00"}
            },
            "slot_duration_minutes": 30,
            "slots_per_session": 3,
            "pending_expiry_minutes": 15
        }
    
    Note: Be cautious when changing configuration as it may affect existing appointments.
    """
    user_info = kwargs.get('_auth_user')
    actor = user_info['username']
    
    data = request.get_json()
    
    if not data:
        return jsonify({
            'success': False,
            'error': 'No configuration data provided'
        }), 400
    
    try:
        # Get current config
        current_config = _scheduling_db.get_config()
        
        # Update with new values
        updated_config = {**current_config, **data}
        
        # Validate required fields (match database schema)
        required = ['timezone_name', 'working_days', 'sessions', 
                   'slot_duration_minutes', 'slots_per_session']
        missing = [f for f in required if f not in updated_config]
        
        if missing:
            return jsonify({
                'success': False,
                'error': f'Missing required configuration fields: {", ".join(missing)}'
            }), 400
        
        # Save updated config
        success = _scheduling_db.set_config(updated_config)
        
        if success:
            # Log the configuration change
            conn = _scheduling_db.get_connection()
            try:
                conn.execute('''
                    INSERT INTO audit_log (
                        timestamp, action, actor, details
                    ) VALUES (?, ?, ?, ?)
                ''', (
                    datetime.now(timezone.utc).isoformat(),
                    'config_update',
                    actor,
                    f'Updated configuration: {list(data.keys())}'
                ))
                conn.commit()
            finally:
                conn.close()
            
            return jsonify({
                'success': True,
                'message': 'Configuration updated',
                'config': updated_config
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to update configuration'
            }), 500
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Configuration update failed: {str(e)}'
        }), 500


# ============================================================================
# Appointment Management Endpoints (State-Changing - Require CSRF)
# ============================================================================

@calendar_bp.route('/appointments', methods=['GET'])
@require_auth
@require_role('staff', 'admin')
def get_appointments(**kwargs):
    """
    Get appointments for a date range.
    
    Query parameters:
        - start_date: YYYY-MM-DD (optional)
        - end_date: YYYY-MM-DD (optional)
        - status: Filter by status (optional)
    
    Returns all appointments if no filters provided.
    """
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    status = request.args.get('status')
    
    conn = _scheduling_db.get_connection()
    try:
        query_parts = ['SELECT * FROM appointment_requests WHERE 1=1']
        params = []
        
        if start_date:
            query_parts.append('AND slot_date >= ?')
            params.append(start_date)
        
        if end_date:
            query_parts.append('AND slot_date <= ?')
            params.append(end_date)
        
        if status:
            query_parts.append('AND status = ?')
            params.append(status)
        
        query_parts.append('ORDER BY slot_datetime_utc ASC')
        
        query = ' '.join(query_parts)
        rows = conn.execute(query, params).fetchall()
        appointments = [dict(row) for row in rows]
        
        return jsonify({
            'success': True,
            'appointments': appointments,
            'count': len(appointments)
        })
        
    finally:
        conn.close()


@calendar_bp.route('/appointments/pending', methods=['GET'])
@require_auth
@require_role('staff', 'admin')
def get_pending_appointments(**kwargs):
    """
    Get all pending appointment requests requiring approval.
    """
    pending = _scheduling_db.get_pending_requests()
    
    return jsonify({
        'success': True,
        'appointments': pending,
        'count': len(pending)
    })


@calendar_bp.route('/appointments/<int:appointment_id>', methods=['GET'])
@require_auth
@require_role('staff', 'admin')
def get_appointment_detail(appointment_id, **kwargs):
    """
    Get detailed information about a specific appointment.
    """
    appointment = _scheduling_db.get_appointment(appointment_id)
    
    if not appointment:
        return jsonify({
            'success': False,
            'error': 'Appointment not found'
        }), 404
    
    return jsonify({
        'success': True,
        'appointment': appointment
    })


@calendar_bp.route('/appointments/<int:appointment_id>/approve', methods=['POST'])
@require_auth
@require_role('staff', 'admin')
@require_csrf
def approve_appointment(appointment_id, **kwargs):
    """
    Approve a pending appointment request with validation.
    
    Validates:
    - Hold has not expired
    - Capacity is still available
    - Slot is not blocked
    
    Requires:
    - Valid session
    - Staff or admin role
    - CSRF token in X-CSRF-Token header
    
    Returns:
        Success status and updated appointment
    """
    user_info = kwargs.get('_auth_user')
    actor = user_info['username']
    
    # Use service method with validation
    result = _calendar_service.approve_with_validation(appointment_id, actor)
    
    if result['success']:
        updated = _scheduling_db.get_appointment(appointment_id)
        return jsonify({
            'success': True,
            'message': 'Appointment approved',
            'appointment': updated
        })
    else:
        # Return 404 for not found, 400 for validation failures
        status_code = 404 if 'not found' in result.get('error', '').lower() else 400
        return jsonify(result), status_code


@calendar_bp.route('/appointments/<int:appointment_id>/decline', methods=['POST'])
@require_auth
@require_role('staff', 'admin')
@require_csrf
def decline_appointment(appointment_id, **kwargs):
    """
    Decline a pending appointment request.
    
    Requires:
    - Valid session
    - Staff or admin role
    - CSRF token
    
    Request body:
        {
            "reason": "Reason for declining"
        }
    """
    user_info = kwargs.get('_auth_user')
    actor = user_info['username']
    
    data = request.get_json()
    reason = data.get('reason', 'Declined by staff') if data else 'Declined by staff'
    
    # Check appointment exists and is pending
    appointment = _scheduling_db.get_appointment(appointment_id)
    if not appointment:
        return jsonify({
            'success': False,
            'error': 'Appointment not found'
        }), 404
    
    if appointment['status'] != AppointmentStatus.PENDING.value:
        return jsonify({
            'success': False,
            'error': f'Cannot decline appointment with status: {appointment["status"]}'
        }), 400
    
    # Decline
    success = _scheduling_db.decline_appointment(appointment_id, actor, reason)
    
    if success:
        updated = _scheduling_db.get_appointment(appointment_id)
        return jsonify({
            'success': True,
            'message': 'Appointment declined',
            'appointment': updated
        })
    else:
        return jsonify({
            'success': False,
            'error': 'Failed to decline appointment'
        }), 500


@calendar_bp.route('/appointments/<int:appointment_id>/cancel', methods=['POST'])
@require_auth
@require_role('staff', 'admin')
@require_csrf
def cancel_appointment(appointment_id, **kwargs):
    """
    Cancel an existing appointment (pending or confirmed).
    
    Request body:
        {
            "reason": "Reason for cancellation"
        }
    """
    user_info = kwargs.get('_auth_user')
    actor = user_info['username']
    
    data = request.get_json()
    reason = data.get('reason', 'Cancelled by staff') if data else 'Cancelled by staff'
    
    appointment = _scheduling_db.get_appointment(appointment_id)
    if not appointment:
        return jsonify({
            'success': False,
            'error': 'Appointment not found'
        }), 404
    
    # Cancel
    success = _scheduling_db.cancel_appointment(appointment_id, actor, reason)
    
    if success:
        updated = _scheduling_db.get_appointment(appointment_id)
        return jsonify({
            'success': True,
            'message': 'Appointment cancelled',
            'appointment': updated
        })
    else:
        return jsonify({
            'success': False,
            'error': 'Failed to cancel appointment'
        }), 500


@calendar_bp.route('/appointments/<int:appointment_id>/complete', methods=['POST'])
@require_auth
@require_role('staff', 'admin')
@require_csrf
def complete_appointment(appointment_id, **kwargs):
    """
    Mark an appointment as completed.
    
    Request body (optional):
        {
            "notes": "Completion notes"
        }
    """
    user_info = kwargs.get('_auth_user')
    actor = user_info['username']
    
    data = request.get_json() or {}
    notes = data.get('notes', '')
    
    appointment = _scheduling_db.get_appointment(appointment_id)
    if not appointment:
        return jsonify({
            'success': False,
            'error': 'Appointment not found'
        }), 404
    
    if appointment['status'] != AppointmentStatus.CONFIRMED.value:
        return jsonify({
            'success': False,
            'error': f'Cannot complete appointment with status: {appointment["status"]}'
        }), 400
    
    # Mark as completed
    conn = _scheduling_db.get_connection()
    try:
        conn.execute('''
            UPDATE appointment_requests
            SET status = ?, updated_at = ?
            WHERE id = ?
        ''', (AppointmentStatus.COMPLETED.value, datetime.now(timezone.utc).isoformat(), appointment_id))
        
        # Add audit log
        conn.execute('''
            INSERT INTO audit_log (
                timestamp, action, actor, appointment_request_id, details
            ) VALUES (?, ?, ?, ?, ?)
        ''', (
            datetime.now(timezone.utc).isoformat(),
            'appointment_completed',
            actor,
            appointment_id,
            notes or 'Appointment marked as completed'
        ))
        
        conn.commit()
        
        updated = _scheduling_db.get_appointment(appointment_id)
        return jsonify({
            'success': True,
            'message': 'Appointment marked as completed',
            'appointment': updated
        })
        
    except Exception as e:
        conn.rollback()
        return jsonify({
            'success': False,
            'error': f'Failed to complete appointment: {str(e)}'
        }), 500
    finally:
        conn.close()


@calendar_bp.route('/appointments', methods=['POST'])
@require_auth
@require_role('staff', 'admin')
@require_csrf
def create_appointment(**kwargs):
    """
    Create a new appointment (staff-initiated booking).
    
    IMPORTANT: Dates are handled as date-only strings (YYYY-MM-DD) throughout
    the system to avoid timezone conversion issues. The frontend sends slot_date
    as a pure date string, and the backend validates it against the clinic timezone.
    
    Request body:
        {
            "patient_id": "P001",
            "patient_name": "John Doe",
            "follow_up_case_id": "CASE-001" (optional),
            "slot_datetime_utc": "2026-10-15T09:00:00Z" (optional, will be normalized),
            "slot_date": "2026-10-15",
            "slot_session": "morning",
            "slot_time": "09:00",
            "follow_up_reason": "Follow-up appointment" (optional)
        }
    """
    user_info = kwargs.get('_auth_user')
    actor = user_info['username']
    
    data = request.get_json()
    
    required_fields = ['patient_id', 'patient_name', 
                       'slot_date', 'slot_session', 'slot_time']
    missing = [f for f in required_fields if f not in data]
    
    if missing:
        return jsonify({
            'success': False,
            'error': f'Missing required fields: {", ".join(missing)}'
        }), 400
    
    # Validate past slot using clinic timezone
    from datetime import datetime, date
    from zoneinfo import ZoneInfo
    
    config = _scheduling_db.get_config()
    clinic_tz = ZoneInfo(config.get('timezone_name', 'America/New_York'))
    
    try:
        slot_date_obj = date.fromisoformat(data['slot_date'])
        # Get today in clinic timezone
        today_in_clinic = datetime.now(clinic_tz).date()
        
        if slot_date_obj < today_in_clinic:
            return jsonify({
                'success': False,
                'error': 'Cannot book appointments in the past'
            }), 400
    except ValueError:
        return jsonify({
            'success': False,
            'error': 'Invalid slot_date format'
        }), 400
    
    # Use calendar service to check capacity and book
    # Service will normalize slot_datetime_utc from slot_date + slot_time
    result = _calendar_service.check_capacity_and_book(
        patient_id=data['patient_id'],
        patient_name=data['patient_name'],
        slot_datetime_utc=data.get('slot_datetime_utc', ''),  # Optional, will be normalized
        slot_date=data['slot_date'],
        slot_session=data['slot_session'],
        slot_time=data['slot_time'],
        requested_by=actor,
        follow_up_case_id=data.get('follow_up_case_id'),
        follow_up_reason=data.get('follow_up_reason')
    )
    
    if result['success']:
        return jsonify(result), 201
    else:
        return jsonify(result), 400


# ============================================================================
# Blocked Periods Management
# ============================================================================

@calendar_bp.route('/blocked-periods', methods=['GET'])
@require_auth
@require_role('staff', 'admin')
def get_blocked_periods(**kwargs):
    """
    Get blocked periods for a date range.
    
    Query parameters:
        - start_date: YYYY-MM-DD
        - end_date: YYYY-MM-DD
    """
    start_date = request.args.get('start_date', date.today().isoformat())
    end_date = request.args.get('end_date', (date.today() + timedelta(days=30)).isoformat())
    
    blocked = _scheduling_db.get_blocked_periods(
        start_date + 'T00:00:00Z',
        end_date + 'T23:59:59Z'
    )
    
    return jsonify({
        'success': True,
        'blocked_periods': blocked,
        'count': len(blocked)
    })


@calendar_bp.route('/blocked-periods', methods=['POST'])
@require_auth
@require_role('admin')  # Only admins can block periods
@require_csrf
def add_blocked_period(**kwargs):
    """
    Add a blocked period (admin only).
    
    Request body:
        {
            "start_datetime": "2026-10-15T09:00:00Z",
            "end_datetime": "2026-10-15T17:00:00Z",
            "reason": "Holiday closure"
        }
    """
    user_info = kwargs.get('_auth_user')
    actor = user_info['username']
    
    data = request.get_json()
    
    required = ['start_datetime', 'end_datetime', 'reason']
    missing = [f for f in required if f not in data]
    
    if missing:
        return jsonify({
            'success': False,
            'error': f'Missing required fields: {", ".join(missing)}'
        }), 400
    
    block_id = _scheduling_db.add_blocked_period(
        start=data['start_datetime'],
        end=data['end_datetime'],
        reason=data['reason'],
        created_by=actor
    )
    
    return jsonify({
        'success': True,
        'message': 'Blocked period added',
        'block_id': block_id
    }), 201


# ============================================================================
# Audit and Reporting
# ============================================================================

@calendar_bp.route('/audit', methods=['GET'])
@require_auth
@require_role('staff', 'admin')
def get_audit_logs(**kwargs):
    """
    Get audit logs for appointment actions.
    
    Query parameters:
        - appointment_id: Filter by appointment (optional)
        - limit: Max number of entries (default 50)
    """
    appointment_id = request.args.get('appointment_id', type=int)
    limit = request.args.get('limit', 50, type=int)
    
    conn = _scheduling_db.get_connection()
    try:
        if appointment_id:
            query = '''
                SELECT * FROM audit_log 
                WHERE appointment_request_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            '''
            rows = conn.execute(query, (appointment_id, limit)).fetchall()
        else:
            query = '''
                SELECT * FROM audit_log 
                ORDER BY timestamp DESC
                LIMIT ?
            '''
            rows = conn.execute(query, (limit,)).fetchall()
        
        logs = [dict(row) for row in rows]
        
        return jsonify({
            'success': True,
            'logs': logs,
            'count': len(logs)
        })
        
    finally:
        conn.close()
