"""
SMTP email provider adapter for email conversations.

Adapts tools.providers.SmtpEmailProvider to the conversation interface,
adding support for threading headers (In-Reply-To, References).
"""

from typing import Optional, Dict, Any
from dataclasses import dataclass

from tools.providers import SmtpEmailProvider, SendRequest, ProviderOutcome


@dataclass
class ConversationProviderOutcome:
    """
    Email provider outcome for conversation deliveries.
    
    Matches the interface expected by conversation outbound delivery.
    """
    success: bool
    message_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    simulated: bool = False


class SmtpConversationProvider:
    """
    SMTP email provider for conversation deliveries with threading support.
    
    Wraps the existing SmtpEmailProvider and adds conversation-specific
    features like In-Reply-To and References headers for email threading.
    
    This adapter allows conversations to reuse the same SMTP provider
    and configuration that reminders use, without modifying reminder behavior.
    """
    
    def __init__(self, smtp_provider: SmtpEmailProvider):
        """
        Initialize conversation provider.
        
        Args:
            smtp_provider: Configured SMTP provider from tools.providers
        """
        self.smtp_provider = smtp_provider
        self.send_count = 0  # Track calls for testing
    
    def send(
        self,
        to_address: str,
        subject: str,
        body: str,
        headers: Optional[Dict[str, str]] = None
    ) -> ConversationProviderOutcome:
        """
        Send email with conversation threading headers.
        
        Args:
            to_address: Recipient email address
            subject: Email subject line
            body: Plain text body
            headers: Threading headers (In-Reply-To, References, Message-ID)
            
        Returns:
            Conversation provider outcome
        """
        self.send_count += 1
        
        # Build SendRequest with extra headers
        request = SendRequest(
            to=to_address,
            subject=subject,
            body=body,
            extra_headers=headers or {}
        )
        
        # Call underlying SMTP provider
        outcome = self.smtp_provider.send(request)
        
        # Convert to conversation outcome
        return ConversationProviderOutcome(
            success=outcome.success,
            message_id=outcome.message_id,
            error_code=outcome.error_code.value if outcome.error_code else None,
            error_message=outcome.error_message,
            simulated=outcome.simulated
        )
    
    def reset(self):
        """Reset call tracking (for testing)."""
        self.send_count = 0
    
    def is_configured(self) -> bool:
        """Check if SMTP provider is configured."""
        return self.smtp_provider.is_configured()
