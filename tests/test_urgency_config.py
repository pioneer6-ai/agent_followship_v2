import pytest
from core.urgency_config import UrgencyRulesConfig, EpisodeStatus

def test_default_config_is_valid():
    config = UrgencyRulesConfig.get_default()
    is_valid, errors = config.validate()
    assert is_valid, f"Default config should be valid, errors: {errors}"

def test_config_validation_threshold_ordering():
    config = UrgencyRulesConfig(general_thresholds={'medium': 30, 'high': 30, 'critical': 60})
    is_valid, errors = config.validate()
    assert not is_valid
    assert any('must be less than' in err for err in errors)
    config = UrgencyRulesConfig(general_thresholds={'medium': 14, 'high': 60, 'critical': 60})
    is_valid, errors = config.validate()
    assert not is_valid

def test_config_validation_positive_integers():
    config = UrgencyRulesConfig(general_thresholds={'medium': -5, 'high': 30, 'critical': 60})
    is_valid, errors = config.validate()
    assert not is_valid
    assert any('positive' in err for err in errors)
    config = UrgencyRulesConfig(general_thresholds={'medium': 0, 'high': 30, 'critical': 60})
    is_valid, errors = config.validate()
    assert not is_valid

def test_config_validation_boolean_rejected():
    config = UrgencyRulesConfig(general_thresholds={'medium': True, 'high': 30, 'critical': 60})
    is_valid, errors = config.validate()
    assert not is_valid
    assert any('integer' in err for err in errors)

def test_config_validation_treatment_overrides():
    config = UrgencyRulesConfig.get_default()
    config.treatment_overrides = {'post_surgery': {'enabled': True, 'medium': 7, 'high': 14, 'critical': 30}}
    is_valid, errors = config.validate()
    assert is_valid
    config.treatment_overrides = {'post_surgery': {'enabled': True, 'medium': 14, 'high': 14, 'critical': 30}}
    is_valid, errors = config.validate()
    assert not is_valid

def test_config_serialization_round_trip():
    config = UrgencyRulesConfig.get_default()
    config.treatment_overrides = {'cleaning': {'enabled': True, 'medium': 20, 'high': 40, 'critical': 80}}
    data = config.to_dict()
    restored = UrgencyRulesConfig.from_dict(data)
    assert restored.general_thresholds == config.general_thresholds
    assert restored.treatment_overrides == config.treatment_overrides
    assert restored.escalation_rules == config.escalation_rules

def test_config_invalid_urgency_levels():
    config = UrgencyRulesConfig.get_default()
    config.missed_appointment_rules = {'enabled': True, 'count_threshold': 2, 'min_urgency_level': 'SUPER_HIGH'}
    is_valid, errors = config.validate()
    assert not is_valid

def test_escalation_rules_enabled_disabled():
    config = UrgencyRulesConfig.get_default()
    config.escalation_rules = {'enabled': False, 'consecutive_unanswered_threshold': 3}
    is_valid, errors = config.validate()
    assert is_valid
    config.escalation_rules = {'enabled': True, 'consecutive_unanswered_threshold': 0}
    is_valid, errors = config.validate()
    assert not is_valid
    assert any('positive' in err for err in errors)

def test_default_values_match_spec():
    config = UrgencyRulesConfig.get_default()
    assert config.general_thresholds == {'medium': 14, 'high': 30, 'critical': 60}
    assert config.missed_appointment_rules['count_threshold'] == 3
    assert config.unanswered_reminder_urgency_rules['count_threshold'] == 2
    assert config.escalation_rules['consecutive_unanswered_threshold'] == 3
    assert config.escalation_rules['enabled'] == True
