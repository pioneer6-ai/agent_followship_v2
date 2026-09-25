#!/usr/bin/env python3
"""
Standalone Gmail IMAP poller for inbound conversation emails.

Polls a configured Gmail folder for new messages and feeds them
to the email conversation manager for processing.

Usage:
    python scripts/poll_inbound_email.py        # Single run
    python scripts/poll_inbound_email.py --loop # Continuous with 60s interval
"""

import sys
import os
import time
import signal
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from agent.conversation import ConversationManager
from agent.email_conversations import EmailConversationManager
from agent.email_llm_adapter import create_email_llm_client
from agent.smtp_conversation_provider import SmtpConversationProvider
from agent.patient_lookup import PatientLookupService
from agent.imap_poller import GmailImapPoller
from tools.llm_providers import create_llm_client, LlmProviderConfig
from tools.providers import SmtpEmailProvider
from tools.config import MessagingConfig
from core.data_access import MockPatientDataStore


# Global flag for graceful shutdown
shutdown_requested = False


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    global shutdown_requested
    print(f"\n[IMAP Poller] Received signal {signum}, shutting down gracefully...")
    shutdown_requested = True


def setup_signal_handlers():
    """Register signal handlers for graceful shutdown."""
    signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler)  # kill


def get_imap_config():
    """
    Get IMAP configuration from environment.
    
    Returns:
        Dict with IMAP configuration, or None if not configured
    """
    host = os.environ.get('IMAP_HOST', 'imap.gmail.com')
    port = int(os.environ.get('IMAP_PORT', '993'))
    username = os.environ.get('IMAP_USERNAME') or os.environ.get('SMTP_USERNAME')
    password = os.environ.get('IMAP_PASSWORD') or os.environ.get('SMTP_PASSWORD')
    folder = os.environ.get('IMAP_FOLDER', 'INBOX')
    use_ssl = os.environ.get('IMAP_USE_SSL', '1') == '1'
    
    if not username or not password:
        return None
    
    return {
        'host': host,
        'port': port,
        'username': username,
        'password': password,
        'folder': folder,
        'use_ssl': use_ssl
    }


def create_email_system():
    """
    Create and configure email conversation system.
    
    Returns:
        Tuple of (email_manager, imap_poller, patient_lookup_service, data_store)
    """
    # Initialize LLM client
    llm_config = LlmProviderConfig.from_env()
    try:
        provider_client = create_llm_client(llm_config)
        email_llm = create_email_llm_client(provider_client) if provider_client else None
        
        if email_llm:
            print(f"[IMAP Poller] LLM enabled: {llm_config.kind}/{llm_config.model}")
        else:
            print("[IMAP Poller] LLM disabled - using template responses")
    except Exception as e:
        print(f"[IMAP Poller] LLM initialization failed: {e}")
        print("[IMAP Poller] Falling back to template responses")
        email_llm = None
    
    # Initialize SMTP provider
    messaging_config = MessagingConfig.from_env()
    smtp_provider = SmtpEmailProvider(messaging_config)
    conversation_email_provider = SmtpConversationProvider(smtp_provider)
    
    if not smtp_provider.is_configured():
        print("[IMAP Poller] SMTP not configured - replies will be simulated")
    elif not messaging_config.live_requested:
        print("[IMAP Poller] Dry-run mode - replies will be simulated")
    
    # Initialize data store and patient lookup
    data_store = MockPatientDataStore()
    patient_lookup_service = PatientLookupService(data_store)
    
    # Initialize conversation manager
    conversation_manager = ConversationManager(use_llm=False)
    
    db_path = os.environ.get('EMAIL_CONVERSATION_DB_PATH', 'email_conversations.db')
    clinic_domain = os.environ.get('CLINIC_DOMAIN', 'clinic.example.com')
    
    email_manager = EmailConversationManager(
        conversation_manager=conversation_manager,
        llm_client=email_llm,
        clinic_domain=clinic_domain,
        db_path=db_path,
        email_provider=conversation_email_provider
    )
    
    # Initialize IMAP poller
    imap_config = get_imap_config()
    if not imap_config:
        print("[IMAP Poller] ERROR: IMAP not configured")
        print("[IMAP Poller] Set IMAP_USERNAME and IMAP_PASSWORD (or SMTP_USERNAME/SMTP_PASSWORD)")
        sys.exit(1)
    
    checkpoint_db = os.environ.get('IMAP_CHECKPOINT_DB', 'imap_checkpoint.db')
    
    imap_poller = GmailImapPoller(
        host=imap_config['host'],
        port=imap_config['port'],
        username=imap_config['username'],
        password=imap_config['password'],
        folder=imap_config['folder'],
        checkpoint_db=checkpoint_db,
        use_ssl=imap_config['use_ssl']
    )
    
    print(f"[IMAP Poller] Configured for {imap_config['username']} / {imap_config['folder']}")
    
    return email_manager, imap_poller, patient_lookup_service, data_store


def run_once():
    """
    Run IMAP poller once and exit.
    
    Returns:
        Exit code (0 = success, 1 = errors)
    """
    print("[IMAP Poller] Starting single run")
    
    email_manager, imap_poller, patient_lookup_service, data_store = create_email_system()
    
    # Get lookups
    patient_lookup = patient_lookup_service.get_patient_by_email_lookup()
    case_lookup = patient_lookup_service.get_active_cases_lookup()
    
    # Poll for messages
    stats = imap_poller.poll(email_manager, patient_lookup, case_lookup)
    
    print(f"[IMAP Poller] Results: "
          f"fetched={stats['fetched']}, "
          f"processed={stats['processed']}, "
          f"skipped={stats['skipped']}, "
          f"failed={stats['failed']}")
    
    # Exit with error if processing failed
    if stats['failed'] > 0:
        print(f"[IMAP Poller] WARNING: {stats['failed']} message(s) failed processing")
        return 1
    
    return 0


def run_loop(interval_seconds: int = 60):
    """
    Run IMAP poller in continuous loop.
    
    Args:
        interval_seconds: Seconds to wait between polls
    """
    print(f"[IMAP Poller] Starting continuous mode")
    print(f"[IMAP Poller] Interval: {interval_seconds}s")
    print("[IMAP Poller] Press Ctrl+C to stop")
    
    setup_signal_handlers()
    
    email_manager, imap_poller, patient_lookup_service, data_store = create_email_system()
    
    run_count = 0
    
    while not shutdown_requested:
        run_count += 1
        print(f"\n[IMAP Poller] Poll #{run_count}")
        
        try:
            # Get fresh lookups each time (in case patients changed)
            patient_lookup = patient_lookup_service.get_patient_by_email_lookup()
            case_lookup = patient_lookup_service.get_active_cases_lookup()
            
            # Poll for messages
            stats = imap_poller.poll(email_manager, patient_lookup, case_lookup)
            
            # Log results if there was activity
            if any(stats.values()):
                print(f"[IMAP Poller] Results: "
                      f"fetched={stats['fetched']}, "
                      f"processed={stats['processed']}, "
                      f"skipped={stats['skipped']}, "
                      f"failed={stats['failed']}")
            else:
                print("[IMAP Poller] No new messages")
            
            # Alert if processing failed
            if stats['failed'] > 0:
                print(f"[IMAP Poller] WARNING: {stats['failed']} message(s) failed processing")
        
        except Exception as e:
            print(f"[IMAP Poller] ERROR: {e}")
        
        # Wait for next interval (checking shutdown flag periodically)
        wait_remaining = interval_seconds
        while wait_remaining > 0 and not shutdown_requested:
            time.sleep(min(1, wait_remaining))
            wait_remaining -= 1
    
    print("\n[IMAP Poller] Shutdown complete")


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Poll Gmail IMAP for inbound conversation emails"
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
        help="Interval in seconds between polls in loop mode (default: 60)"
    )
    
    args = parser.parse_args()
    
    if args.loop:
        try:
            run_loop(args.interval)
        except KeyboardInterrupt:
            print("\n[IMAP Poller] Interrupted")
            sys.exit(0)
    else:
        exit_code = run_once()
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
