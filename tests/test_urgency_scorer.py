import pytest
from datetime import date
from core.models import PatientRecord, UrgencyLevel, ContactChannel
from core.urgency_config import UrgencyRulesConfig
from agent.business_rules import UrgencyScorer

def test_score_returns_none_for_non_overdue():
    config = UrgencyRulesConfig.get_default()
    config.use_custom_rules = True  # Enable custom scoring for this test
    scorer = UrgencyScorer(config)
    patient = PatientRecord(
        patient_id="P001", name="Test", contact_info={}, 
        preferred_channel=ContactChannel.SMS, last_visit_date=date(2024, 1, 1),
        treatment_type="cleaning", recall_interval_days=180, no_show_history=0
    )
    result = scorer.score(patient, 0)
    assert result is None
    result = scorer.score(patient, -5)
    assert result is None

def test_score_uses_general_thresholds():
    config = UrgencyRulesConfig.get_default()
    config.use_custom_rules = True  # Enable custom scoring for this test
    scorer = UrgencyScorer(config)
    patient = PatientRecord(
        patient_id="P001", name="Test", contact_info={}, 
        preferred_channel=ContactChannel.SMS, last_visit_date=date(2024, 1, 1),
        treatment_type="cleaning", recall_interval_days=180, no_show_history=0
    )
    
    urgency, explanation = scorer.score(patient, 5)
    assert urgency == UrgencyLevel.LOW
    assert "5 days overdue" in explanation
    
    urgency, explanation = scorer.score(patient, 14)
    assert urgency == UrgencyLevel.MEDIUM
    assert "14 days overdue" in explanation
    
    urgency, explanation = scorer.score(patient, 30)
    assert urgency == UrgencyLevel.HIGH
    assert "30 days overdue" in explanation
    
    urgency, explanation = scorer.score(patient, 60)
    assert urgency == UrgencyLevel.CRITICAL
    assert "60 days overdue" in explanation

def test_score_uses_treatment_overrides():
    config = UrgencyRulesConfig.get_default()
    config.use_custom_rules = True  # Enable custom scoring for this test
    config.treatment_overrides = {
        'post_surgery': {'enabled': True, 'medium': 7, 'high': 14, 'critical': 30}
    }
    scorer = UrgencyScorer(config)
    patient = PatientRecord(
        patient_id="P001", name="Test", contact_info={}, 
        preferred_channel=ContactChannel.SMS, last_visit_date=date(2024, 1, 1),
        treatment_type="post_surgery", recall_interval_days=180, no_show_history=0
    )
    
    urgency, explanation = scorer.score(patient, 7)
    assert urgency == UrgencyLevel.MEDIUM
    assert "post_surgery" in explanation
    
    urgency, explanation = scorer.score(patient, 14)
    assert urgency == UrgencyLevel.HIGH

def test_missed_appointment_rule_elevates_urgency():
    config = UrgencyRulesConfig.get_default()
    config.use_custom_rules = True  # Enable custom scoring for this test
    config.missed_appointment_rules = {'enabled': True, 'count_threshold': 2, 'min_urgency_level': 'HIGH'}
    scorer = UrgencyScorer(config)
    patient = PatientRecord(
        patient_id="P001", name="Test", contact_info={}, 
        preferred_channel=ContactChannel.SMS, last_visit_date=date(2024, 1, 1),
        treatment_type="cleaning", recall_interval_days=180, no_show_history=3
    )
    
    urgency, explanation = scorer.score(patient, 5)
    assert urgency == UrgencyLevel.HIGH
    assert "missed appointment rule" in explanation


def test_unanswered_reminder_rule_elevates_urgency():
    config = UrgencyRulesConfig.get_default()
    config.use_custom_rules = True  # Enable custom scoring for this test
    config.unanswered_reminder_urgency_rules = {'enabled': True, 'count_threshold': 2, 'min_urgency_level': 'HIGH'}
    scorer = UrgencyScorer(config)
    patient = PatientRecord(
        patient_id="P001", name="Test", contact_info={}, 
        preferred_channel=ContactChannel.SMS, last_visit_date=date(2024, 1, 1),
        treatment_type="cleaning", recall_interval_days=180, no_show_history=0
    )
    
    # Low base urgency, no elevation (consecutive_unanswered = 1)
    urgency, explanation = scorer.score(patient, 5, consecutive_unanswered=1)
    assert urgency == UrgencyLevel.LOW
    
    # Low base urgency, elevated to HIGH (consecutive_unanswered = 2)
    urgency, explanation = scorer.score(patient, 5, consecutive_unanswered=2)
    assert urgency == UrgencyLevel.HIGH
    assert "unanswered reminder rule" in explanation
    assert "2 unanswered" in explanation

def test_treatment_override_disabled_uses_general_thresholds():
    config = UrgencyRulesConfig.get_default()
    config.use_custom_rules = True  # Enable custom scoring for this test
    config.treatment_overrides = {
        'post_surgery': {'enabled': False, 'medium': 7, 'high': 14, 'critical': 30}
    }
    scorer = UrgencyScorer(config)
    patient = PatientRecord(
        patient_id="P001", name="Test", contact_info={}, 
        preferred_channel=ContactChannel.SMS, last_visit_date=date(2024, 1, 1),
        treatment_type="post_surgery", recall_interval_days=180, no_show_history=0
    )
    
    # Should use general threshold (medium=14), not override (medium=7)
    urgency, explanation = scorer.score(patient, 7)
    assert urgency == UrgencyLevel.LOW  # Below general medium threshold of 14
    assert "post_surgery" not in explanation  # Override not applied
    
    urgency, explanation = scorer.score(patient, 14)
    assert urgency == UrgencyLevel.MEDIUM

def test_low_urgency_explanation_does_not_crash():
    config = UrgencyRulesConfig.get_default()
    config.use_custom_rules = True  # Enable custom scoring for this test
    scorer = UrgencyScorer(config)
    patient = PatientRecord(
        patient_id="P001", name="Test", contact_info={}, 
        preferred_channel=ContactChannel.SMS, last_visit_date=date(2024, 1, 1),
        treatment_type="cleaning", recall_interval_days=180, no_show_history=0
    )
    
    # Low urgency (days_overdue < medium threshold)
    urgency, explanation = scorer.score(patient, 5)
    assert urgency == UrgencyLevel.LOW
    assert "below Medium threshold" in explanation
    assert "5 days overdue" in explanation
