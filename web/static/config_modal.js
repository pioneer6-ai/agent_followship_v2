// Configuration Modal Functions
let currentConfig = null;
let treatmentOverrideCount = 0;

async function showConfigModal() {
    try {
        const response = await fetch('/api/urgency-config');
        const data = await response.json();
        
        if (data.success) {
            currentConfig = data.config;
            loadConfigIntoForm(currentConfig);
            document.getElementById('configModal').style.display = 'flex';
        } else {
            alert('Failed to load configuration: ' + (data.error || 'Unknown error'));
        }
    } catch (error) {
        console.error('Error loading config:', error);
        alert('Failed to load configuration');
    }
}

function closeConfigModal() {
    document.getElementById('configModal').style.display = 'none';
    document.getElementById('previewSection').style.display = 'none';
    clearAlerts('configAlerts');
}

function loadConfigIntoForm(config) {
    // General thresholds
    document.getElementById('threshold_medium').value = config.general_thresholds.medium;
    document.getElementById('threshold_high').value = config.general_thresholds.high;
    document.getElementById('threshold_critical').value = config.general_thresholds.critical;
    
    // Scoring mode
    const useCustomRules = config.use_custom_rules || false;
    document.getElementById(useCustomRules ? "mode_custom" : "mode_baseline").checked = true;
    
    // Escalation rules
    document.getElementById('escalation_enabled').checked = config.escalation_rules.enabled;
    document.getElementById('escalation_threshold').value = config.escalation_rules.consecutive_unanswered_threshold;
    
    // Missed appointment rules
    const missedRules = config.missed_appointment_rules || {enabled: false, count_threshold: 2, min_urgency_level: 'HIGH'};
    document.getElementById('missed_appointment_enabled').checked = missedRules.enabled || false;
    document.getElementById('missed_appointment_threshold').value = missedRules.count_threshold || 2;
    document.getElementById('missed_appointment_min_urgency').value = missedRules.min_urgency_level || 'HIGH';
    
    // Unanswered reminder rules
    const unansweredRules = config.unanswered_reminder_urgency_rules || {enabled: false, count_threshold: 2, min_urgency_level: 'HIGH'};
    document.getElementById('unanswered_reminder_enabled').checked = unansweredRules.enabled || false;
    document.getElementById('unanswered_reminder_threshold').value = unansweredRules.count_threshold || 2;
    document.getElementById('unanswered_reminder_min_urgency').value = unansweredRules.min_urgency_level || 'HIGH';
    
    // Reminder interval
    document.getElementById('reminder_interval').value = config.reminder_interval_days;
    
    // Treatment overrides
    const overridesContainer = document.getElementById('treatmentOverrides');
    overridesContainer.innerHTML = '';
    treatmentOverrideCount = 0;
    
    for (const [treatmentType, overrides] of Object.entries(config.treatment_overrides || {})) {
        addTreatmentOverride(treatmentType, overrides);
    }
}

function addTreatmentOverride(treatmentType = '', overrides = {}) {
    const id = treatmentOverrideCount++;
    const overridesContainer = document.getElementById('treatmentOverrides');
    
    const enabled = overrides.enabled !== undefined ? overrides.enabled : true;
    
    const item = document.createElement('div');
    item.className = 'treatment-override-item';
    item.id = `override-${id}`;
    item.innerHTML = `
        <div class="treatment-override-header">
            <input type="text" placeholder="Treatment type (e.g., post_surgery)" 
                   value="${treatmentType}" 
                   id="override_type_${id}"
                   style="flex: 1; padding: 8px; border: 2px solid #e0e0e0; border-radius: 6px; margin-right: 10px;">
            <label style="margin-right: 10px; font-size: 13px;">
                <input type="checkbox" id="override_enabled_${id}" ${enabled ? 'checked' : ''}>
                Enabled
            </label>
            <button class="btn btn-small btn-danger" onclick="removeTreatmentOverride(${id})">Remove</button>
        </div>
        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-top: 10px;">
            <div>
                <label style="font-size: 12px; color: #666;">Medium</label>
                <input type="number" id="override_medium_${id}" class="config-input" min="1" 
                       value="${overrides.medium || ''}" placeholder="Default" style="width: 100%;">
            </div>
            <div>
                <label style="font-size: 12px; color: #666;">High</label>
                <input type="number" id="override_high_${id}" class="config-input" min="1" 
                       value="${overrides.high || ''}" placeholder="Default" style="width: 100%;">
            </div>
            <div>
                <label style="font-size: 12px; color: #666;">Critical</label>
                <input type="number" id="override_critical_${id}" class="config-input" min="1" 
                       value="${overrides.critical || ''}" placeholder="Default" style="width: 100%;">
            </div>
        </div>
    `;
    
    overridesContainer.appendChild(item);
}

function removeTreatmentOverride(id) {
    const item = document.getElementById(`override-${id}`);
    if (item) item.remove();
}

function getConfigFromForm() {
    const config = {
        general_thresholds: {
            medium: parseInt(document.getElementById('threshold_medium').value),
            high: parseInt(document.getElementById('threshold_high').value),
            critical: parseInt(document.getElementById('threshold_critical').value)
        },
        treatment_overrides: {},
        escalation_rules: {
            enabled: document.getElementById('escalation_enabled').checked,
            consecutive_unanswered_threshold: parseInt(document.getElementById('escalation_threshold').value)
        },
        missed_appointment_rules: {
            enabled: document.getElementById('missed_appointment_enabled').checked,
            count_threshold: parseInt(document.getElementById('missed_appointment_threshold').value),
            min_urgency_level: document.getElementById('missed_appointment_min_urgency').value
        },
        unanswered_reminder_urgency_rules: {
            enabled: document.getElementById('unanswered_reminder_enabled').checked,
            count_threshold: parseInt(document.getElementById('unanswered_reminder_threshold').value),
            min_urgency_level: document.getElementById('unanswered_reminder_min_urgency').value
        },
        reminder_interval_days: parseInt(document.getElementById('reminder_interval').value),
        use_custom_rules: document.getElementById('mode_custom').checked
    };
    
    const overridesContainer = document.getElementById('treatmentOverrides');
    const overrideItems = overridesContainer.querySelectorAll('.treatment-override-item');
    
    overrideItems.forEach(item => {
        const id = item.id.split('-')[1];
        const treatmentType = document.getElementById(`override_type_${id}`).value.trim();
        
        if (treatmentType) {
            const overrides = {
                enabled: document.getElementById(`override_enabled_${id}`).checked
            };
            const medium = document.getElementById(`override_medium_${id}`).value;
            const high = document.getElementById(`override_high_${id}`).value;
            const critical = document.getElementById(`override_critical_${id}`).value;
            
            if (medium) overrides.medium = parseInt(medium);
            if (high) overrides.high = parseInt(high);
            if (critical) overrides.critical = parseInt(critical);
            
            config.treatment_overrides[treatmentType] = overrides;
        }
    });
    
    return config;
}

async function previewConfig() {
    clearAlerts('configAlerts');
    const config = getConfigFromForm();
    
    try {
        const response = await fetch('/api/urgency-config/preview', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        });
        
        const data = await response.json();
        
        if (!data.success) {
            if (data.validation_errors) {
                showAlert('configAlerts', 'error', 'Validation errors: ' + data.validation_errors.join(', '));
            } else {
                showAlert('configAlerts', 'error', 'Preview failed: ' + (data.error || 'Unknown error'));
            }
            return;
        }
        
        displayPreview(data.preview);
    } catch (error) {
        console.error('Error previewing config:', error);
        showAlert('configAlerts', 'error', 'Failed to preview configuration');
    }
}

function displayPreview(preview) {
    const previewSection = document.getElementById('previewSection');
    const previewContent = document.getElementById('previewContent');
    
    const summary = preview.summary;
    const changes = preview.changes;
    
    let html = `
        <div class="preview-summary">
            <div class="preview-summary-row">
                <strong>Total Patients:</strong>
                <span>${summary.total_patients}</span>
            </div>
            <div class="preview-summary-row">
                <strong>Urgency Increased:</strong>
                <span style="color: #e53e3e;">${summary.increased}</span>
            </div>
            <div class="preview-summary-row">
                <strong>Urgency Decreased:</strong>
                <span style="color: #38a169;">${summary.decreased}</span>
            </div>
            <div class="preview-summary-row">
                <strong>Unchanged:</strong>
                <span>${summary.unchanged}</span>
            </div>
        </div>
    `;
    
    const changedCases = changes.filter(c => c.changed);
    
    if (changedCases.length === 0) {
        html += '<div class="empty-state">No urgency changes detected</div>';
    } else {
        html += changedCases.map(c => `
            <div class="preview-item ${c.increased ? 'increased' : (c.decreased ? 'decreased' : '')}">
                <div class="preview-item-header">
                    ${c.patient_name} (${c.days_overdue} days overdue)
                </div>
                <div class="preview-change">
                    ${c.old_urgency.toUpperCase()} -> ${c.new_urgency.toUpperCase()}
                    ${c.increased ? 'increased' : 'decreased'}
                </div>
                <div style="font-size: 11px; color: #999; margin-top: 5px;">
                    ${c.new_explanation}
                </div>
            </div>
        `).join('');
    }
    
    previewContent.innerHTML = html;
    previewSection.style.display = 'block';
}

async function saveConfig() {
    clearAlerts('configAlerts');
    const config = getConfigFromForm();
    
    try {
        const response = await fetch('/api/urgency-config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        });
        
        const data = await response.json();
        
        if (data.success) {
            showAlert('configAlerts', 'success', '? Configuration saved and applied successfully!');
            currentConfig = config;
            
            setTimeout(() => {
                closeConfigModal();
                refreshData();
            }, 2000);
        } else {
            if (data.validation_errors) {
                showAlert('configAlerts', 'error', 'Validation errors: ' + data.validation_errors.join(', '));
            } else {
                showAlert('configAlerts', 'error', 'Save failed: ' + (data.error || 'Unknown error'));
            }
        }
    } catch (error) {
        console.error('Error saving config:', error);
        showAlert('configAlerts', 'error', 'Failed to save configuration');
    }
}

async function restoreDefaults() {
    if (!confirm('Are you sure you want to restore default configuration? This will overwrite your current settings.')) {
        return;
    }
    
    const defaultConfig = {
        general_thresholds: {medium: 14, high: 30, critical: 60},
        treatment_overrides: {},
        escalation_rules: {enabled: true, consecutive_unanswered_threshold: 3},
        missed_appointment_rules: {enabled: false, count_threshold: 2, min_urgency_level: 'HIGH'},
        unanswered_reminder_urgency_rules: {enabled: false, count_threshold: 2, min_urgency_level: 'HIGH'},
        reminder_interval_days: 7
    };
    
    loadConfigIntoForm(defaultConfig);
    document.getElementById('previewSection').style.display = 'none';
    showAlert('configAlerts', 'warning', 'Default configuration loaded. Click Save to apply.');
}

function showAlert(containerId, type, message) {
    const container = document.getElementById(containerId);
    const alert = document.createElement('div');
    alert.className = `alert alert-${type}`;
    alert.textContent = message;
    container.appendChild(alert);
}

function clearAlerts(containerId) {
    document.getElementById(containerId).innerHTML = '';
}
