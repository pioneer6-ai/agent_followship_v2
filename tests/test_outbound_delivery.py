"""
Tests for outbound conversation email delivery and retry logic.

Tests cover:
- Pending delivery creation
- Successful delivery
- Replay prevention (duplicate delivery records)
- Retry with bounded backoff
- Concurrent claim atomicity
- Suppression checks before send (opt-out, staff takeover, escalation)
- Provider success followed by local save failure
- Uncertain outcomes requiring review
"""

import pytest
import tempfile
from datetime import date, datetime, timezone, timedelta

from core.models import PatientRecord, FollowUpCase, ContactChannel, UrgencyLevel, CaseStatus
from agent.conversation import ConversationManager
from agent.email_conversations import EmailConversationManager
from agent.mock_llm import MockLLMClient
from agent.email_provider import MockConversationEmailProvider


@pytest.fixture
def temp_db():
    """Create temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        yield f.name


@pytest.fixture
def patient_alice():
    """Create test patient Alice."""
    return PatientRecord(
        patient_id="P001",
        name="Alice Smith",
        contact_info={
            ContactChannel.EMAIL: "alice@example.com",
            ContactChannel.SMS: "+15550001111"
        },
        preferred_channel=ContactChannel.EMAIL,
        last_visit_date=date(2024, 1, 1),
        treatment_type="cleaning",
        recall_interval_days=180
    )


@pytest.fixture
def case_alice(patient_alice):
    """Create test follow-up case for Alice."""
    return FollowUpCase(
        patient=patient_alice,
        days_overdue=30,
        urgency=UrgencyLevel.MEDIUM,
        reason="6-month cleaning overdue"
    )


def test_reply_creates_pending_delivery(temp_db, patient_alice, case_alice):
    """Generated reply creates pending delivery record before sending."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    email_provider = MockConversationEmailProvider()
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db,
        email_provider=email_provider
    )
    
    # Patient sends booking intent
    result = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment question",
        body="I want to schedule my cleaning",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result.success is True
    assert result.reply_generated is True
    
    # Provider should NOT have been called yet
    assert email_provider.send_count == 0
    
    # Verify pending delivery record exists
    from agent.email_persistence import EmailPersistence
    persistence = EmailPersistence(temp_db)
    
    pending = persistence.get_pending_deliveries()
    assert len(pending) == 1
    assert pending[0]['to_address'] == "alice@example.com"
    assert "cleaning" in pending[0]['body'].lower() or "schedule" in pending[0]['body'].lower()


def test_process_deliveries_sends_pending(temp_db, patient_alice, case_alice):
    """Processing pending deliveries sends via provider."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    email_provider = MockConversationEmailProvider()
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db,
        email_provider=email_provider
    )
    
    # Generate reply (creates pending delivery)
    email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment",
        body="I want to schedule",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Process deliveries
    stats = email_manager.process_pending_deliveries()
    
    assert stats['delivered'] == 1
    assert stats['failed'] == 0
    assert email_provider.send_count == 1
    
    # Verify delivery marked as delivered
    from agent.email_persistence import EmailPersistence
    persistence = EmailPersistence(temp_db)
    pending = persistence.get_pending_deliveries()
    assert len(pending) == 0  # No longer pending


def test_duplicate_delivery_prevention(temp_db, patient_alice, case_alice):
    """Duplicate inbound message doesn't create duplicate delivery."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    email_provider = MockConversationEmailProvider()
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db,
        email_provider=email_provider
    )
    
    # First message - creates delivery
    email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment",
        body="I want to schedule",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Replay - should not create another delivery
    result2 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment",
        body="I want to schedule",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result2.success is True
    assert result2.reply_generated is False  # Replay
    
    # Only one delivery should exist
    from agent.email_persistence import EmailPersistence
    persistence = EmailPersistence(temp_db)
    pending = persistence.get_pending_deliveries()
    assert len(pending) == 1


def test_retry_with_backoff(temp_db, patient_alice, case_alice):
    """Retryable failure schedules retry with backoff."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    email_provider = MockConversationEmailProvider()
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db,
        email_provider=email_provider
    )
    
    # Generate reply
    email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment",
        body="I want to schedule",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Configure provider to fail with retryable error
    email_provider.set_failure_mode('RATE_LIMITED', 'Rate limit exceeded')
    
    # First attempt - should fail and schedule retry
    stats1 = email_manager.process_pending_deliveries()
    assert stats1['delivered'] == 0
    assert stats1['skipped'] == 1  # Will retry
    
    # Should still be pending with retry scheduled
    from agent.email_persistence import EmailPersistence
    persistence = EmailPersistence(temp_db)
    pending = persistence.get_pending_deliveries()
    assert len(pending) == 0  # Not yet (retry in future)
    
    # Check delivery state
    from agent.email_persistence import EmailPersistence
    with persistence._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM outbound_deliveries")
        delivery = cursor.fetchone()
        assert delivery['attempt_count'] == 1
        assert delivery['delivery_state'] == 'pending'
        assert delivery['next_retry_at'] is not None


def test_concurrent_claim_atomicity(temp_db, patient_alice, case_alice):
    """Concurrent processors cannot both claim same delivery."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    email_provider = MockConversationEmailProvider()
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db,
        email_provider=email_provider
    )
    
    # Generate reply
    email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment",
        body="I want to schedule",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    from agent.email_persistence import EmailPersistence
    persistence = EmailPersistence(temp_db)
    pending = persistence.get_pending_deliveries()
    delivery_id = pending[0]['id']
    
    # First processor claims
    claimed1 = persistence.claim_delivery(delivery_id, "processor1")
    assert claimed1 is True
    
    # Second processor tries to claim same delivery
    claimed2 = persistence.claim_delivery(delivery_id, "processor2")
    assert claimed2 is False  # Already claimed


def test_opt_out_suppresses_before_send(temp_db, patient_alice, case_alice):
    """Opt-out checked before sending, suppresses delivery."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    email_provider = MockConversationEmailProvider()
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db,
        email_provider=email_provider
    )
    
    # Generate reply
    result = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment",
        body="I want to schedule",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    thread_id = result.thread_id
    
    # Patient opts out before delivery is processed
    email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Re: Appointment",
        body="STOP",
        message_id="<msg002@example.com>",
        headers={'In-Reply-To': '<msg001@example.com>'},
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Process deliveries - should suppress send
    stats = email_manager.process_pending_deliveries()
    
    assert stats['delivered'] == 0
    assert stats['failed'] == 1  # Terminal failure (opted out)
    assert email_provider.send_count == 0  # Never called provider


def test_staff_takeover_suppresses_before_send(temp_db, patient_alice, case_alice):
    """Staff takeover checked before sending, suppresses delivery."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    email_provider = MockConversationEmailProvider()
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    def staff_auth(user_id: str, action: str) -> bool:
        """Auth callback matching the actual interface (user_id, action)."""
        return user_id == "staff001"
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db,
        email_provider=email_provider,
        staff_auth_callback=staff_auth
    )
    
    # Generate reply
    result = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment",
        body="I want to schedule",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    thread_id = result.thread_id
    
    # Staff takes over before delivery
    email_manager.mark_staff_takeover(thread_id, "staff001")
    
    # Process deliveries - should suppress
    stats = email_manager.process_pending_deliveries()
    
    assert stats['delivered'] == 0
    assert stats['failed'] == 1
    assert email_provider.send_count == 0


def test_escalation_suppresses_before_send(temp_db, patient_alice, case_alice):
    """Escalation checked before sending, suppresses delivery."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    email_provider = MockConversationEmailProvider()
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db,
        email_provider=email_provider
    )
    
    # Patient asks clinical question - escalates
    result1 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Pain question",
        body="I have pain. What should I do?",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # No delivery created for escalated message
    from agent.email_persistence import EmailPersistence
    persistence = EmailPersistence(temp_db)
    pending = persistence.get_pending_deliveries()
    assert len(pending) == 0  # Escalated, no auto-reply


def test_provider_success_local_save_failure_needs_review(temp_db, patient_alice, case_alice):
    """Provider accepts but local save fails - marked for review."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    email_provider = MockConversationEmailProvider()
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db,
        email_provider=email_provider
    )
    
    # Generate reply
    email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment",
        body="I want to schedule",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Inject failure in record_delivery_attempt at the post-send boundary
    from agent.email_persistence import EmailPersistence
    persistence = email_manager.persistence
    original_record = persistence.record_delivery_attempt
    
    def failing_record(delivery_id, outcome, next_retry_at=None):
        if outcome.get('success'):
            # Simulate SQLite write failure after provider accepts
            raise Exception("Database write failed after provider success")
        return original_record(delivery_id, outcome, next_retry_at)
    
    persistence.record_delivery_attempt = failing_record
    
    # Process delivery - provider succeeds but save fails
    stats = email_manager.process_pending_deliveries()
    
    assert stats['needs_review'] == 1
    assert email_provider.send_count == 1  # Provider was called
    
    # Verify marked for review (using proper context manager)
    persistence.record_delivery_attempt = original_record
    with persistence._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM outbound_deliveries")
        delivery = cursor.fetchone()
        assert delivery['delivery_state'] == 'needs_review'
        assert delivery['provider_result'] is not None  # Provider outcome recorded
    
    # Verify it's not resent after manager recreation
    email_manager2 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db,
        email_provider=email_provider
    )
    
    stats2 = email_manager2.process_pending_deliveries()
    assert stats2['delivered'] == 0
    assert stats2['needs_review'] == 0  # Still in needs_review state
    assert email_provider.send_count == 1  # No additional send


def test_delivery_restart_recovery(temp_db, patient_alice, case_alice):
    """Pending deliveries survive restart and can be processed."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    email_provider1 = MockConversationEmailProvider()
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # First manager - generate reply
    email_manager1 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db,
        email_provider=email_provider1
    )
    
    email_manager1.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment",
        body="I want to schedule",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Destroy manager (simulate restart)
    del email_manager1
    
    # Second manager with fresh provider
    email_provider2 = MockConversationEmailProvider()
    email_manager2 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db,
        email_provider=email_provider2
    )
    
    # Process deliveries after restart
    stats = email_manager2.process_pending_deliveries()
    
    assert stats['delivered'] == 1
    assert email_provider2.send_count == 1
