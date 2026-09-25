"""
Agent actions and decision framework.

This module defines the available actions that the agent can take
when processing follow-up cases, representing the "Act" phase of
the agentic loop (Perceive -> Decide -> Act -> Observe).
"""

from enum import Enum


class AgentAction(Enum):
    """
    Enumeration of all possible actions the agent can take.
    
    These actions represent the agent's decision space. Each action
    corresponds to a specific behavior that moves a follow-up case
    forward in the workflow.
    
    Actions:
        SEND_REMINDER: Send initial or follow-up reminder to patient
        PROPOSE_SLOT: Suggest available appointment times to patient
        CONFIRM_BOOKING: Finalize appointment after patient confirms
        RESCHEDULE: Change existing appointment to new time
        ESCALATE_TO_STAFF: Transfer case to human staff for manual handling
        MARK_DECLINED: Record that patient declined or postponed follow-up
        DO_NOTHING: No action needed at this time (e.g., already contacted recently)
        RECORD_OPT_OUT: Record that the patient asked not to be contacted again
        REQUEST_CLARIFICATION: Ask the patient a clarifying question instead of
            proceeding, because their consent signal was ambiguous
        MARK_PENDING_FUTURE_AVAILABILITY: Record that the patient engaged but no
            offered slot worked for them; park the case for a later re-check
            rather than declining or booking
    """
    SEND_REMINDER = "send_reminder"
    PROPOSE_SLOT = "propose_slot"
    CONFIRM_BOOKING = "confirm_booking"
    RESCHEDULE = "reschedule"
    ESCALATE_TO_STAFF = "escalate_to_staff"
    MARK_DECLINED = "mark_declined"
    DO_NOTHING = "do_nothing"
    RECORD_OPT_OUT = "record_opt_out"
    REQUEST_CLARIFICATION = "request_clarification"
    MARK_PENDING_FUTURE_AVAILABILITY = "mark_pending_future_availability"
