from datetime import date
from typing import Optional, Tuple
from core.models import PatientRecord, FollowUpCase, UrgencyLevel
from core.config import ClinicPolicyConfig
from core.urgency_config import UrgencyRulesConfig

class RecallRuleEngine:
    def __init__(self, config: ClinicPolicyConfig):
        self.config = config

    def compute_overdue_patients(self, patients: list[PatientRecord], as_of: date) -> list[FollowUpCase]:
        overdue_cases = []
        for patient in patients:
            days_since_visit = (as_of - patient.last_visit_date).days
            days_overdue = days_since_visit - patient.recall_interval_days
            if days_overdue > 0:
                reason = self._generate_reason(patient, days_overdue)
                case = FollowUpCase(patient=patient, days_overdue=days_overdue, urgency=UrgencyLevel.LOW, reason=reason)
                overdue_cases.append(case)
        return overdue_cases

    def _generate_reason(self, patient: PatientRecord, days_overdue: int) -> str:
        return f"Patient {patient.name} (ID: {patient.patient_id}) is {days_overdue} days overdue for {patient.treatment_type} follow-up. Last visit: {patient.last_visit_date.isoformat()}, recommended interval: {patient.recall_interval_days} days."



class LegacyUrgencyScorer:
    """
    Baseline urgency scorer from ba79e64.
    
    Preserves the original scoring algorithm that uses ClinicPolicyConfig
    with treatment type weights and no-show history multipliers.
    """
    def __init__(self, policy: ClinicPolicyConfig):
        self.config = policy
        self._treatment_weights = {
            "post_surgery": 2.0,
            "orthodontic_adjustment": 1.5,
            "cavity_treatment": 1.5,
            "root_canal_followup": 1.8,
            "periodontal_maintenance": 1.3,
            "cleaning": 1.0,
            "checkup": 0.8,
        }
    
    def score(self, patient: PatientRecord, days_overdue: int, consecutive_unanswered: int = 0) -> Optional[Tuple[UrgencyLevel, str]]:
        """Score using baseline algorithm (treatment weights + no-show multiplier)."""
        if days_overdue <= 0:
            return None
        
        # Apply treatment type weight
        treatment_weight = self._treatment_weights.get(patient.treatment_type.lower(), 1.0)
        weighted_days = days_overdue * treatment_weight
        
        # Adjust for patient reliability (no-show history)
        if patient.no_show_history > 2:
            weighted_days *= 1.2
        
        # Determine urgency based on weighted overdue days
        if weighted_days >= self.config.critical_urgency_threshold_days:
            urgency = UrgencyLevel.CRITICAL
            explanation = f"CRITICAL: {days_overdue} days overdue for {patient.treatment_type} (weighted: {weighted_days:.1f} days >= {self.config.critical_urgency_threshold_days})"
        elif weighted_days >= self.config.high_urgency_threshold_days:
            urgency = UrgencyLevel.HIGH
            explanation = f"HIGH: {days_overdue} days overdue for {patient.treatment_type} (weighted: {weighted_days:.1f} days >= {self.config.high_urgency_threshold_days})"
        elif weighted_days >= 14:
            urgency = UrgencyLevel.MEDIUM
            explanation = f"MEDIUM: {days_overdue} days overdue for {patient.treatment_type} (weighted: {weighted_days:.1f} days >= 14)"
        else:
            urgency = UrgencyLevel.LOW
            explanation = f"LOW: {days_overdue} days overdue for {patient.treatment_type} (weighted: {weighted_days:.1f} days)"
        
        return (urgency, explanation)

class UrgencyScorer:
    def __init__(self, urgency_config: UrgencyRulesConfig, policy: Optional[ClinicPolicyConfig] = None):
        self.config = urgency_config
        self.policy = policy or ClinicPolicyConfig.from_env()
        self._legacy_scorer = LegacyUrgencyScorer(self.policy) if not urgency_config.use_custom_rules else None

    def score(self, patient: PatientRecord, days_overdue: int, consecutive_unanswered: int = 0) -> Optional[Tuple[UrgencyLevel, str]]:
        # Delegate to legacy scorer if custom rules are disabled
        if self._legacy_scorer is not None:
            return self._legacy_scorer.score(patient, days_overdue, consecutive_unanswered)
        
        if days_overdue <= 0:
            return None
        
        # Check for treatment-specific override (must be enabled)
        treatment = patient.treatment_type
        thresholds = self.config.general_thresholds
        override_used = False
        
        if treatment in self.config.treatment_overrides:
            override = self.config.treatment_overrides[treatment]
            if override.get('enabled', False):
                thresholds = override
                override_used = True
        
        # Determine base urgency from thresholds
        if days_overdue >= thresholds['critical']:
            base_urgency = UrgencyLevel.CRITICAL
            threshold_name = 'critical'
            threshold_value = thresholds['critical']
        elif days_overdue >= thresholds['high']:
            base_urgency = UrgencyLevel.HIGH
            threshold_name = 'high'
            threshold_value = thresholds['high']
        elif days_overdue >= thresholds['medium']:
            base_urgency = UrgencyLevel.MEDIUM
            threshold_name = 'medium'
            threshold_value = thresholds['medium']
        else:
            base_urgency = UrgencyLevel.LOW
            threshold_name = 'low'
            threshold_value = thresholds['medium']  # Use medium threshold as reference for low
        
        final_urgency = base_urgency
        urgency_rules_applied = []
        
        # Apply missed appointment rule
        if self.config.missed_appointment_rules.get('enabled', False):
            count_thresh = self.config.missed_appointment_rules.get('count_threshold', 0)
            if patient.no_show_history >= count_thresh:
                min_level_str = self.config.missed_appointment_rules.get('min_urgency_level', 'HIGH')
                min_level = UrgencyLevel[min_level_str]
                if self._urgency_priority(min_level) > self._urgency_priority(final_urgency):
                    final_urgency = min_level
                    urgency_rules_applied.append(f"missed appointment rule ({patient.no_show_history} no-shows, threshold: {count_thresh})")
        
        # Apply unanswered reminder rule
        if self.config.unanswered_reminder_urgency_rules.get('enabled', False):
            count_thresh = self.config.unanswered_reminder_urgency_rules.get('count_threshold', 0)
            if consecutive_unanswered >= count_thresh:
                min_level_str = self.config.unanswered_reminder_urgency_rules.get('min_urgency_level', 'HIGH')
                min_level = UrgencyLevel[min_level_str]
                if self._urgency_priority(min_level) > self._urgency_priority(final_urgency):
                    final_urgency = min_level
                    urgency_rules_applied.append(f"unanswered reminder rule ({consecutive_unanswered} unanswered, threshold: {count_thresh})")
        
        # Generate explanation
        explanation_parts = []
        if override_used:
            explanation_parts.append(f"{final_urgency.value.upper()}: {days_overdue} days overdue for {treatment} ({threshold_name.capitalize()} threshold: {threshold_value} days)")
        else:
            if threshold_name == 'low':
                explanation_parts.append(f"{final_urgency.value.upper()}: {days_overdue} days overdue (below Medium threshold of {threshold_value} days)")
            else:
                explanation_parts.append(f"{final_urgency.value.upper()}: {days_overdue} days overdue ({threshold_name.capitalize()} threshold: {threshold_value} days)")
        
        if urgency_rules_applied:
            explanation_parts.append(f"Elevated by {', '.join(urgency_rules_applied)}")
        
        explanation = '. '.join(explanation_parts)
        return (final_urgency, explanation)
    
    def _urgency_priority(self, urgency: UrgencyLevel) -> int:
        priority_map = {UrgencyLevel.LOW: 1, UrgencyLevel.MEDIUM: 2, UrgencyLevel.HIGH: 3, UrgencyLevel.CRITICAL: 4}
        return priority_map[urgency]

    def sort_by_urgency(self, cases: list[FollowUpCase]) -> list[FollowUpCase]:
        urgency_priority = {UrgencyLevel.CRITICAL: 4, UrgencyLevel.HIGH: 3, UrgencyLevel.MEDIUM: 2, UrgencyLevel.LOW: 1}
        return sorted(cases, key=lambda c: (urgency_priority[c.urgency], c.days_overdue), reverse=True)
