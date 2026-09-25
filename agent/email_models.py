"""
Data models for email conversations.

Separated to avoid circular imports with persistence layer.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Dict


@dataclass
class EmailMessage:
    """
    Single message in an email thread.
    
    Attributes:
        message_id: Unique Message-ID from email headers
        from_address: Sender email address
        to_address: Recipient email address
        subject: Email subject line
        body: Email body text
        timestamp: When the email was sent/received
        direction: 'inbound' (from patient) or 'outbound' (from clinic)
        headers: Raw email headers for threading (In-Reply-To, References, etc.)
    """
    message_id: str
    from_address: str
    to_address: str
    subject: str
    body: str
    timestamp: datetime
    direction: str  # 'inbound' or 'outbound'
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class EmailThread:
    """
    Represents an email conversation thread with a patient.
    
    Threads are deduplicated using standard email threading headers
    (In-Reply-To, References) to maintain conversation continuity.
    
    Attributes:
        thread_id: Unique identifier for this thread
        patient_id: Patient this thread belongs to
        case_episode_id: Links to FollowUpCase.episode_id
        subject: Original email subject
        messages: Chronological list of messages in thread
        staff_takeover: True if staff has manually taken over this conversation
        opted_out: True if patient has opted out of communications
        created_at: When thread was created
        last_activity: When last message was sent/received
    """
    thread_id: str
    patient_id: str
    case_episode_id: str
    subject: str
    messages: List[EmailMessage] = field(default_factory=list)
    staff_takeover: bool = False
    opted_out: bool = False
    needs_staff_review: bool = False  # Thread escalated, no auto-replies until staff action
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def add_message(self, message: EmailMessage) -> None:
        """Add a message to the thread and update last activity."""
        self.messages.append(message)
        self.last_activity = datetime.now(timezone.utc)


@dataclass
class EmailProcessingResult:
    """
    Result of processing an inbound email.
    
    Attributes:
        success: Whether email was processed successfully
        thread_id: ID of the thread this email belongs to
        reply_generated: Whether an automated reply was generated
        reply_text: The generated reply text (if any)
        reason: Explanation of the processing result
        should_send_reply: Whether the reply should be sent
    """
    success: bool
    thread_id: str | None = None
    reply_generated: bool = False
    reply_text: str | None = None
    reason: str = ""
    should_send_reply: bool = False
