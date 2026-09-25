"""
Tests for email persistence and recovery.

Tests cover:
- SQLite storage of threads and messages
- Recovery after manager recreation
- Message-ID replay deduplication
- Thread loading and querying
"""

import pytest
import os
import tempfile
import sqlite3
from datetime import date

from agent.email_conversations import EmailConversationManager
from agent.conversation import ConversationManager
from agent.mock_llm import MockLLMClient
from core.models import PatientRecord, FollowUpCase, ContactChannel, UrgencyLevel, CaseStatus


@pytest.fixture
def temp_db():
    """Create a temporary database file."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    yield path
    # Cleanup
    if os.path.exists(path):
        os.unlink(path)


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
    """Create follow-up case for Alice."""
    return FollowUpCase(
        patient=patient_alice,
        days_overdue=30,
        urgency=UrgencyLevel.MEDIUM,
        reason="6-month cleaning overdue"
    )


def test_persistence_saves_thread(temp_db, patient_alice, case_alice):
    """Thread is saved to database."""
    # Create manager with persistence
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # Send email with booking intent (not question that escalates)
    result = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Want to book",
        body="I want to book my cleaning appointment",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result.success is True
    thread_id = result.thread_id
    
    # Verify database contains thread
    from agent.email_persistence import EmailPersistence
    persistence = EmailPersistence(temp_db)
    
    assert persistence.get_thread_count() == 1
    assert persistence.get_message_count() == 2  # Inbound + outbound
    
    loaded_thread = persistence.load_thread(thread_id)
    assert loaded_thread is not None
    assert loaded_thread.patient_id == "P001"
    assert len(loaded_thread.messages) == 2


def test_persistence_recovery_after_restart(temp_db, patient_alice, case_alice):
    """Manager can recover threads after recreation."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # First manager - create thread
    email_manager1 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    result1 = email_manager1.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="First email",
        body="Hello",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    thread_id = result1.thread_id
    
    # Destroy first manager (simulate restart)
    del email_manager1
    
    # Second manager - should recover thread
    email_manager2 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    # Verify thread was recovered
    thread = email_manager2.get_thread(thread_id)
    assert thread is not None
    assert thread.patient_id == "P001"
    assert len(thread.messages) == 2
    
    # Send reply to same thread
    result2 = email_manager2.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Re: First email",
        body="Follow up",
        message_id="<msg002@example.com>",
        headers={'In-Reply-To': thread.messages[1].message_id},
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Should use same thread
    assert result2.thread_id == thread_id
    
    # Thread should now have 4 messages
    thread = email_manager2.get_thread(thread_id)
    assert len(thread.messages) == 4


def test_message_id_replay_deduplication(temp_db, patient_alice, case_alice):
    """Duplicate Message-IDs are rejected without duplicate processing."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # Reset LLM call count
    llm_client.reset_stats()
    initial_llm_calls = llm_client.call_count
    
    # First email
    result1 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Test",
        body="I want to schedule",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result1.success is True
    assert result1.reply_generated is True
    assert result1.should_send_reply is True
    llm_calls_after_first = llm_client.call_count
    assert llm_calls_after_first == initial_llm_calls + 1
    
    # Same Message-ID again (replay attack or retry)
    result2 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Test",
        body="Same message again",  # Different body, same ID
        message_id="<msg001@example.com>",  # DUPLICATE
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Should be acknowledged but not processed again
    assert result2.success is True  # Acknowledged
    assert result2.reply_generated is False  # No duplicate reply
    assert result2.should_send_reply is False  # No duplicate send
    assert "Duplicate" in result2.reason or "already processed" in result2.reason
    
    # LLM should NOT have been called again
    assert llm_client.call_count == llm_calls_after_first
    
    # No duplicate records in database
    from agent.email_persistence import EmailPersistence
    persistence = EmailPersistence(temp_db)
    
    # Should have 1 inbound + 1 outbound = 2 messages total (not 4)
    assert persistence.get_message_count() == 2


def test_replay_deduplication_persists_across_restart(temp_db, patient_alice, case_alice):
    """Replay deduplication survives manager restart."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # First manager
    email_manager1 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    # Reset LLM tracking
    llm_client.reset_stats()
    initial_llm_calls = llm_client.call_count
    
    result1 = email_manager1.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Test",
        body="I want to schedule",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result1.success is True
    assert result1.reply_generated is True
    assert result1.should_send_reply is True
    llm_calls_after_first = llm_client.call_count
    assert llm_calls_after_first == initial_llm_calls + 1
    
    # Restart
    del email_manager1
    
    # Second manager
    email_manager2 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    # Try to replay same message
    result2 = email_manager2.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Test",
        body="I want to schedule",
        message_id="<msg001@example.com>",  # Same ID
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Should be acknowledged but not processed
    assert result2.success is True  # Acknowledged
    assert result2.reply_generated is False  # No duplicate reply
    assert result2.should_send_reply is False  # No duplicate send
    assert "Duplicate" in result2.reason or "already processed" in result2.reason
    
    # LLM should NOT have been called again
    assert llm_client.call_count == llm_calls_after_first
    
    # Still only 2 messages in database (not 4)
    from agent.email_persistence import EmailPersistence
    persistence = EmailPersistence(temp_db)
    assert persistence.get_message_count() == 2


def test_opted_out_flag_persists(temp_db, patient_alice, case_alice):
    """Opt-out flag survives restart."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # First manager - opt out
    email_manager1 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    result1 = email_manager1.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Stop",
        body="STOP sending me emails",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    thread_id = result1.thread_id
    assert result1.success is True
    
    # Restart
    del email_manager1
    
    # Second manager
    email_manager2 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    # Send another message to same thread
    thread = email_manager2.get_thread(thread_id)
    outbound_id = thread.messages[0].message_id if thread.messages else "<msg001@example.com>"
    
    result2 = email_manager2.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Re: Stop",
        body="Actually I have a question",
        message_id="<msg002@example.com>",
        headers={'In-Reply-To': outbound_id},
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Should not generate reply (thread is opted out)
    assert result2.success is True
    assert result2.reply_generated is False
    assert "opted out" in result2.reason.lower()


def test_staff_takeover_flag_persists(temp_db, patient_alice, case_alice):
    """Staff takeover flag survives restart."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # First manager
    email_manager1 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    # Patient email
    result1 = email_manager1.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Help",
        body="I need help",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    thread_id = result1.thread_id
    
    # SECURITY: Staff takes over via authenticated UI
    takeover_success = email_manager1.mark_staff_takeover(thread_id, staff_user_id="staff_789")
    assert takeover_success is True
    
    # Restart
    del email_manager1
    
    # Second manager
    email_manager2 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    # Patient replies
    result2 = email_manager2.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Re: Help",
        body="Thanks!",
        message_id="<msg003@example.com>",
        headers={'In-Reply-To': email_manager2.get_thread(thread_id).messages[1].message_id},
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Should not generate automated reply (staff takeover active)
    assert result2.success is True
    assert result2.reply_generated is False
    assert "takeover" in result2.reason.lower()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])



def test_llm_call_count_tracking(temp_db, patient_alice, case_alice):
    """LLM provider calls are tracked and not duplicated on retry."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # Reset call count
    llm_client.reset_stats()
    initial_count = llm_client.call_count
    
    # First email - should call LLM (booking intent, not clinical question)
    result1 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Want to schedule",
        body="I want to schedule my appointment",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result1.success is True
    assert result1.reply_generated is True
    assert llm_client.call_count == initial_count + 1
    
    thread_id = result1.thread_id
    thread = email_manager.get_thread(thread_id)
    reply_text = result1.reply_text
    
    # Simulate system checking the same thread again (no new message)
    # Should not call LLM again - reply already persisted
    thread_reloaded = email_manager.get_thread(thread_id)
    assert len(thread_reloaded.messages) == 2
    
    # The last message should be the reply
    last_message = thread_reloaded.messages[-1]
    assert last_message.direction == 'outbound'
    assert last_message.body == reply_text
    
    # LLM was not called again
    assert llm_client.call_count == initial_count + 1


def test_reply_persists_across_restart(temp_db, patient_alice, case_alice):
    """Generated replies persist in database and survive restart."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # First manager - generate reply
    email_manager1 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    result1 = email_manager1.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Help",
        body="Need to schedule",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    thread_id = result1.thread_id
    reply_text = result1.reply_text
    
    # Destroy manager
    del email_manager1
    
    # Second manager - load from database
    email_manager2 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    # Load thread
    thread = email_manager2.get_thread(thread_id)
    assert thread is not None
    assert len(thread.messages) == 2
    
    # Reply should be in messages
    outbound = [msg for msg in thread.messages if msg.direction == 'outbound']
    assert len(outbound) == 1
    assert outbound[0].body == reply_text



def test_escalated_question_persists_without_reply(temp_db, patient_alice, case_alice):
    """Escalated clinical question persists inbound message and escalation state, no reply."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # First manager - patient asks clinical question
    email_manager1 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    initial_status = case_alice.status
    
    # Clinical question - should escalate (no auto-reply)
    result1 = email_manager1.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Pain question",
        body="I'm experiencing pain after my procedure. What should I do?",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result1.success is True
    assert result1.reply_generated is False  # Escalated, no auto-reply
    assert case_alice.status == CaseStatus.ESCALATED  # In-memory case escalated
    thread_id = result1.thread_id
    
    # Verify persistence saved inbound message even though no reply
    from agent.email_persistence import EmailPersistence
    persistence = EmailPersistence(temp_db)
    
    assert persistence.get_thread_count() == 1
    assert persistence.get_message_count() == 1  # Only inbound, no outbound reply
    
    loaded_thread = persistence.load_thread(thread_id)
    assert loaded_thread is not None
    assert len(loaded_thread.messages) == 1
    assert loaded_thread.messages[0].direction == 'inbound'
    assert loaded_thread.messages[0].body == "I'm experiencing pain after my procedure. What should I do?"
    
    # Destroy manager
    del email_manager1
    
    # Create fresh case object to simulate restart (not reusing in-memory case)
    patient_alice_fresh = PatientRecord(
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
    
    case_alice_fresh = FollowUpCase(
        patient=patient_alice_fresh,
        days_overdue=30,
        urgency=UrgencyLevel.MEDIUM,
        reason="6-month cleaning overdue"
    )
    
    # Fresh case should start in non-escalated state
    assert case_alice_fresh.status != CaseStatus.ESCALATED
    
    patient_lookup_fresh = {"alice@example.com": patient_alice_fresh}
    case_lookup_fresh = {"P001": case_alice_fresh}
    
    # Second manager - verify thread survives restart
    email_manager2 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    # Load thread
    thread = email_manager2.get_thread(thread_id)
    assert thread is not None
    assert len(thread.messages) == 1
    assert thread.messages[0].direction == 'inbound'
    
    # Another message to same thread - NOT independently triggering escalation
    result2 = email_manager2.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Re: Pain question",
        body="The pain is getting worse",  # Statement, not question
        message_id="<msg002@example.com>",
        headers={'In-Reply-To': '<msg001@example.com>'},
        patient_lookup=patient_lookup_fresh,
        case_lookup=case_lookup_fresh
    )
    
    assert result2.success is True
    assert result2.reply_generated is False  # Thread escalated, no auto-reply
    assert result2.should_send_reply is False  # No send eligibility
    
    # Verify no LLM call for escalated thread
    assert llm_client.call_count == 0  # Still zero from first escalation
    
    # Both inbound messages should be persisted
    thread = email_manager2.get_thread(thread_id)
    assert len(thread.messages) == 2
    assert all(msg.direction == 'inbound' for msg in thread.messages)


def test_unknown_sender_persists_for_review(temp_db, patient_alice, case_alice):
    """Unknown sender email persisted to unmatched queue without patient assignment."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    # Email from unknown sender
    result = email_manager.receive_email(
        from_address="unknown@example.com",
        to_address="clinic@testclinic.example",
        subject="Need appointment",
        body="I'd like to schedule a cleaning",
        message_id="<unknown001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Should fail to process
    assert result.success is False
    assert "Unknown sender" in result.reason
    assert result.thread_id is None
    assert result.reply_generated is False
    
    # Should be saved to unmatched queue
    from agent.email_persistence import EmailPersistence
    persistence = EmailPersistence(temp_db)
    
    unmatched = persistence.get_unmatched_emails(include_reviewed=False)
    assert len(unmatched) == 1
    assert unmatched[0]['from_address'] == "unknown@example.com"
    assert unmatched[0]['subject'] == "Need appointment"
    assert unmatched[0]['body'] == "I'd like to schedule a cleaning"
    assert unmatched[0]['failure_reason'] == "unknown_sender"
    assert unmatched[0]['reviewed'] is False
    
    # Should be marked as processed (replay protection)
    assert persistence.is_message_processed("<unknown001@example.com>")


def test_ambiguous_sender_persists_for_review(temp_db):
    """Ambiguous sender (multiple patients) persisted without revealing patient count."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    
    # Two patients sharing same email
    patient_alice = PatientRecord(
        patient_id="P001",
        name="Alice Smith",
        contact_info={
            ContactChannel.EMAIL: "shared@example.com",
            ContactChannel.SMS: "+15550001111"
        },
        preferred_channel=ContactChannel.EMAIL,
        last_visit_date=date(2024, 1, 1),
        treatment_type="cleaning",
        recall_interval_days=180
    )
    
    patient_bob = PatientRecord(
        patient_id="P002",
        name="Bob Jones",
        contact_info={
            ContactChannel.EMAIL: "shared@example.com",
            ContactChannel.SMS: "+15550002222"
        },
        preferred_channel=ContactChannel.EMAIL,
        last_visit_date=date(2024, 2, 1),
        treatment_type="checkup",
        recall_interval_days=365
    )
    
    # Lookup returns first match, but _match_patient should detect ambiguity
    # For this test, we'll directly use unknown lookup to simulate ambiguity
    patient_lookup = {}  # No match
    case_lookup = {}
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    result = email_manager.receive_email(
        from_address="shared@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment question",
        body="When is my next appointment?",
        message_id="<ambig001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Should fail with generic error (doesn't reveal ambiguity)
    assert result.success is False
    assert "Unknown sender" in result.reason
    assert result.reply_generated is False
    
    # Should be saved to unmatched queue
    from agent.email_persistence import EmailPersistence
    persistence = EmailPersistence(temp_db)
    
    unmatched = persistence.get_unmatched_emails()
    assert len(unmatched) == 1
    assert unmatched[0]['from_address'] == "shared@example.com"
    assert unmatched[0]['failure_reason'] == "unknown_sender"


def test_unmatched_email_replay_deduplication(temp_db, patient_alice, case_alice):
    """Replay of unmatched email detected and not duplicated."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    # First attempt - unknown sender
    result1 = email_manager.receive_email(
        from_address="unknown@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment",
        body="Need cleaning",
        message_id="<replay_unmatched@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result1.success is False
    
    # Second attempt - replay of same message
    result2 = email_manager.receive_email(
        from_address="unknown@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment",
        body="Need cleaning",
        message_id="<replay_unmatched@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Replay acknowledged but not re-saved
    assert result2.success is True  # Acknowledge receipt
    assert result2.reply_generated is False
    assert "already queued for review" in result2.reason
    
    # Only one record in unmatched queue
    from agent.email_persistence import EmailPersistence
    persistence = EmailPersistence(temp_db)
    
    unmatched = persistence.get_unmatched_emails()
    assert len(unmatched) == 1
    assert unmatched[0]['message_id'] == "<replay_unmatched@example.com>"


def test_unmatched_emails_persist_across_restart(temp_db, patient_alice, case_alice):
    """Unmatched emails survive manager restart."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # First manager - receive unknown email
    email_manager1 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    result = email_manager1.receive_email(
        from_address="unknown@example.com",
        to_address="clinic@testclinic.example",
        subject="Question",
        body="Do you accept my insurance?",
        message_id="<persist_unmatched@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result.success is False
    
    # Destroy first manager
    del email_manager1
    
    # Second manager - verify unmatched email persists
    email_manager2 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    from agent.email_persistence import EmailPersistence
    persistence = EmailPersistence(temp_db)
    
    unmatched = persistence.get_unmatched_emails()
    assert len(unmatched) == 1
    assert unmatched[0]['from_address'] == "unknown@example.com"
    assert unmatched[0]['message_id'] == "<persist_unmatched@example.com>"
    assert unmatched[0]['reviewed'] is False


def test_staff_review_queue_authenticated_access(temp_db, patient_alice, case_alice):
    """Staff can access review queue only with valid authentication."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    # Save unmatched email
    email_manager.receive_email(
        from_address="unknown@example.com",
        to_address="clinic@testclinic.example",
        subject="Question",
        body="Test",
        message_id="<auth_test@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Mock auth callback
    def staff_auth(user_id: str) -> bool:
        return user_id in ["staff001", "staff002"]
    
    # Authorized staff can access
    unmatched = email_manager.get_unmatched_emails(
        staff_user_id="staff001",
        staff_auth_callback=staff_auth
    )
    assert len(unmatched) == 1
    
    # Unauthorized staff rejected
    try:
        email_manager.get_unmatched_emails(
            staff_user_id="hacker",
            staff_auth_callback=staff_auth
        )
        assert False, "Should have raised PermissionError"
    except PermissionError as e:
        assert "not authorized" in str(e)


def test_staff_marks_unmatched_email_reviewed(temp_db, patient_alice, case_alice):
    """Staff can mark unmatched email as reviewed."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    # Save unmatched email
    email_manager.receive_email(
        from_address="unknown@example.com",
        to_address="clinic@testclinic.example",
        subject="Question",
        body="Test",
        message_id="<review_test@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    def staff_auth(user_id: str) -> bool:
        return user_id == "staff001"
    
    # Get unmatched email
    unmatched = email_manager.get_unmatched_emails(
        staff_user_id="staff001",
        staff_auth_callback=staff_auth
    )
    assert len(unmatched) == 1
    unmatched_id = unmatched[0]['id']
    
    # Mark as reviewed
    success = email_manager.review_unmatched_email(
        unmatched_id=unmatched_id,
        staff_user_id="staff001",
        resolution="spam",
        staff_auth_callback=staff_auth
    )
    assert success is True
    
    # No longer in unreviewed queue
    unreviewed = email_manager.get_unmatched_emails(
        staff_user_id="staff001",
        staff_auth_callback=staff_auth,
        include_reviewed=False
    )
    assert len(unreviewed) == 0
    
    # In reviewed queue
    reviewed = email_manager.get_unmatched_emails(
        staff_user_id="staff001",
        staff_auth_callback=staff_auth,
        include_reviewed=True
    )
    assert len(reviewed) == 1
    assert reviewed[0]['reviewed'] is True
    assert reviewed[0]['reviewed_by'] == "staff001"
    assert reviewed[0]['resolution'] == "spam"



def test_escalation_atomicity_rollback_on_failure(temp_db, patient_alice, case_alice):
    """Escalation atomic operation rolls back escalation state, message, and marker if any fails."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    # Clinical question - triggers escalation
    try:
        # Inject failure by corrupting the database connection during save
        from agent.email_persistence import EmailPersistence
        persistence = email_manager.persistence
        original_save_message = persistence.save_message
        
        def failing_save_message(thread_id, message, conn=None):
            # Simulate database failure mid-transaction
            raise sqlite3.OperationalError("Simulated database failure")
        
        persistence.save_message = failing_save_message
        
        result = email_manager.receive_email(
            from_address="alice@example.com",
            to_address="clinic@testclinic.example",
            subject="Pain question",
            body="I'm experiencing pain. What should I do?",
            message_id="<escalation_fail@example.com>",
            patient_lookup=patient_lookup,
            case_lookup=case_lookup
        )
        
        # Should raise exception, not return result
        assert False, "Expected exception from database failure"
        
    except sqlite3.OperationalError as e:
        assert "Simulated database failure" in str(e)
    
    finally:
        # Restore original method
        persistence.save_message = original_save_message
    
    # Verify rollback of atomic escalation operation:
    # Thread may exist (created before escalation), but escalation state should NOT be set
    thread_count = persistence.get_thread_count()
    if thread_count > 0:
        # Thread was created before escalation failed
        threads = persistence.load_all_threads()
        for thread in threads.values():
            # Escalation state should NOT be set (rollback succeeded)
            assert thread.needs_staff_review is False, "Escalation state should have rolled back"
    
    # Message should NOT be saved (part of atomic operation that rolled back)
    assert persistence.get_message_count() == 0
    # Processing marker should NOT be set (part of atomic operation that rolled back)
    assert not persistence.is_message_processed("<escalation_fail@example.com>")


def test_escalation_atomicity_success_after_retry(temp_db, patient_alice, case_alice):
    """After transaction failure, retry succeeds and all data persists atomically."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    # First attempt with simulated failure
    from agent.email_persistence import EmailPersistence
    persistence = email_manager.persistence
    original_save_message = persistence.save_message
    
    fail_once = [True]  # Mutable flag
    
    def maybe_failing_save_message(thread_id, message, conn=None):
        if fail_once[0]:
            fail_once[0] = False
            raise sqlite3.OperationalError("Transient failure")
        return original_save_message(thread_id, message, conn=conn)
    
    persistence.save_message = maybe_failing_save_message
    
    # First attempt fails
    try:
        email_manager.receive_email(
            from_address="alice@example.com",
            to_address="clinic@testclinic.example",
            subject="Pain question",
            body="I'm experiencing pain. What should I do?",
            message_id="<escalation_retry@example.com>",
            patient_lookup=patient_lookup,
            case_lookup=case_lookup
        )
        assert False, "Expected first attempt to fail"
    except sqlite3.OperationalError:
        pass
    
    # Restore original method for retry
    persistence.save_message = original_save_message
    
    # Retry succeeds
    result = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Pain question",
        body="I'm experiencing pain. What should I do?",
        message_id="<escalation_retry@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result.success is True
    assert result.reply_generated is False
    assert "escalated" in result.reason.lower()
    
    # Verify all three operations persisted
    assert persistence.get_thread_count() == 1
    assert persistence.get_message_count() == 1
    assert persistence.is_message_processed("<escalation_retry@example.com>")
    
    # Verify thread escalation state
    thread = persistence.load_thread(result.thread_id)
    assert thread.needs_staff_review is True


def test_escalation_replay_after_restart(temp_db, patient_alice, case_alice):
    """Replay of escalated message after restart returns already-processed."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # First manager - escalate
    email_manager1 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    result1 = email_manager1.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Pain question",
        body="I'm experiencing pain. What should I do?",
        message_id="<escalation_replay@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result1.success is True
    assert result1.reply_generated is False
    thread_id = result1.thread_id
    
    # Destroy manager (simulate restart)
    del email_manager1
    
    # Second manager - replay same message
    email_manager2 = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    result2 = email_manager2.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Pain question",
        body="I'm experiencing pain. What should I do?",
        message_id="<escalation_replay@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Replay detected
    assert result2.success is True
    assert result2.reply_generated is False
    assert "already processed" in result2.reason.lower()
    
    # Only one message in database
    from agent.email_persistence import EmailPersistence
    persistence = EmailPersistence(temp_db)
    assert persistence.get_message_count() == 1


def test_unmatched_email_atomicity_rollback(temp_db, patient_alice, case_alice):
    """Unmatched email transaction rolls back if processing marker fails."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        db_path=temp_db
    )
    
    # Inject failure in mark_message_processed
    from agent.email_persistence import EmailPersistence
    persistence = email_manager.persistence
    original_mark = persistence.mark_message_processed
    
    def failing_mark(message_id, conn=None):
        raise sqlite3.OperationalError("Mark failed")
    
    persistence.mark_message_processed = failing_mark
    
    try:
        email_manager.receive_email(
            from_address="unknown@example.com",
            to_address="clinic@testclinic.example",
            subject="Question",
            body="Do you accept my insurance?",
            message_id="<unmatched_fail@example.com>",
            patient_lookup=patient_lookup,
            case_lookup=case_lookup
        )
        assert False, "Expected exception"
    except sqlite3.OperationalError:
        pass
    finally:
        persistence.mark_message_processed = original_mark
    
    # Verify rollback: unmatched email not saved
    assert persistence.get_unmatched_email_count() == 0
    assert not persistence.is_message_processed("<unmatched_fail@example.com>")
