"""
Regression tests for baseline urgency scoring behavior.

These tests verify that the default scoring mode (use_custom_rules=False)
preserves the exact ba79e64 algorithm including treatment weights,
no-show multipliers, and ClinicPolicyConfig thresholds.
"""
from datetime import date
from core.models import PatientRecord, ContactChannel, UrgencyLevel
from core.config import ClinicPolicyConfig
from core.urgency_config import UrgencyRulesConfig
from agent.business_rules import UrgencyScorer


def test_baseline_uses_treatment_weights():
    """Baseline mode applies treatment type weights (e.g., post_surgery=2.0x)."""
    config = UrgencyRulesConfig.get_default()
    # Default: use_custom_rules=False, so baseline algorithm is used
    assert config.use_custom_rules == False
    
    policy = ClinicPolicyConfig(
        high_urgency_threshold_days=30,
        critical_urgency_threshold_days=60
    )
    scorer = UrgencyScorer(config, policy)
    
    patient = PatientRecord(
        patient_id="TEST001",
        name="Test Patient",
        contact_info={ContactChannel.SMS: "+15551234567"},
        preferred_channel=ContactChannel.SMS,
        last_visit_date=date(2024, 1, 1),
        treatment_type="post_surgery",  # 2.0x weight
        recall_interval_days=180,
        no_show_history=0
    )
    
    # 16 days overdue * 2.0 weight = 32 weighted days >= HIGH threshold (30)
    result = scorer.score(patient, days_overdue=16, consecutive_unanswered=0)
    assert result is not None
    urgency, explanation = result
    assert urgency == UrgencyLevel.HIGH, f"Expected HIGH for post_surgery at 16 days (weighted 32), got {urgency}"
    assert "post_surgery" in explanation
    assert "weighted" in explanation.lower()


def test_baseline_uses_no_show_multiplier():
    """Baseline mode applies 1.2x multiplier when no_show_history > 2."""
    config = UrgencyRulesConfig.get_default()
    policy = ClinicPolicyConfig(
        high_urgency_threshold_days=30,
        critical_urgency_threshold_days=60
    )
    scorer = UrgencyScorer(config, policy)
    
    patient = PatientRecord(
        patient_id="TEST002",
        name="Test Patient",
        contact_info={ContactChannel.SMS: "+15551234567"},
        preferred_channel=ContactChannel.SMS,
        last_visit_date=date(2024, 1, 1),
        treatment_type="cleaning",  # 1.0x weight
        recall_interval_days=180,
        no_show_history=3  # Triggers 1.2x multiplier
    )
    
    # 26 days overdue * 1.0 weight * 1.2 no-show = 31.2 weighted days >= HIGH threshold (30)
    result = scorer.score(patient, days_overdue=26, consecutive_unanswered=0)
    assert result is not None
    urgency, explanation = result
    assert urgency == UrgencyLevel.HIGH, f"Expected HIGH for 3 no-shows at 26 days (weighted 31.2), got {urgency}"


def test_baseline_respects_environment_thresholds():
    """Baseline mode uses ClinicPolicyConfig thresholds from environment."""
    config = UrgencyRulesConfig.get_default()
    
    # Custom policy thresholds different from default
    policy = ClinicPolicyConfig(
        high_urgency_threshold_days=20,  # Lower than default 30
        critical_urgency_threshold_days=40  # Lower than default 60
    )
    scorer = UrgencyScorer(config, policy)
    
    patient = PatientRecord(
        patient_id="TEST003",
        name="Test Patient",
        contact_info={ContactChannel.SMS: "+15551234567"},
        preferred_channel=ContactChannel.SMS,
        last_visit_date=date(2024, 1, 1),
        treatment_type="cleaning",  # 1.0x weight
        recall_interval_days=180,
        no_show_history=0
    )
    
    # 21 days overdue with 1.0 weight = 21 weighted days
    # Should be HIGH with custom threshold (20) but would be MEDIUM with default (30)
    result = scorer.score(patient, days_overdue=21, consecutive_unanswered=0)
    assert result is not None
    urgency, explanation = result
    assert urgency == UrgencyLevel.HIGH, f"Expected HIGH with threshold 20, got {urgency}"


def test_baseline_medium_threshold_is_14_days():
    """Baseline mode uses hardcoded 14-day threshold for MEDIUM."""
    config = UrgencyRulesConfig.get_default()
    policy = ClinicPolicyConfig()
    scorer = UrgencyScorer(config, policy)
    
    patient = PatientRecord(
        patient_id="TEST004",
        name="Test Patient",
        contact_info={ContactChannel.SMS: "+15551234567"},
        preferred_channel=ContactChannel.SMS,
        last_visit_date=date(2024, 1, 1),
        treatment_type="checkup",  # 0.8x weight
        recall_interval_days=180,
        no_show_history=0
    )
    
    # 18 days overdue * 0.8 weight = 14.4 weighted days >= 14
    result = scorer.score(patient, days_overdue=18, consecutive_unanswered=0)
    assert result is not None
    urgency, explanation = result
    assert urgency == UrgencyLevel.MEDIUM, f"Expected MEDIUM for checkup at 18 days (weighted 14.4), got {urgency}"


def test_custom_mode_ignores_treatment_weights():
    """Custom mode (use_custom_rules=True) does NOT apply treatment weights."""
    config = UrgencyRulesConfig.get_default()
    config.use_custom_rules = True  # Enable custom rules
    config.general_thresholds = {'medium': 14, 'high': 30, 'critical': 60}
    
    policy = ClinicPolicyConfig()
    scorer = UrgencyScorer(config, policy)
    
    patient = PatientRecord(
        patient_id="TEST005",
        name="Test Patient",
        contact_info={ContactChannel.SMS: "+15551234567"},
        preferred_channel=ContactChannel.SMS,
        last_visit_date=date(2024, 1, 1),
        treatment_type="post_surgery",  # Would be 2.0x in baseline
        recall_interval_days=180,
        no_show_history=0
    )
    
    # 16 days overdue - no weight applied in custom mode
    # Should be MEDIUM (< 30) not HIGH
    result = scorer.score(patient, days_overdue=16, consecutive_unanswered=0)
    assert result is not None
    urgency, explanation = result
    assert urgency == UrgencyLevel.MEDIUM, f"Expected MEDIUM in custom mode (no weights), got {urgency}"
    # Should NOT mention "weighted" in custom mode (that's baseline-specific)
    assert "weighted" not in explanation.lower()


def test_mode_persistence_in_config():
    """use_custom_rules flag persists through to_dict/from_dict."""
    config = UrgencyRulesConfig.get_default()
    config.use_custom_rules = True
    
    # Serialize and deserialize
    data = config.to_dict()
    assert data['use_custom_rules'] == True
    
    restored = UrgencyRulesConfig.from_dict(data)
    assert restored.use_custom_rules == True
    
    # Default should be False
    config_default = UrgencyRulesConfig.get_default()
    assert config_default.use_custom_rules == False
