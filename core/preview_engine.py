"""
Preview Engine for Urgency Configuration Changes

This module provides side-effect-free simulation of urgency scoring
with proposed configuration changes. Allows clinic staff to preview
the impact of configuration changes before applying them.
"""

from datetime import date
from typing import Optional
from dataclasses import dataclass
from core.models import PatientRecord, UrgencyLevel, FollowUpCase, calculate_days_overdue
from core.urgency_config import UrgencyRulesConfig
from agent.business_rules import UrgencyScorer, RecallRuleEngine
from core.data_access import PatientDataStore


@dataclass
class UrgencyChangePreview:
    """
    Represents the change in urgency for a single patient.
    """
    patient_id: str
    patient_name: str
    days_overdue: int
    old_urgency: UrgencyLevel
    old_explanation: str
    new_urgency: UrgencyLevel
    new_explanation: str
    
    @property
    def urgency_changed(self) -> bool:
        """Check if urgency level changed."""
        return self.old_urgency != self.new_urgency
    
    @property
    def urgency_increased(self) -> bool:
        """Check if urgency level increased."""
        levels = [UrgencyLevel.LOW, UrgencyLevel.MEDIUM, UrgencyLevel.HIGH, UrgencyLevel.CRITICAL]
        if not self.urgency_changed:
            return False
        return levels.index(self.new_urgency) > levels.index(self.old_urgency)
    
    @property
    def urgency_decreased(self) -> bool:
        """Check if urgency level decreased."""
        levels = [UrgencyLevel.LOW, UrgencyLevel.MEDIUM, UrgencyLevel.HIGH, UrgencyLevel.CRITICAL]
        if not self.urgency_changed:
            return False
        return levels.index(self.new_urgency) < levels.index(self.old_urgency)


@dataclass
class ConfigurationPreview:
    """
    Complete preview of configuration changes across all patients.
    """
    changes: list[UrgencyChangePreview]
    summary: dict
    
    @classmethod
    def create(cls, changes: list[UrgencyChangePreview]) -> "ConfigurationPreview":
        """Create preview with summary statistics."""
        increased = sum(1 for c in changes if c.urgency_increased)
        decreased = sum(1 for c in changes if c.urgency_decreased)
        unchanged = sum(1 for c in changes if not c.urgency_changed)
        
        urgency_counts = {
            'low': sum(1 for c in changes if c.new_urgency == UrgencyLevel.LOW),
            'medium': sum(1 for c in changes if c.new_urgency == UrgencyLevel.MEDIUM),
            'high': sum(1 for c in changes if c.new_urgency == UrgencyLevel.HIGH),
            'critical': sum(1 for c in changes if c.new_urgency == UrgencyLevel.CRITICAL),
        }
        
        summary = {
            'total_patients': len(changes),
            'increased': increased,
            'decreased': decreased,
            'unchanged': unchanged,
            'urgency_distribution': urgency_counts
        }
        
        return cls(changes=changes, summary=summary)


class PreviewEngine:
    """
    Simulates urgency scoring with proposed configuration changes.
    
    This engine provides a side-effect-free way to preview how
    configuration changes would affect urgency scoring for all
    active patients, without modifying any actual case state.
    """
    
    def __init__(
        self, 
        data_store: PatientDataStore, 
        current_config: UrgencyRulesConfig,
        policy: Optional["ClinicPolicyConfig"] = None,
        active_cases: Optional[dict[str, FollowUpCase]] = None
    ):
        """
        Initialize preview engine.
        
        Args:
            data_store: Patient data store
            current_config: Current urgency configuration
            active_cases: Optional dict of active cases (episode_id -> case) for matching historical state
        """
        from core.config import ClinicPolicyConfig
        self.data_store = data_store
        self.current_config = current_config
        self.policy = policy or ClinicPolicyConfig.from_env()
        self.active_cases = active_cases or {}
    
    def preview_config_change(
        self, 
        proposed_config: UrgencyRulesConfig, 
        today: Optional[date] = None
    ) -> ConfigurationPreview:
        """
        Preview urgency changes with proposed configuration.
        
        Matches existing episodes by episode_id (patient_id + last_visit_date) to
        preserve historical state like consecutive_unanswered count. Uses zero only
        for genuinely new episodes.
        
        Args:
            proposed_config: Proposed new configuration
            today: Reference date (defaults to today)
            
        Returns:
            ConfigurationPreview with changes and summary
        """
        if today is None:
            today = date.today()
        
        # Get all active patients
        patients = self.data_store.get_all_active_patients()
        
        # Score with current config
        current_scorer = UrgencyScorer(self.current_config, self.policy)
        
        # Score with proposed config
        proposed_scorer = UrgencyScorer(proposed_config, self.policy)
        
        changes = []
        
        for patient in patients:
            # Calculate days overdue
            from datetime import timedelta
            recall_date = patient.last_visit_date + timedelta(days=patient.recall_interval_days)
            days_overdue = (today - recall_date).days
            
            # Skip patients who aren't overdue yet
            if days_overdue <= 0:
                continue
            
            # Match existing case by patient_id, then verify episode_id
            expected_episode_id = f"{patient.patient_id}_{patient.last_visit_date.isoformat()}"
            
            # Get consecutive_unanswered from existing case, or 0 for new episodes
            consecutive_unanswered = 0
            existing = self.active_cases.get(patient.patient_id)
            if existing and existing.episode_id == expected_episode_id:
                # Same episode - use actual consecutive_unanswered
                consecutive_unanswered = existing.consecutive_unanswered_reminders
            # else: different episode or no existing case - use 0
            
            # Score with current config
            current_result = current_scorer.score(patient, days_overdue, consecutive_unanswered=consecutive_unanswered)
            if current_result is None:
                continue
            current_urgency, current_explanation = current_result
            
            # Score with proposed config (same consecutive_unanswered)
            proposed_result = proposed_scorer.score(patient, days_overdue, consecutive_unanswered=consecutive_unanswered)
            if proposed_result is None:
                continue
            proposed_urgency, proposed_explanation = proposed_result
            
            # Create change record
            change = UrgencyChangePreview(
                patient_id=patient.patient_id,
                patient_name=patient.name,
                days_overdue=days_overdue,
                old_urgency=current_urgency,
                old_explanation=current_explanation,
                new_urgency=proposed_urgency,
                new_explanation=proposed_explanation
            )
            changes.append(change)
        
        return ConfigurationPreview.create(changes)
    
    def preview_threshold_change(
        self,
        urgency_level: str,
        new_threshold: int,
        today: Optional[date] = None
    ) -> ConfigurationPreview:
        """
        Preview impact of changing a single threshold.
        
        Args:
            urgency_level: 'medium', 'high', or 'critical'
            new_threshold: New threshold value in days
            today: Reference date (defaults to today)
            
        Returns:
            ConfigurationPreview with changes and summary
        """
        # Create proposed config with modified threshold
        proposed_config = UrgencyRulesConfig.from_dict(self.current_config.to_dict())
        proposed_config.general_thresholds[urgency_level] = new_threshold
        
        return self.preview_config_change(proposed_config, today)
    
    def preview_treatment_override(
        self,
        treatment_type: str,
        overrides: dict[str, int],
        today: Optional[date] = None
    ) -> ConfigurationPreview:
        """
        Preview impact of adding/modifying a treatment override.
        
        Args:
            treatment_type: Treatment type identifier
            overrides: Dict of urgency level -> threshold in days
            today: Reference date (defaults to today)
            
        Returns:
            ConfigurationPreview with changes and summary
        """
        # Create proposed config with treatment override
        proposed_config = UrgencyRulesConfig.from_dict(self.current_config.to_dict())
        proposed_config.treatment_overrides[treatment_type] = overrides
        
        return self.preview_config_change(proposed_config, today)
