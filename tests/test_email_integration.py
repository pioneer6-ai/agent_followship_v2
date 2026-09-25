"""
Integration test for the complete email conversation flow.

Tests the assembled components working together:
- Inbound email adapter (simulated webhook)
- Conversation manager with LLM
- Persisted reply storage
- Delivery processor
- Provider interface

Does not require live email sending or real LLM - uses mocks.
"""

import pytest
import tempfile
from datetime import date
from pathlib import Path

from core.models import PatientRecord, FollowUpCase, ContactChannel, UrgencyLevel, CaseStatus
from agent.conversation import ConversationManager
from agent.email_conversations import EmailConversationManager
from agent.mock_llm import MockLLMClient
from agent.email_provider import MockConversationEmailProvider
from agent.patient_lookup import PatientLookupService
from core.data_access import MockPatientDataStore


@pytest.fixture
def temp_db():
    """Create temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        yield f.name


@pytest.fixture
def data_store():
    """Create mock data store with test patients."""
    store = MockPatientDataStore()
    
    # Add test patient
    patient = PatientRecord(
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
    store.add_patient(patient)
    
    return store


@pytest.fixture
def patient_lookup(data_store):
    """Create patient lookup service."""
    return PatientLookupService(data_store)


@pytest.fixture
def case_alice():
    """Create test follow-up case for Alice."""
    return FollowUpCase(
        patient=PatientRecord(
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
        ),
        days_overdue=30,
        urgency=UrgencyLevel.MEDIUM,
        reason="6-month cleaning overdue"
    )


def test_end_to_end_conversation_flow(temp_db, data_store, patient_lookup, case_alice):
    """
    Complete integration test of email conversation flow.
    
    Steps:
    1. Patient sends inbound email (webhook simulation)
    2. Conversation manager generates reply with mock LLM
    3. Reply is persisted as pending delivery
    4. Delivery processor sends via provider
    5. Verify threading headers and provider call
    """
    # Initialize components
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    email_provider = MockConversationEmailProvider()
    
    # Create email conversation manager
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db,
        email_provider=email_provider
    )
    
    # Step 1: Simulate inbound email (webhook)
    patient_lookup_dict = patient_lookup.get_patient_by_email_lookup()
    case_lookup_dict = patient_lookup.get_active_cases_lookup()
    
    inbound_result = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment question",
        body="Hi, I'd like to schedule my dental cleaning appointment. Are there any openings next week?",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup_dict,
        case_lookup=case_lookup_dict
    )
    
    # Step 2: Verify reply was generated
    assert inbound_result.success is True
    assert inbound_result.reply_generated is True
    thread_id = inbound_result.thread_id
    assert thread_id is not None
    
    # Step 3: Verify reply is persisted as pending delivery (not sent yet)
    from agent.email_persistence import EmailPersistence
    persistence = EmailPersistence(temp_db)
    
    pending = persistence.get_pending_deliveries()
    assert len(pending) == 1
    assert pending[0]['to_address'] == "alice@example.com"
    assert "schedule" in pending[0]['body'].lower() or "appointment" in pending[0]['body'].lower()
    
    # Verify provider has NOT been called yet
    assert email_provider.send_count == 0
    
    # Step 4: Process deliveries
    stats = email_manager.process_pending_deliveries("test-processor")
    
    assert stats['delivered'] == 1
    assert stats['failed'] == 0
    assert stats['skipped'] == 0
    assert stats['needs_review'] == 0
    
    # Step 5: Verify provider was called with threading headers
    assert email_provider.send_count == 1
    assert len(email_provider.send_calls) == 1
    
    call = email_provider.send_calls[0]
    assert call['to_address'] == "alice@example.com"
    assert call['subject'] == "Re: Appointment question"
    
    # Verify threading headers present
    headers = call['headers']
    assert 'In-Reply-To' in headers
    assert headers['In-Reply-To'] == '<msg001@example.com>'
    assert 'References' in headers
    
    # Step 6: Verify no pending deliveries remain
    pending_after = persistence.get_pending_deliveries()
    assert len(pending_after) == 0


def test_llm_mode_visibility(temp_db, data_store, patient_lookup):
    """
    Verify LLM mode is visible and mock responses work correctly.
    
    Tests that:
    - Mock LLM client is used when provided
    - Responses are generated with LLM
    - Mode can be determined from configuration
    """
    conv_manager = ConversationManager(use_llm=False)
    email_provider = MockConversationEmailProvider()
    
    # Test with mock LLM
    mock_llm = MockLLMClient()
    
    email_manager_with_llm = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=mock_llm,
        clinic_domain="testclinic.example",
        db_path=temp_db,
        email_provider=email_provider
    )
    
    patient_lookup_dict = patient_lookup.get_patient_by_email_lookup()
    case_lookup_dict = patient_lookup.get_active_cases_lookup()
    
    # Generate reply with mock LLM
    result_llm = email_manager_with_llm.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Question",
        body="Can I schedule an appointment?",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup_dict,
        case_lookup=case_lookup_dict
    )
    
    # Verify reply was generated
    assert result_llm.success is True
    assert result_llm.reply_generated is True
    
    # Process delivery to get generated text
    email_manager_with_llm.process_pending_deliveries("test")
    llm_reply = email_provider.send_calls[0]['body']
    
    # Mock LLM response should contain appointment-related content
    assert "schedule" in llm_reply.lower() or "appointment" in llm_reply.lower()
    
    # Verify LLM client was called
    assert mock_llm.call_count > 0, "LLM client should have been called"
    
    # Test with no LLM (None)
    email_manager_no_llm = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=None,  # No LLM - will use template fallback
        clinic_domain="testclinic.example",
        db_path=temp_db + ".no_llm",
        email_provider=email_provider
    )
    
    # Verify manager recognizes no LLM
    assert email_manager_no_llm.llm_client is None, "Should have no LLM client"


def test_provider_interface_compatibility(temp_db, data_store, patient_lookup):
    """
    Verify email provider interface is compatible with existing provider pattern.
    
    Tests that conversation provider matches the interface used by reminders.
    """
    from agent.smtp_conversation_provider import SmtpConversationProvider
    from tools.providers import SmtpEmailProvider, SendRequest
    from tools.config import MessagingConfig
    
    # Create messaging config (will be unconfigured/simulated)
    config = MessagingConfig.from_env()
    smtp_provider = SmtpEmailProvider(config)
    
    # Wrap with conversation adapter
    conversation_provider = SmtpConversationProvider(smtp_provider)
    
    # Verify adapter has expected interface
    assert hasattr(conversation_provider, 'send')
    assert hasattr(conversation_provider, 'is_configured')
    
    # Test send method signature
    outcome = conversation_provider.send(
        to_address="test@example.com",
        subject="Test",
        body="Test body",
        headers={'In-Reply-To': '<original@example.com>'}
    )
    
    # Verify outcome structure
    assert hasattr(outcome, 'success')
    assert hasattr(outcome, 'message_id')
    assert hasattr(outcome, 'error_code')
    assert hasattr(outcome, 'error_message')
    assert hasattr(outcome, 'simulated')
    
    # Should be simulated (no real SMTP config)
    assert outcome.simulated is True
    assert outcome.success is True


def test_delivery_processor_idempotency(temp_db, data_store, patient_lookup):
    """
    Verify delivery processor can be run multiple times safely.
    
    Tests that:
    - Delivered messages are not resent
    - Claimed deliveries are not re-claimed
    - Multiple processor instances don't conflict
    """
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    email_provider = MockConversationEmailProvider()
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db,
        email_provider=email_provider
    )
    
    patient_lookup_dict = patient_lookup.get_patient_by_email_lookup()
    case_lookup_dict = patient_lookup.get_active_cases_lookup()
    
    # Generate reply
    email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Test",
        body="Test message",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup_dict,
        case_lookup=case_lookup_dict
    )
    
    # First processor run
    stats1 = email_manager.process_pending_deliveries("processor-1")
    assert stats1['delivered'] == 1
    assert email_provider.send_count == 1
    
    # Second processor run (should find nothing)
    stats2 = email_manager.process_pending_deliveries("processor-2")
    assert stats2['delivered'] == 0
    assert email_provider.send_count == 1  # No additional send
    
    # Third processor run (still nothing)
    stats3 = email_manager.process_pending_deliveries("processor-3")
    assert stats3['delivered'] == 0
    assert email_provider.send_count == 1  # Still no additional send
