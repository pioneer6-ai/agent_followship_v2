"""
Data models for the Patient Follow-up Agent system.

This module defines the core data structures used throughout the application,
including enums for urgency levels, contact channels, and case statuses,
as well as dataclasses for patient records and follow-up cases.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from datetime import date
from typing import Optional


class UrgencyLevel(Enum):
    """
    Defines the urgency level for patient follow-up cases.
    
    - LOW: Routine follow-up, no immediate health concerns
    - MEDIUM: Follow-up recommended, approaching overdue threshold
    - HIGH: Treatment interrupted or chronic condition follow-up significantly overdue
    - CRITICAL: Post-surgery follow-up severely overdue, requires immediate staff intervention
    """
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ContactChannel(Enum):
    """
    Available communication channels for contacting patients.
    
    Different patients may prefer different channels based on
    accessibility, age, or personal preference.
    """
    SMS = "sms"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    PHONE_CALL = "phone_call"


class CaseStatus(Enum):
    """
    Tracks the current state of a follow-up case in the agent workflow.
    
    The status transitions typically follow:
    PENDING -> MESSAGE_SENT -> AWAITING_REPLY -> BOOKED/DECLINED/ESCALATED
    """
    PENDING = "pending"                # Awaiting contact
    MESSAGE_SENT = "message_sent"      # Reminder sent to patient
    AWAITING_REPLY = "awaiting_reply"  # Waiting for patient response
    BOOKED = "booked"                  # Successfully scheduled appointment
    DECLINED = "declined"              # Patient declined or postponed
    ESCALATED = "escalated"            # Transferred to staff for manual handling
    OPTED_OUT = "opted_out"            # Patient asked not to be contacted again


@dataclass
class PatientRecord:
    """
    Represents a patient's basic information and clinical history.
    
    This data is typically sourced from the clinic's Practice Management System (PMS)
    or Electronic Health Record (EHR) system.
    
    Attributes:
        patient_id: Unique identifier for the patient
        name: Patient's full name
        contact_info: Dictionary mapping contact channels to contact details (phone/email)
        preferred_channel: Patient's preferred method of communication
        last_visit_date: Date of most recent clinic visit
        treatment_type: Type of treatment (e.g., "cleaning", "orthodontic_checkup", "post_surgery")
        recall_interval_days: Recommended days between visits for this treatment type
        no_show_history: Number of previous no-shows (affects priority scoring)
        language: Patient's preferred language for messages (default: English)
        opted_out: Whether the patient has asked not to be contacted again.
            Once set, PolicyGuard blocks all further outbound/booking actions
            for this patient regardless of urgency or reminder cadence.
    """
    patient_id: str
    name: str
    contact_info: dict[ContactChannel, str]
    preferred_channel: ContactChannel
    last_visit_date: date
    treatment_type: str
    recall_interval_days: int
    no_show_history: int = 0
    language: str = "en"
    opted_out: bool = False


@dataclass
class FollowUpCase:
    """
    Represents a single follow-up case requiring agent action.

    Attributes:
        patient: Reference to the patient record.
        days_overdue: Days past the recommended recall date.
        urgency: Computed urgency level.
        reason: Explanation of why follow-up is needed.
        status: Current workflow state.
        conversation_log: History of interactions.
        last_contacted: Date of the latest contact.
        reminder_count: Number of reminders sent.
        episode_id: Identifier for this patient's recall episode.
        consecutive_unanswered_reminders: Unanswered reminders in this episode.
        urgency_explanation: Explanation of the urgency score.
        next_followup_at: Explicit date to re-trigger this case, if set.
            Used by TriggerService's basic re-trigger check.
    """

    patient: PatientRecord
    days_overdue: int
    urgency: UrgencyLevel
    reason: str
    status: CaseStatus = CaseStatus.PENDING
    conversation_log: list[str] = field(default_factory=list)
    last_contacted: Optional[date] = None
    reminder_count: int = 0
    episode_id: str = field(default="")
    consecutive_unanswered_reminders: int = 0
    urgency_explanation: str = ""
    next_followup_at: Optional[date] = None

    def __post_init__(self):
        if not self.episode_id:
            self.episode_id = (
                f"{self.patient.patient_id}_"
                f"{self.patient.last_visit_date.isoformat()}"
            )

    def add_to_log(self, entry: str) -> None:
        """Add an entry to the conversation log."""
        self.conversation_log.append(entry)


def calculate_days_overdue(patient: PatientRecord, as_of_date: date) -> int:
    """
    Calculate days overdue for a patient relative to a specific date.
    
    This is the canonical calculation used by both preview and immediate
    application to ensure consistency.
    
    Args:
        patient: Patient record with last_visit_date and recall_interval_days
        as_of_date: The reference date (typically date.today())
        
    Returns:
        Number of days overdue (can be negative if not yet due)
    """
    from datetime import timedelta
    recall_date = patient.last_visit_date + timedelta(days=patient.recall_interval_days)
    return (as_of_date - recall_date).days
