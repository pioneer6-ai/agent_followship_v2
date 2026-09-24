from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum

class EpisodeStatus(Enum):
    """Episode lifecycle state - ACTIVE or HISTORICAL"""
    ACTIVE = "active"
    HISTORICAL = "historical"

@dataclass
class UrgencyRulesConfig:
    general_thresholds: dict[str, int] = field(default_factory=dict)
    treatment_overrides: dict[str, dict] = field(default_factory=dict)
    reminder_interval_days: int = 7
    missed_appointment_rules: dict = field(default_factory=dict)
    unanswered_reminder_urgency_rules: dict = field(default_factory=dict)
    escalation_rules: dict = field(default_factory=dict)
    use_custom_rules: bool = False
    
    @classmethod
    def get_default(cls):
        return cls(
            general_thresholds={'medium': 14, 'high': 30, 'critical': 60},
            treatment_overrides={},
            reminder_interval_days=7,
            missed_appointment_rules={'enabled': False, 'count_threshold': 3, 'min_urgency_level': 'HIGH'},
            unanswered_reminder_urgency_rules={'enabled': False, 'count_threshold': 2, 'min_urgency_level': 'HIGH'},
            escalation_rules={'enabled': True, 'consecutive_unanswered_threshold': 3},
            use_custom_rules=False
        )
    
    def validate(self):
        errors = []
        # Validate use_custom_rules is boolean
        if not isinstance(self.use_custom_rules, bool):
            errors.append('use_custom_rules must be a boolean')
        required_keys = {'medium', 'high', 'critical'}
        if not all(key in self.general_thresholds for key in required_keys):
            errors.append("General thresholds must include medium, high, and critical")
            return (False, errors)
        
        for key, value in self.general_thresholds.items():
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"General threshold {key} must be an integer")
            elif value <= 0:
                errors.append(f"General threshold {key} must be positive")
        
        medium = self.general_thresholds.get('medium', 0)
        high = self.general_thresholds.get('high', 0)
        critical = self.general_thresholds.get('critical', 0)
        
        if isinstance(medium, int) and isinstance(high, int) and not isinstance(medium, bool) and not isinstance(high, bool):
            if medium >= high:
                errors.append(f"Medium ({medium}) must be less than High ({high})")
        
        if isinstance(high, int) and isinstance(critical, int) and not isinstance(high, bool) and not isinstance(critical, bool):
            if high >= critical:
                errors.append(f"High ({high}) must be less than Critical ({critical})")
        
        if not isinstance(self.reminder_interval_days, int) or isinstance(self.reminder_interval_days, bool):
            errors.append("Reminder interval must be an integer")
        elif self.reminder_interval_days <= 0:
            errors.append("Reminder interval must be positive")
        
        if not isinstance(self.missed_appointment_rules, dict):
            errors.append("Missed appointment rules must be a dict")
        else:
            if 'enabled' in self.missed_appointment_rules:
                if not isinstance(self.missed_appointment_rules['enabled'], bool):
                    errors.append("Missed appointment enabled must be boolean")
            if self.missed_appointment_rules.get('enabled', False):
                ct = self.missed_appointment_rules.get('count_threshold')
                if ct is not None:
                    if not isinstance(ct, int) or isinstance(ct, bool):
                        errors.append("Missed appointment count_threshold must be integer")
                    elif ct <= 0:
                        errors.append("Missed appointment count_threshold must be positive")
                min_urg = self.missed_appointment_rules.get('min_urgency_level', '')
                if min_urg not in {'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'}:
                    errors.append(f"Invalid min_urgency_level: {min_urg}")
        
        if not isinstance(self.unanswered_reminder_urgency_rules, dict):
            errors.append("Unanswered reminder rules must be a dict")
        else:
            if 'enabled' in self.unanswered_reminder_urgency_rules:
                if not isinstance(self.unanswered_reminder_urgency_rules['enabled'], bool):
                    errors.append("Unanswered reminder enabled must be boolean")
            if self.unanswered_reminder_urgency_rules.get('enabled', False):
                ct = self.unanswered_reminder_urgency_rules.get('count_threshold')
                if ct is not None:
                    if not isinstance(ct, int) or isinstance(ct, bool):
                        errors.append("Unanswered reminder count_threshold must be integer")
                    elif ct <= 0:
                        errors.append("Unanswered reminder count_threshold must be positive")
                min_urg = self.unanswered_reminder_urgency_rules.get('min_urgency_level', '')
                if min_urg not in {'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'}:
                    errors.append(f"Invalid min_urgency_level: {min_urg}")
        
        if not isinstance(self.escalation_rules, dict):
            errors.append("Escalation rules must be a dict")
        else:
            if 'enabled' in self.escalation_rules:
                if not isinstance(self.escalation_rules['enabled'], bool):
                    errors.append("Escalation enabled must be boolean")
            if self.escalation_rules.get('enabled', True):
                thresh = self.escalation_rules.get('consecutive_unanswered_threshold')
                if thresh is not None:
                    if not isinstance(thresh, int) or isinstance(thresh, bool):
                        errors.append("Escalation threshold must be integer")
                    elif thresh <= 0:
                        errors.append("Escalation threshold must be positive")
        
        for treatment, override in self.treatment_overrides.items():
            if not isinstance(override, dict):
                errors.append(f"Treatment override for {treatment} must be dict")
                continue
            if override.get('enabled', False):
                if not all(key in override for key in ['medium', 'high', 'critical']):
                    errors.append(f"Treatment {treatment} must include medium, high, critical")
                    continue
                for key in ['medium', 'high', 'critical']:
                    value = override.get(key)
                    if not isinstance(value, int) or isinstance(value, bool):
                        errors.append(f"Treatment {treatment}.{key} must be integer")
                    elif value <= 0:
                        errors.append(f"Treatment {treatment}.{key} must be positive")
                t_medium = override.get('medium', 0)
                t_high = override.get('high', 0)
                t_critical = override.get('critical', 0)
                if isinstance(t_medium, int) and isinstance(t_high, int) and not isinstance(t_medium, bool) and not isinstance(t_high, bool):
                    if t_medium >= t_high:
                        errors.append(f"Treatment {treatment}: medium must be less than high")
                if isinstance(t_high, int) and isinstance(t_critical, int) and not isinstance(t_high, bool) and not isinstance(t_critical, bool):
                    if t_high >= t_critical:
                        errors.append(f"Treatment {treatment}: high must be less than critical")
        
        return (len(errors) == 0, errors)
    
    def to_dict(self):
        return {
            'general_thresholds': self.general_thresholds.copy(),
            'treatment_overrides': {k: v.copy() for k, v in self.treatment_overrides.items()},
            'reminder_interval_days': self.reminder_interval_days,
            'missed_appointment_rules': self.missed_appointment_rules.copy(),
            'unanswered_reminder_urgency_rules': self.unanswered_reminder_urgency_rules.copy(),
            'escalation_rules': self.escalation_rules.copy(),
            'use_custom_rules': self.use_custom_rules
        }
    
    @classmethod
    def from_dict(cls, data):
        return cls(
            general_thresholds=data.get('general_thresholds', {}).copy(),
            treatment_overrides={k: v.copy() for k, v in data.get('treatment_overrides', {}).items()},
            reminder_interval_days=data.get('reminder_interval_days', 7),
            missed_appointment_rules=data.get('missed_appointment_rules', {}).copy(),
            unanswered_reminder_urgency_rules=data.get('unanswered_reminder_urgency_rules', {}).copy(),
            escalation_rules=data.get('escalation_rules', {}).copy(),
            use_custom_rules=data.get('use_custom_rules', False)
        )
