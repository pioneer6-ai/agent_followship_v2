"""
Adapter to bridge email conversation LLM interface to real provider interface.

The email conversation manager expects a simple generate(system_prompt, prompt) interface,
while the real LLM providers use client.messages.create(**kwargs) with a different signature.
"""

from typing import Optional, Any, Dict, List


class EmailLLMAdapter:
    """
    Adapts real LLM provider clients to email conversation interface.
    
    Email conversations need:
        llm.generate(system_prompt: str, prompt: str) -> str
    
    Real providers expect:
        client.messages.create(
            model=str,
            system=str,
            messages=[{"role": "user", "content": str}],
            max_tokens=int,
            ...
        ) -> response with content blocks
    
    This adapter translates between the two interfaces.
    """
    
    def __init__(
        self,
        client: Any,
        model: str = "claude-3-5-sonnet-20241022",
        max_tokens: int = 1024,
        temperature: float = 0.7
    ):
        """
        Initialize adapter.
        
        Args:
            client: Real LLM provider client (Anthropic, OpenAI-compatible, etc.)
            model: Model identifier
            max_tokens: Maximum tokens for response
            temperature: Sampling temperature
        """
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.call_count = 0  # Track provider calls for testing
        self.last_response = None
    
    def generate(self, system_prompt: str, prompt: str) -> str:
        """
        Generate a response using the real LLM provider.
        
        Args:
            system_prompt: System instructions
            prompt: User prompt
            
        Returns:
            Generated text response
        """
        self.call_count += 1
        
        try:
            # Call real provider
            response = self.client.messages.create(
                model=self.model,
                system=system_prompt,
                messages=[
                    {"role": "user", "content": prompt}
                ],
                max_tokens=self.max_tokens,
                temperature=self.temperature
            )
            
            self.last_response = response
            
            # Extract text from response
            # Response format: {"content": [{"type": "text", "text": "..."}], ...}
            if isinstance(response, dict):
                content = response.get('content', [])
            else:
                # If response is an object with attributes
                content = getattr(response, 'content', [])
            
            # Extract text blocks
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get('type') == 'text':
                        text_parts.append(block.get('text', ''))
                elif hasattr(block, 'type') and block.type == 'text':
                    text_parts.append(getattr(block, 'text', ''))
            
            return '\n'.join(text_parts) if text_parts else ""
            
        except Exception as e:
            # Log error and return fallback response
            # In production, this should use proper logging
            print(f"LLM adapter error: {e}")
            
            # Return a safe fallback message
            return """Thank you for your message!

For assistance with your appointment, please contact our clinic directly. Our staff will be happy to help.

You can reach us during business hours or use our online booking system."""
    
    def reset_stats(self):
        """Reset call tracking statistics."""
        self.call_count = 0
        self.last_response = None


def create_email_llm_client(
    provider_client: Optional[Any] = None,
    model: Optional[str] = None,
    **kwargs
) -> Optional[EmailLLMAdapter]:
    """
    Factory function to create email LLM client.
    
    Args:
        provider_client: Real LLM provider client (None for mock/testing)
        model: Model identifier
        **kwargs: Additional parameters for adapter
        
    Returns:
        EmailLLMAdapter if provider_client provided, None for testing mode
    """
    if provider_client is None:
        # No provider - email manager will use template fallback
        return None
    
    return EmailLLMAdapter(
        client=provider_client,
        model=model or "claude-3-5-sonnet-20241022",
        **kwargs
    )
