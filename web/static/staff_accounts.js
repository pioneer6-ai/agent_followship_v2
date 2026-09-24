// State
let currentUser = null;
let csrfToken = null;
let accounts = [];

// API helpers
async function apiCall(endpoint, options = {}) {
    const url = `/api/staff${endpoint}`;
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
        throw new Error(data.message || 'Access forbidden - Admin only');
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
        
        // Check if user is admin
        if (currentUser.role !== 'admin') {
            alert('Access denied. Admin privileges required.');
            window.location.href = '/staff/calendar';
            return false;
        }
        
        return true;
    } catch (error) {
        const returnUrl = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.href = `/staff/login?next=${returnUrl}`;
        return false;
    }
}

// UI Functions
function showError(elementId, message) {
    const errorDiv = document.getElementById(elementId);
    if (errorDiv) {
        errorDiv.textContent = message;
        errorDiv.classList.add('show');
    }
}

function hideError(elementId) {
    const errorDiv = document.getElementById(elementId);
    if (errorDiv) {
        errorDiv.classList.remove('show');
    }
}

async function loadAccounts() {
    try {
        const data = await apiCall('/accounts');
        accounts = data.accounts || [];
        renderAccountsTable();
    } catch (error) {
        document.getElementById('accountsTable').innerHTML = 
            `<div class="error-message show">Error loading accounts: ${error.message}</div>`;
    }
}

function renderAccountsTable() {
    const container = document.getElementById('accountsTable');
    
    if (accounts.length === 0) {
        container.innerHTML = '<div class="empty-state">No staff accounts found</div>';
        return;
    }
    
    const html = `
        <table>
            <thead>
                <tr>
                    <th>Username</th>
                    <th>Role</th>
                    <th>Status</th>
                    <th>Last Login</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                ${accounts.map(account => `
                    <tr>
                        <td>${escapeHtml(account.username)}</td>
                        <td><span class="badge badge-${account.role}">${account.role}</span></td>
                        <td><span class="badge badge-${account.is_active ? 'active' : 'inactive'}">
                            ${account.is_active ? 'Active' : 'Inactive'}
                        </span></td>
                        <td>${account.last_login ? new Date(account.last_login).toLocaleString() : 'Never'}</td>
                        <td>
                            <button class="btn btn-sm btn-primary" onclick="openEditModal(${account.id})">Edit Role</button>
                            ${account.is_active ? 
                                `<button class="btn btn-sm btn-warning" onclick="deactivateAccount(${account.id}, '${escapeHtml(account.username)}')">Deactivate</button>` :
                                `<button class="btn btn-sm btn-success" onclick="reactivateAccount(${account.id}, '${escapeHtml(account.username)}')">Reactivate</button>`
                            }
                            <button class="btn btn-sm btn-danger" onclick="resetPassword(${account.id}, '${escapeHtml(account.username)}')">Reset Password</button>
                        </td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;
    
    container.innerHTML = html;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Modal Functions
function openCreateModal() {
    document.getElementById('createForm').reset();
    hideError('createError');
    document.getElementById('createModal').classList.add('active');
}

function closeCreateModal() {
    document.getElementById('createModal').classList.remove('active');
}

function openEditModal(accountId) {
    const account = accounts.find(a => a.id === accountId);
    if (!account) return;
    
    document.getElementById('editAccountId').value = account.id;
    document.getElementById('editUsername').value = account.username;
    document.getElementById('editRole').value = account.role;
    hideError('editError');
    document.getElementById('editModal').classList.add('active');
}

function closeEditModal() {
    document.getElementById('editModal').classList.remove('active');
}

// Account Operations
async function createAccount(event) {
    event.preventDefault();
    
    const submitBtn = event.target.querySelector('button[type="submit"]');
    hideError('createError');
    
    const username = document.getElementById('createUsername').value.trim();
    const password = document.getElementById('createPassword').value;
    const role = document.getElementById('createRole').value;
    
    if (!username || !password || !role) {
        showError('createError', 'All fields are required');
        return;
    }
    
    if (password.length < 8) {
        showError('createError', 'Password must be at least 8 characters');
        return;
    }
    
    try {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Creating...';
        
        await refreshSession(); // Get fresh CSRF token
        
        const data = await apiCall('/accounts', {
            method: 'POST',
            headers: {'X-CSRF-Token': csrfToken},
            body: JSON.stringify({username, password, role})
        });
        
        if (data.success) {
            closeCreateModal();
            await loadAccounts();
            alert(`Account created successfully. User must change password on first login.`);
        } else {
            showError('createError', data.error || 'Failed to create account');
        }
    } catch (error) {
        showError('createError', error.message);
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Create Account';
    }
}

async function editAccount(event) {
    event.preventDefault();
    
    const submitBtn = event.target.querySelector('button[type="submit"]');
    hideError('editError');
    
    const accountId = document.getElementById('editAccountId').value;
    const role = document.getElementById('editRole').value;
    
    try {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Saving...';
        
        await refreshSession();
        
        const data = await apiCall(`/accounts/${accountId}/role`, {
            method: 'POST',
            headers: {'X-CSRF-Token': csrfToken},
            body: JSON.stringify({role})
        });
        
        if (data.success) {
            closeEditModal();
            await loadAccounts();
            alert('Role updated successfully');
        } else {
            showError('editError', data.error || 'Failed to update role');
        }
    } catch (error) {
        showError('editError', error.message);
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Save Changes';
    }
}

async function deactivateAccount(accountId, username) {
    if (!confirm(`Deactivate account "${username}"?\n\nThis will immediately log them out and prevent future logins.\nBooking and audit history will be preserved.`)) {
        return;
    }
    
    try {
        await refreshSession();
        
        const data = await apiCall(`/accounts/${accountId}/deactivate`, {
            method: 'POST',
            headers: {'X-CSRF-Token': csrfToken}
        });
        
        if (data.success) {
            await loadAccounts();
            alert('Account deactivated successfully');
        } else {
            alert('Error: ' + (data.error || 'Failed to deactivate account'));
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

async function reactivateAccount(accountId, username) {
    if (!confirm(`Reactivate account "${username}"?`)) {
        return;
    }
    
    try {
        await refreshSession();
        
        const data = await apiCall(`/accounts/${accountId}/reactivate`, {
            method: 'POST',
            headers: {'X-CSRF-Token': csrfToken}
        });
        
        if (data.success) {
            await loadAccounts();
            alert('Account reactivated successfully');
        } else {
            alert('Error: ' + (data.error || 'Failed to reactivate account'));
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

async function resetPassword(accountId, username) {
    const newPassword = prompt(`Reset password for "${username}"?\n\nEnter new temporary password (min 8 characters):`);
    
    if (!newPassword) return;
    
    if (newPassword.length < 8) {
        alert('Password must be at least 8 characters');
        return;
    }
    
    try {
        await refreshSession();
        
        const data = await apiCall(`/accounts/${accountId}/reset-password`, {
            method: 'POST',
            headers: {'X-CSRF-Token': csrfToken},
            body: JSON.stringify({new_password: newPassword})
        });
        
        if (data.success) {
            alert('Password reset successfully. User must change it on next login.');
        } else {
            alert('Error: ' + (data.error || 'Failed to reset password'));
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

// Event Listeners
document.getElementById('createAccountBtn').addEventListener('click', openCreateModal);
document.getElementById('createForm').addEventListener('submit', createAccount);
document.getElementById('editForm').addEventListener('submit', editAccount);
document.getElementById('logoutBtn').addEventListener('click', async () => {
    try {
        await fetch('/api/calendar/logout', {
            method: 'POST',
            credentials: 'same-origin'
        });
    } catch (error) {
        console.error('Logout error:', error);
    }
    window.location.href = '/staff/login';
});

// Initialize
(async () => {
    const authenticated = await refreshSession();
    if (authenticated) {
        await loadAccounts();
    }
})();
