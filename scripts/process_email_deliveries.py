#!/usr/bin/env python3
"""
Standalone email delivery processor.

Processes pending outbound email deliveries with graceful shutdown.
Can be run as a cron job or standalone process.

Usage:
    python scripts/process_email_deliveries.py        # Single run
    python scripts/process_email_deliveries.py --loop # Continuous with 60s interval
"""

import sys
import os
import time
import signal
from pathlib import Path
from typing import Optional

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from agent.conversation import ConversationManager
from agent.email_conversations import EmailConversationManager
from agent.email_llm_adapter import create_email_llm_client
from agent.smtp_conversation_provider import SmtpConversationProvider
from tools.llm_providers import create_llm_client, LlmProviderConfig
from tools.providers import SmtpEmailProvider
from tools.config import MessagingConfig


# Global flag for graceful shutdown
shutdown_requested = False


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    global shutdown_requested
    print(f"\n[Delivery Processor] Received signal {signum}, shutting down gracefully...")
    shutdown_requested = True


def setup_signal_handlers():
    """Register signal handlers for graceful shutdown."""
    signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler)  # kill


def create_email_manager() -> EmailConversationManager:
    """
    Create and configure email conversation manager.
    
    Returns:
        Configured EmailConversationManager instance
    """
    # Initialize LLM client
    llm_config = LlmProviderConfig.from_env()
    provider_client = create_llm_client(llm_config)
    email_llm = create_email_llm_client(provider_client) if provider_client else None
    
    if email_llm:
        print(f"[Delivery Processor] LLM enabled: {llm_config.kind}/{llm_config.model}")
    else:
        print("[Delivery Processor] LLM disabled - using template responses")
    
    # Initialize SMTP provider
    messaging_config = MessagingConfig.from_env()
    smtp_provider = SmtpEmailProvider(messaging_config)
    conversation_email_provider = SmtpConversationProvider(smtp_provider)
    
    if not smtp_provider.is_configured():
        print("[Delivery Processor] SMTP not configured - sends will be simulated")
    elif not messaging_config.live_requested:
        print("[Delivery Processor] Dry-run mode - sends will be simulated")
    else:
        print(f"[Delivery Processor] SMTP configured: {messaging_config.smtp_host}")
    
    # Initialize conversation manager
    conversation_manager = ConversationManager(use_llm=False)
    
    db_path = os.environ.get('EMAIL_CONVERSATION_DB_PATH', 'email_conversations.db')
    clinic_domain = os.environ.get('CLINIC_DOMAIN', 'clinic.example.com')
    
    return EmailConversationManager(
        conversation_manager=conversation_manager,
        llm_client=email_llm,
        clinic_domain=clinic_domain,
        db_path=db_path,
        email_provider=conversation_email_provider
    )


def process_deliveries(email_manager: EmailConversationManager, processor_id: str) -> dict:
    """
    Process pending email deliveries.
    
    Args:
        email_manager: Email conversation manager
        processor_id: Unique identifier for this processor
        
    Returns:
        Statistics dict with delivery counts
    """
    try:
        stats = email_manager.process_pending_deliveries(processor_id)
        return stats
    except Exception as e:
        print(f"[Delivery Processor ERROR] {type(e).__name__}: {e}")
        return {'delivered': 0, 'failed': 0, 'skipped': 0, 'needs_review': 0}


def run_once(processor_id: Optional[str] = None):
    """
    Run delivery processor once and exit.
    
    Args:
        processor_id: Optional processor identifier
        
    Returns:
        Exit code (0 = success, 1 = errors needing review)
    """
    if processor_id is None:
        processor_id = f"standalone-{os.getpid()}"
    
    print(f"[Delivery Processor] Starting single run - ID: {processor_id}")
    
    email_manager = create_email_manager()
    stats = process_deliveries(email_manager, processor_id)
    
    print(f"[Delivery Processor] Results: "
          f"delivered={stats['delivered']}, "
          f"failed={stats['failed']}, "
          f"skipped={stats['skipped']}, "
          f"needs_review={stats['needs_review']}")
    
    # Exit with error if deliveries need review (for monitoring/alerting)
    if stats['needs_review'] > 0:
        print(f"[Delivery Processor] WARNING: {stats['needs_review']} deliveries need staff review")
        return 1
    
    return 0


def run_loop(interval_seconds: int = 60):
    """
    Run delivery processor in continuous loop.
    
    Args:
        interval_seconds: Seconds to wait between processing runs
    """
    processor_id = f"loop-{os.getpid()}"
    
    print(f"[Delivery Processor] Starting continuous mode - ID: {processor_id}")
    print(f"[Delivery Processor] Interval: {interval_seconds}s")
    print("[Delivery Processor] Press Ctrl+C to stop")
    
    setup_signal_handlers()
    
    email_manager = create_email_manager()
    
    run_count = 0
    
    while not shutdown_requested:
        run_count += 1
        print(f"\n[Delivery Processor] Run #{run_count}")
        
        stats = process_deliveries(email_manager, processor_id)
        
        # Only log if there was activity
        if any(stats.values()):
            print(f"[Delivery Processor] Results: "
                  f"delivered={stats['delivered']}, "
                  f"failed={stats['failed']}, "
                  f"skipped={stats['skipped']}, "
                  f"needs_review={stats['needs_review']}")
        else:
            print("[Delivery Processor] No pending deliveries")
        
        # Alert if deliveries need review
        if stats['needs_review'] > 0:
            print(f"[Delivery Processor] WARNING: {stats['needs_review']} deliveries need staff review")
        
        # Wait for next interval (checking shutdown flag periodically)
        wait_remaining = interval_seconds
        while wait_remaining > 0 and not shutdown_requested:
            time.sleep(min(1, wait_remaining))
            wait_remaining -= 1
    
    print("\n[Delivery Processor] Shutdown complete")


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Process pending email conversation deliveries"
    )
    parser.add_argument(
        '--loop',
        action='store_true',
        help="Run continuously (default: single run)"
    )
    parser.add_argument(
        '--interval',
        type=int,
        default=60,
        help="Interval in seconds between runs in loop mode (default: 60)"
    )
    parser.add_argument(
        '--processor-id',
        type=str,
        help="Custom processor identifier (default: auto-generated)"
    )
    
    args = parser.parse_args()
    
    if args.loop:
        try:
            run_loop(args.interval)
        except KeyboardInterrupt:
            print("\n[Delivery Processor] Interrupted")
            sys.exit(0)
    else:
        exit_code = run_once(args.processor_id)
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
