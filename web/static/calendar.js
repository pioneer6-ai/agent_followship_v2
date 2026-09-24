// State
let currentUser = null;
let csrfToken = null;
let currentDate = new Date();
let selectedDate = null;
let calendarConfig = null;
let appointments = [];
let pendingAppointmentId = null;

// Date utilities - handle dates as date-only values without timezone conversion
function formatDate(date) {
    // CRITICAL FIX: Use local date components, not UTC conversion
    // This prevents the Asia/Singapore timezone shift where clicking the 25th
    // would generate "2024-09-24" due to toISOString() UTC conversion
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
}

function parseDateOnly(dateStr) {
    // Parse YYYY-MM-DD as a date-only value at local midnight
    // Avoids timezone shifts from Date(dateStr) which parses as UTC
    const [year, month, day] = dateStr.split('-').map(Number);
    return new Date(year, month - 1, day);
}

function getToday() {
    // Return today's date in the clinic timezone
    // For now, use browser's local date since we can't access server timezone in pure JS
    // The server will validate against clinic timezone
    return formatDate(new Date());
}

function formatDateDisplay(date) {
    return date.toLocaleDateString('en-US', {
        weekday: 'long',
        year: 'numeric',
        month: 'long',
        day: 'numeric'
    });
}

function getMonthName(date) {
    return date.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
}

// API helpers
async function apiCall(endpoint, options = {}) {
    const url = `/api/calendar${endpoint}`;
    const response = await fetch(url, {
        ...options,
        credentials: 'same-origin',
        headers: {
            'Content-Type': 'application/json',
            ...options.headers
        }
    });
    
    // Handle 401 Unauthorized - redirect to login
    if (response.status === 401) {
        const returnUrl = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.href = `/staff/login?next=${returnUrl}`;
        throw new Error('Unauthorized - redirecting to login');
    }
    
    // Handle 403 Forbidden
    if (response.status === 403) {
        const data = await response.json();
        throw new Error(data.message || 'Access forbidden');
    }
    
    return response.json();
}

async function refreshSession() {
    try {
        const data = await apiCall('/session');
        if (!data.authenticated) {
            const returnUrl = encodeURIComponent(window.location.pathname + window.location.search);
            window.location.href = `/staff/login?next=${returnUrl}`;
            return false;
        }
        currentUser = data.user;
        csrfToken = data.csrf_token;
        return true;
    } catch (error) {
        // If session check fails, redirect to login
        const returnUrl = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.href = `/staff/login?next=${returnUrl}`;
        return false;
    }
}

// UI Functions
function showAlert(message, type = 'info') {
    const container = document.getElementById('alertContainer');
    const alert = document.createElement('div');
    alert.className = `alert alert-${type}`;
    alert.textContent = message;
    container.appendChild(alert);
    
    setTimeout(() => alert.remove(), 5000);
}

function renderCalendar() {
    const grid = document.getElementById('calendarGrid');
    document.getElementById('currentMonth').textContent = getMonthName(currentDate);
    
    // Calendar headers
    const headers = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    grid.innerHTML = headers.map(h => 
        `<div class="calendar-header">${h}</div>`
    ).join('');
    
    // Get first day of month and number of days
    const year = currentDate.getFullYear();
    const month = currentDate.getMonth();
    const firstDay = new Date(year, month, 1);
    const lastDay = new Date(year, month + 1, 0);
    const prevLastDay = new Date(year, month, 0);
    
    const startingDayOfWeek = firstDay.getDay();
    const daysInMonth = lastDay.getDate();
    const daysInPrevMonth = prevLastDay.getDate();
    
    const today = getToday();  // FIXED: Use getToday() utility for consistent date-only comparison
    
    // Previous month days
    for (let i = startingDayOfWeek - 1; i >= 0; i--) {
        const day = daysInPrevMonth - i;
        const date = new Date(year, month - 1, day);
        grid.innerHTML += createDayCell(date, true);
    }
    
    // Current month days
    for (let day = 1; day <= daysInMonth; day++) {
        const date = new Date(year, month, day);
        grid.innerHTML += createDayCell(date, false);
    }
    
    // Next month days
    const remainingCells = 42 - (startingDayOfWeek + daysInMonth);
    for (let day = 1; day <= remainingCells; day++) {
        const date = new Date(year, month + 1, day);
        grid.innerHTML += createDayCell(date, true);
    }
}

function createDayCell(date, otherMonth) {
    const dateStr = formatDate(date);
    const today = formatDate(new Date());
    const isSelected = selectedDate && formatDate(selectedDate) === dateStr;
    
    let classes = ['calendar-day'];
    if (otherMonth) classes.push('other-month');
    if (dateStr === today) classes.push('today');
    if (isSelected) classes.push('selected');
    
    // Count appointments for this day
    const dayAppointments = appointments.filter(a => a.slot_date === dateStr);
    const pending = dayAppointments.filter(a => a.status === 'pending').length;
    const confirmed = dayAppointments.filter(a => a.status === 'confirmed').length;
    
    let indicators = '';
    if (pending > 0) {
        indicators += `<div class="day-indicator indicator-pending">${pending} pending</div>`;
    }
    if (confirmed > 0) {
        indicators += `<div class="day-indicator indicator-confirmed">${confirmed} confirmed</div>`;
    }
    
    return `
        <div class="${classes.join(' ')}" onclick="selectDate('${dateStr}')">
            <div class="day-number">${date.getDate()}</div>
            ${indicators}
        </div>
    `;
}

function selectDate(dateStr) {
    // CRITICAL FIX: Parse date string without timezone conversion
    // Using parseDateOnly() instead of new Date(dateStr + 'T12:00:00')
    // to avoid timezone shifts
    selectedDate = parseDateOnly(dateStr);
    renderCalendar();
    showDaySchedule();
}

async function showDaySchedule() {
    if (!selectedDate) return;
    
    const dateStr = formatDate(selectedDate);
    document.getElementById('selectedDateTitle').textContent = formatDateDisplay(selectedDate);
    document.getElementById('daySchedule').classList.remove('hidden');
    
    const container = document.getElementById('scheduleContent');
    container.innerHTML = '<div class="loading">Loading schedule...</div>';
    
    try {
        // Get availability for this date
        const data = await apiCall(`/availability?start_date=${dateStr}&end_date=${dateStr}`);
        
        if (data.slots.length === 0) {
            container.innerHTML = '<div class="empty-state">No availability for this date</div>';
            return;
        }
        
        // Group by session
        const sessions = {};
        data.slots.forEach(slot => {
            if (!sessions[slot.session]) {
                sessions[slot.session] = [];
            }
            sessions[slot.session].push(slot);
        });
        
        // Render sessions
        let html = '';
        for (const [sessionName, slots] of Object.entries(sessions)) {
            html += `
                <div class="session-block">
                    <div class="session-title">${sessionName.charAt(0).toUpperCase() + sessionName.slice(1)}</div>
                    <div class="time-slots">
                        ${slots.map(slot => renderTimeSlot(slot)).join('')}
                    </div>
                </div>
            `;
        }
        
        container.innerHTML = html;
        
    } catch (error) {
        container.innerHTML = `<div class="alert alert-error">Failed to load schedule: ${error.message}</div>`;
    }
}

function renderTimeSlot(slot) {
    let statusClass = 'slot-available';
    let statusText = `${slot.available} available`;
    
    if (slot.is_blocked) {
        statusClass = 'slot-blocked';
        statusText = 'Blocked';
    } else if (slot.available === 0) {
        statusClass = 'slot-full';
        statusText = 'Full';
    } else if (slot.available <= 2) {
        statusClass = 'slot-limited';
        statusText = `${slot.available} left`;
    }
    
    // Find appointments for this slot
    const slotAppointments = appointments.filter(a => 
        a.slot_datetime_utc === slot.datetime_utc
    );
    
    let appointmentsList = '';
    if (slotAppointments.length > 0) {
        appointmentsList = `
            <div class="appointments-list">
                ${slotAppointments.map(a => `
                    <div class="appointment-item" onclick="viewAppointment(${a.id})">
                        <strong>${a.patient_name}</strong> 
                        <span class="badge badge-${a.status}">${a.status}</span>
                    </div>
                `).join('')}
            </div>
        `;
    }
    
    return `
        <div class="time-slot ${statusClass}">
            <div>
                <div class="slot-time">${slot.time}</div>
                ${appointmentsList}
            </div>
            <div class="slot-status">
                <span class="slot-capacity">${statusText}</span>
            </div>
        </div>
    `;
}

async function viewAppointment(id) {
    const appointment = appointments.find(a => a.id === id);
    if (!appointment) return;
    
    const detail = document.getElementById('appointmentDetail');
    detail.innerHTML = `
        <div class="appointment-detail">
            <div class="detail-row">
                <span class="detail-label">Status:</span>
                <span class="detail-value">
                    <span class="badge badge-${appointment.status}">${appointment.status}</span>
                </span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Patient:</span>
                <span class="detail-value">${appointment.patient_name}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Patient ID:</span>
                <span class="detail-value">${appointment.patient_id}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Date:</span>
                <span class="detail-value">${appointment.slot_date}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Time:</span>
                <span class="detail-value">${appointment.slot_time} (${appointment.slot_session})</span>
            </div>
            ${appointment.follow_up_reason ? `
            <div class="detail-row">
                <span class="detail-label">Reason:</span>
                <span class="detail-value">${appointment.follow_up_reason}</span>
            </div>
            ` : ''}
            ${appointment.approved_by ? `
            <div class="detail-row">
                <span class="detail-label">Approved by:</span>
                <span class="detail-value">${appointment.approved_by}</span>
            </div>
            ` : ''}
        </div>
    `;
    
    // Show available actions
    const actions = document.getElementById('appointmentActions');
    actions.innerHTML = '';
    
    if (appointment.status === 'pending') {
        actions.innerHTML = `
            <button class="btn btn-success" onclick="approveAppointment(${id})">✓ Approve</button>
            <button class="btn btn-danger" onclick="showDeclineModal(${id})">✗ Decline</button>
        `;
    } else if (appointment.status === 'confirmed') {
        actions.innerHTML = `
            <button class="btn btn-danger" onclick="cancelAppointment(${id})">Cancel</button>
            <button class="btn btn-success" onclick="completeAppointment(${id})">Mark Complete</button>
        `;
    }
    
    actions.innerHTML += '<button class="btn btn-secondary" onclick="closeAppointmentModal()">Close</button>';
    
    document.getElementById('appointmentModal').classList.add('active');
}

async function approveAppointment(id) {
    try {
        await refreshSession();
        const data = await apiCall(`/appointments/${id}/approve`, {
            method: 'POST',
            headers: {'X-CSRF-Token': csrfToken}
        });
        
        if (data.success) {
            showAlert('Appointment approved successfully', 'success');
            closeAppointmentModal();
            await loadData();
        } else {
            showAlert(data.error || 'Failed to approve', 'error');
        }
    } catch (error) {
        showAlert('Error: ' + error.message, 'error');
    }
}

function showDeclineModal(id) {
    pendingAppointmentId = id;
    document.getElementById('declineReason').value = '';
    closeAppointmentModal();
    document.getElementById('declineModal').classList.add('active');
}

function closeDeclineModal() {
    document.getElementById('declineModal').classList.remove('active');
    pendingAppointmentId = null;
}

async function confirmDecline() {
    const reason = document.getElementById('declineReason').value.trim();
    if (!reason) {
        showAlert('Please provide a reason', 'error');
        return;
    }
    
    try {
        await refreshSession();
        const data = await apiCall(`/appointments/${pendingAppointmentId}/decline`, {
            method: 'POST',
            headers: {'X-CSRF-Token': csrfToken},
            body: JSON.stringify({reason})
        });
        
        if (data.success) {
            showAlert('Appointment declined', 'success');
            closeDeclineModal();
            await loadData();
        } else {
            showAlert(data.error || 'Failed to decline', 'error');
        }
    } catch (error) {
        showAlert('Error: ' + error.message, 'error');
    }
}

async function cancelAppointment(id) {
    if (!confirm('Cancel this appointment?')) return;
    
    const reason = prompt('Reason for cancellation:');
    if (!reason) return;
    
    try {
        await refreshSession();
        const data = await apiCall(`/appointments/${id}/cancel`, {
            method: 'POST',
            headers: {'X-CSRF-Token': csrfToken},
            body: JSON.stringify({reason})
        });
        
        if (data.success) {
            showAlert('Appointment cancelled', 'success');
            closeAppointmentModal();
            await loadData();
        } else {
            showAlert(data.error || 'Failed to cancel', 'error');
        }
    } catch (error) {
        showAlert('Error: ' + error.message, 'error');
    }
}

async function completeAppointment(id) {
    if (!confirm('Mark this appointment as complete?')) return;
    
    try {
        await refreshSession();
        const data = await apiCall(`/appointments/${id}/complete`, {
            method: 'POST',
            headers: {'X-CSRF-Token': csrfToken}
        });
        
        if (data.success) {
            showAlert('Appointment marked complete', 'success');
            closeAppointmentModal();
            await loadData();
        } else {
            showAlert(data.error || 'Failed to complete', 'error');
        }
    } catch (error) {
        showAlert('Error: ' + error.message, 'error');
    }
}

function closeAppointmentModal() {
    document.getElementById('appointmentModal').classList.remove('active');
}

async function loadSettings() {
    try {
        const data = await apiCall('/config');
        calendarConfig = data.config;
        
        // Show settings and accounts buttons for admins
        if (currentUser.role === 'admin') {
            document.getElementById('settingsBtn').classList.remove('hidden');
            document.getElementById('accountsBtn').classList.remove('hidden');
        }
    } catch (error) {
        showAlert('Failed to load settings', 'error');
    }
}

function openSettingsModal() {
    if (!calendarConfig) return;
    
    // Populate form
    document.getElementById('settingsTimezone').value = calendarConfig.timezone_name;
    document.getElementById('settingsSlotDuration').value = calendarConfig.slot_duration_minutes;
    document.getElementById('settingsCapacity').value = calendarConfig.slots_per_session;
    document.getElementById('settingsExpiry').value = calendarConfig.pending_expiry_minutes;
    
    // Working days
    for (let i = 0; i < 7; i++) {
        document.getElementById(`day${i}`).checked = calendarConfig.working_days.includes(i);
    }
    
    // Sessions
    const sessionsDiv = document.getElementById('sessionsConfig');
    sessionsDiv.innerHTML = Object.entries(calendarConfig.sessions).map(([name, times]) => `
        <div class="session-config">
            <div class="session-config-header">
                <strong>${name}</strong>
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label>Start Time</label>
                    <input type="time" id="session_${name}_start" value="${times.start}">
                </div>
                <div class="form-group">
                    <label>End Time</label>
                    <input type="time" id="session_${name}_end" value="${times.end}">
                </div>
            </div>
        </div>
    `).join('');
    
    document.getElementById('settingsModal').classList.add('active');
}

function closeSettingsModal() {
    document.getElementById('settingsModal').classList.remove('active');
}

async function saveSettings(event) {
    event.preventDefault();
    
    const workingDays = [];
    for (let i = 0; i < 7; i++) {
        if (document.getElementById(`day${i}`).checked) {
            workingDays.push(i);
        }
    }
    
    const sessions = {};
    Object.keys(calendarConfig.sessions).forEach(name => {
        sessions[name] = {
            start: document.getElementById(`session_${name}_start`).value,
            end: document.getElementById(`session_${name}_end`).value
        };
    });
    
    const config = {
        timezone_name: document.getElementById('settingsTimezone').value,
        working_days: workingDays,
        sessions: sessions,
        slot_duration_minutes: parseInt(document.getElementById('settingsSlotDuration').value),
        slots_per_session: parseInt(document.getElementById('settingsCapacity').value),
        pending_expiry_minutes: parseInt(document.getElementById('settingsExpiry').value)
    };
    
    try {
        await refreshSession();
        const data = await apiCall('/config', {
            method: 'POST',
            headers: {'X-CSRF-Token': csrfToken},
            body: JSON.stringify(config)
        });
        
        if (data.success) {
            showAlert('Settings saved successfully', 'success');
            closeSettingsModal();
            await loadSettings();
            await loadData();
        } else {
            showAlert(data.error || 'Failed to save settings', 'error');
        }
    } catch (error) {
        showAlert('Error: ' + error.message, 'error');
    }
}

function openNewRequestModal() {
    // Populate session options
    if (calendarConfig) {
        const sessionSelect = document.getElementById('reqSession');
        sessionSelect.innerHTML = '<option value="">Select session...</option>' +
            Object.keys(calendarConfig.sessions).map(name => 
                `<option value="${name}">${name}</option>`
            ).join('');
    }
    
    // FIXED: Set default date to today using getToday() utility
    document.getElementById('reqDate').value = getToday();
    
    document.getElementById('newRequestModal').classList.add('active');
    updateAvailableSlots();
}

function closeNewRequestModal() {
    document.getElementById('newRequestModal').classList.remove('active');
    document.getElementById('newRequestForm').reset();
}

function updateAvailableSlots() {
    const date = document.getElementById('reqDate').value;
    const session = document.getElementById('reqSession').value;
    
    if (!date || !session || !calendarConfig) return;
    
    const sessionConfig = calendarConfig.sessions[session];
    if (!sessionConfig) return;
    
    // Generate time slots
    const start = sessionConfig.start.split(':');
    const end = sessionConfig.end.split(':');
    const startMinutes = parseInt(start[0]) * 60 + parseInt(start[1]);
    const endMinutes = parseInt(end[0]) * 60 + parseInt(end[1]);
    const slotDuration = calendarConfig.slot_duration_minutes;
    
    const timeSelect = document.getElementById('reqTime');
    timeSelect.innerHTML = '<option value="">Select time...</option>';
    
    for (let minutes = startMinutes; minutes < endMinutes; minutes += slotDuration) {
        const hours = Math.floor(minutes / 60);
        const mins = minutes % 60;
        const time = `${hours.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}`;
        timeSelect.innerHTML += `<option value="${time}">${time}</option>`;
    }
}

async function createNewRequest(event) {
    event.preventDefault();
    
    const errorDiv = document.getElementById('newRequestError');
    const submitBtn = event.target.querySelector('button[type="submit"]');
    
    // Clear previous errors
    if (errorDiv) {
        errorDiv.textContent = '';
        errorDiv.style.display = 'none';
    }
    
    // Get form values
    const patientId = document.getElementById('reqPatientId').value.trim();
    const patientName = document.getElementById('reqPatientName').value.trim();
    const slotDate = document.getElementById('reqDate').value;
    const slotSession = document.getElementById('reqSession').value;
    const slotTime = document.getElementById('reqTime').value;
    const reason = document.getElementById('reqReason').value.trim();
    
    // Validate required fields
    if (!patientId || !patientName || !slotDate || !slotSession || !slotTime) {
        showRequestError('Please fill in all required fields');
        return;
    }
    
    // CRITICAL FIX: Validate past date using date-only comparison
    // Parse both dates as date-only values to avoid timezone shifts
    const today = getToday();
    if (slotDate < today) {
        showRequestError('Cannot book appointments in the past');
        return;
    }
    
    const formData = {
        patient_id: patientId,
        patient_name: patientName,
        follow_up_reason: reason || null,
        slot_date: slotDate,
        slot_session: slotSession,
        slot_time: slotTime
    };
    
    try {
        // Disable submit button
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.textContent = 'Creating...';
        }
        
        await refreshSession();
        const data = await apiCall('/appointments', {
            method: 'POST',
            headers: {'X-CSRF-Token': csrfToken},
            body: JSON.stringify(formData)
        });
        
        if (data.success) {
            showAlert('Follow-up request created successfully', 'success');
            closeNewRequestModal();
            await loadData();
        } else {
            showRequestError(data.error || 'Failed to create request');
        }
    } catch (error) {
        showRequestError('Error: ' + error.message);
    } finally {
        // Re-enable submit button
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Create Request';
        }
    }
}

function showRequestError(message) {
    const errorDiv = document.getElementById('newRequestError');
    if (errorDiv) {
        errorDiv.textContent = message;
        errorDiv.style.display = 'block';
    } else {
        showAlert(message, 'error');
    }
}

async function loadData() {
    try {
        // Load all appointments for the month
        const startDate = new Date(currentDate.getFullYear(), currentDate.getMonth(), 1);
        const endDate = new Date(currentDate.getFullYear(), currentDate.getMonth() + 1, 0);
        
        const data = await apiCall(
            `/appointments?start_date=${formatDate(startDate)}&end_date=${formatDate(endDate)}`
        );
        
        appointments = data.appointments || [];
        renderCalendar();
        
        if (selectedDate) {
            showDaySchedule();
        }
    } catch (error) {
        showAlert('Failed to load appointments', 'error');
    }
}

// Event Handlers
document.getElementById('prevMonthBtn').addEventListener('click', () => {
    currentDate = new Date(currentDate.getFullYear(), currentDate.getMonth() - 1, 1);
    loadData();
});

document.getElementById('nextMonthBtn').addEventListener('click', () => {
    currentDate = new Date(currentDate.getFullYear(), currentDate.getMonth() + 1, 1);
    loadData();
});

document.getElementById('todayBtn').addEventListener('click', () => {
    currentDate = new Date();
    selectedDate = new Date();
    loadData();
});

document.getElementById('closeDayViewBtn').addEventListener('click', () => {
    document.getElementById('daySchedule').classList.add('hidden');
    selectedDate = null;
    renderCalendar();
});

document.getElementById('refreshBtn').addEventListener('click', loadData);
document.getElementById('newRequestBtn').addEventListener('click', openNewRequestModal);
document.getElementById('settingsBtn').addEventListener('click', openSettingsModal);
document.getElementById('logoutBtn').addEventListener('click', async () => {
    try {
        await fetch('/api/calendar/logout', {
            method: 'POST',
            credentials: 'same-origin'
        });
    } catch (error) {
        console.error('Logout error:', error);
    }
    // Redirect to login page (session cleared server-side)
    window.location.href = '/staff/login';
});

document.getElementById('newRequestForm').addEventListener('submit', createNewRequest);
document.getElementById('settingsForm').addEventListener('submit', saveSettings);
document.getElementById('reqDate').addEventListener('change', updateAvailableSlots);
document.getElementById('reqSession').addEventListener('change', updateAvailableSlots);

// Initialize
(async () => {
    const authenticated = await refreshSession();
    if (authenticated) {
        document.getElementById('userDisplay').textContent = 
            `${currentUser.username} (${currentUser.role})`;
        await loadSettings();
        await loadData();
    }
})();
