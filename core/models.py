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
    PENDING_FUTURE_AVAILABILITY = "pending_future_availability"
    # Patient engaged (e.g. via the patient portal) but no offered slot
    # worked for them. NOT a decline and NOT a booking - the case is parked
    # with `next_followup_at` set so TriggerService makes it actionable
    # again once that date arrives. Unlike the terminal outreach statuses
    # above, this one is NOT excluded from TriggerService's actionability
    # check - it is specifically what schedules the case to be picked up
    # again.


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
    Represents a single follow-up case that requires agent action.
    
    This is the atomic unit of decision-making in the agent workflow.
    Each case tracks a patient who needs follow-up and maintains the
    conversation history and current status.
    
    Attributes:
        patient: Reference to the patient record
        days_overdue: Number of days past the recommended recall date
        urgency: Computed urgency level for prioritization
        reason: Human-readable explanation for why follow-up is needed (for audit trail)
        status: Current state in the workflow
        conversation_log: History of all interactions with the patient
        last_contacted: Date when the patient was last contacted
        reminder_count: Number of reminders sent for this case
        next_followup_at: Explicit date to re-trigger this case on, if set.
            Used by TriggerService's basic re-trigger check (Phase 1 only;
            the fuller park-and-retry lifecycle - pending_reason,
            contact_attempt_count, last_booking_window - is deferred).
    """
    patient: PatientRecord
    days_overdue: int
    urgency: UrgencyLevel
    reason: str
    status: CaseStatus = CaseStatus.PENDING
    conversation_log: list[str] = field(default_factory=list)
    last_contacted: Optional[date] = None
    reminder_count: int = 0
    next_followup_at: Optional[date] = None
    
    def add_to_log(self, entry: str) -> None:
        """Add a timestamped entry to the conversation log."""
        self.conversation_log.append(entry)
