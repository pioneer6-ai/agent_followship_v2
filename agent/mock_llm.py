"""
Mock LLM client for testing email conversations.

Provides scripted responses for testing without calling actual LLM APIs.
"""

from typing import Optional


class MockLLMClient:
    """
    Mock LLM client with scripted responses.
    
    Used for testing email conversation flow without external dependencies.
    """
    
    def __init__(self, scripted_responses: Optional[dict] = None):
        """
        Initialize mock LLM with optional scripted responses.
        
        Args:
            scripted_responses: Dict mapping keywords to responses.
                               If None, uses default helpful responses.
        """
        self.scripted_responses = scripted_responses or {}
        self.call_count = 0
        self.last_system_prompt = None
        self.last_prompt = None
    
    def generate(self, system_prompt: str, prompt: str) -> str:
        """
        Generate a response based on the prompt.
        
        Args:
            system_prompt: System instructions for the LLM
            prompt: User prompt
            
        Returns:
            Generated response text
        """
        self.call_count += 1
        self.last_system_prompt = system_prompt
        self.last_prompt = prompt
        
        prompt_lower = prompt.lower()
        
        # Check for scripted responses based on keywords
        for keyword, response in self.scripted_responses.items():
            if keyword.lower() in prompt_lower:
                return response
        
        # Default responses based on common patterns
        if 'book' in prompt_lower or 'schedule' in prompt_lower or 'appointment' in prompt_lower:
            return """Thank you for reaching out! I'd be happy to help you schedule your follow-up appointment.

To book a convenient time, please call our clinic or use our online booking system where you can see all available slots.

Looking forward to seeing you soon!"""
        
        elif 'question' in prompt_lower or '?' in prompt:
            return """Thank you for your question!

For the most accurate information, I'd recommend calling our clinic directly. Our staff will be happy to help with any specific questions you have about your treatment or appointment.

You can reach us during business hours, or feel free to use our online booking system."""
        
        elif 'stop' in prompt_lower or 'unsubscribe' in prompt_lower:
            # This shouldn't reach the LLM (should be caught earlier), but handle it safely
            return "We've noted your preference. You won't receive further automated messages."
        
        else:
            # Generic helpful response
            return """Thank you for your message!

If you'd like to schedule your follow-up appointment, please call our clinic or use our online booking system.

We're here to help if you have any questions!"""
    
    def reset_stats(self):
        """Reset call tracking stats."""
        self.call_count = 0
        self.last_system_prompt = None
        self.last_prompt = None


class RefuseBookingLLMClient(MockLLMClient):
    """
    Mock LLM that always refuses to book appointments.
    
    Used to test that the LLM correctly directs booking requests
    to staff/phone/online system rather than handling them directly.
    """
    
    def generate(self, system_prompt: str, prompt: str) -> str:
        """Always redirect booking requests."""
        self.call_count += 1
        self.last_system_prompt = system_prompt
        self.last_prompt = prompt
        
        return """I'm unable to book appointments directly through email. 

To schedule your appointment, please:
1. Call our clinic during business hours, or
2. Use our online booking system

Our staff will help you find a convenient time for your visit.

Thank you for understanding!"""
