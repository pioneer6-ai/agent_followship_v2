"""
Mock email provider for conversation delivery testing.

Provides a simple mock that records calls without sending real emails.
"""

from typing import Dict, Any, Optional
from dataclasses import dataclass, field


@dataclass
class MockEmailProviderOutcome:
    """Mock provider outcome matching the real provider interface."""
    success: bool
    message_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    simulated: bool = True


class MockConversationEmailProvider:
    """
    Mock email provider for testing conversation delivery.
    
    Records all send attempts for verification in tests.
    Does not send real emails.
    """
    
    def __init__(self):
        """Initialize mock provider with call tracking."""
        self.send_calls = []
        self.send_count = 0
        self.should_fail = False
        self.failure_error_code = None
        self.failure_error_message = None
    
    def send(
        self,
        to_address: str,
        subject: str,
        body: str,
        headers: Optional[Dict[str, str]] = None
    ) -> MockEmailProviderOutcome:
        """
        Mock send that records the call.
        
        Args:
            to_address: Recipient email
            subject: Email subject
            body: Email body
            headers: Email headers
            
        Returns:
            Mock outcome
        """
        self.send_count += 1
        
        call_record = {
            'to_address': to_address,
            'subject': subject,
            'body': body,
            'headers': headers or {}
        }
        self.send_calls.append(call_record)
        
        if self.should_fail:
            return MockEmailProviderOutcome(
                success=False,
                error_code=self.failure_error_code or 'NETWORK_ERROR',
                error_message=self.failure_error_message or 'Simulated failure'
            )
        
        # Simulate successful send
        mock_message_id = f"<mock-{self.send_count}@testprovider.example>"
        return MockEmailProviderOutcome(
            success=True,
            message_id=mock_message_id
        )
    
    def reset(self):
        """Reset call tracking."""
        self.send_calls = []
        self.send_count = 0
        self.should_fail = False
        self.failure_error_code = None
        self.failure_error_message = None
    
    def set_failure_mode(self, error_code: str, error_message: str):
        """Configure provider to fail."""
        self.should_fail = True
        self.failure_error_code = error_code
        self.failure_error_message = error_message
