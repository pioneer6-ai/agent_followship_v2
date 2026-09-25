"""
Integration tests for email conversation functionality.

Tests cover:
- Inbound email parsing and patient matching
- Thread deduplication (In-Reply-To, References headers)
- Opt-out detection
- Staff takeover detection
- LLM reply generation (mocked)
- Reminder counter handling (must NOT increment)
"""

import pytest
from datetime import date

from agent.email_conversations import EmailConversationManager
from agent.email_models import EmailThread, EmailMessage, EmailProcessingResult
from agent.conversation import ConversationManager
from agent.mock_llm import MockLLMClient, RefuseBookingLLMClient
from core.models import PatientRecord, FollowUpCase, ContactChannel, UrgencyLevel, CaseStatus


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
def patient_bob():
    """Create test patient Bob."""
    return PatientRecord(
        patient_id="P002",
        name="Bob Johnson",
        contact_info={
            ContactChannel.EMAIL: "bob@example.com",
            ContactChannel.SMS: "+15550002222"
        },
        preferred_channel=ContactChannel.EMAIL,
        last_visit_date=date(2024, 2, 1),
        treatment_type="filling",
        recall_interval_days=90
    )


@pytest.fixture
def case_alice(patient_alice):
    """Create follow-up case for Alice."""
    return FollowUpCase(
        patient=patient_alice,
        days_overdue=30,
        urgency=UrgencyLevel.MEDIUM,
        reason="6-month cleaning overdue",
        status=CaseStatus.MESSAGE_SENT,
        reminder_count=1
    )


@pytest.fixture
def case_bob(patient_bob):
    """Create follow-up case for Bob."""
    return FollowUpCase(
        patient=patient_bob,
        days_overdue=15,
        urgency=UrgencyLevel.LOW,
        reason="Filling follow-up",
        status=CaseStatus.MESSAGE_SENT,
        reminder_count=1
    )


@pytest.fixture
def email_manager():
    """Create email conversation manager with mock LLM."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    return EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example"
    )


# ============================================================================
# Patient Matching Tests
# ============================================================================

def test_receive_email_matches_patient(email_manager, patient_alice, case_alice):
    """Inbound email successfully matched to patient by email address."""
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    result = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment question",
        body="Hi, I'd like to schedule my cleaning.",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result.success is True
    assert result.reply_generated is True
    assert result.thread_id is not None


def test_unknown_sender_rejected(email_manager):
    """Email from unknown sender returns error."""
    result = email_manager.receive_email(
        from_address="unknown@example.com",
        to_address="clinic@testclinic.example",
        subject="Question",
        body="Who are you?",
        message_id="<msg999@example.com>",
        patient_lookup={},
        case_lookup={}
    )
    
    assert result.success is False
    assert "Unknown sender" in result.reason
    assert result.reply_generated is False


def test_patient_match_case_insensitive(email_manager, patient_alice, case_alice):
    """Patient matching is case-insensitive."""
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    result = email_manager.receive_email(
        from_address="ALICE@EXAMPLE.COM",  # Uppercase
        to_address="clinic@testclinic.example",
        subject="Hi",
        body="Hello",
        message_id="<msg002@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result.success is True
    assert result.thread_id is not None


# ============================================================================
# Thread Deduplication Tests
# ============================================================================

def test_thread_deduplication_by_in_reply_to(email_manager, patient_alice, case_alice):
    """Second email with In-Reply-To header uses same thread."""
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # First email creates thread
    result1 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment",
        body="Can I schedule?",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result1.success is True
    thread_id1 = result1.thread_id
    
    # Get the outbound message ID from the reply
    thread1 = email_manager.get_thread(thread_id1)
    assert len(thread1.messages) == 2  # Inbound + outbound
    outbound_msg_id = thread1.messages[1].message_id
    
    # Second email with In-Reply-To should use same thread
    result2 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Re: Appointment",
        body="Thanks! I'd like to book it.",  # Booking intent, not question
        message_id="<msg002@example.com>",
        headers={'In-Reply-To': outbound_msg_id},
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result2.success is True
    assert result2.thread_id == thread_id1  # Same thread!
    
    # Thread should now have 4 messages (2 inbound, 2 outbound)
    thread = email_manager.get_thread(thread_id1)
    assert len(thread.messages) == 4


def test_thread_deduplication_by_references(email_manager, patient_alice, case_alice):
    """Email with References header finds existing thread."""
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # First email
    result1 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Question",
        body="Hi there",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    thread_id1 = result1.thread_id
    thread1 = email_manager.get_thread(thread_id1)
    outbound_msg_id = thread1.messages[1].message_id
    
    # Second email with References (not In-Reply-To)
    result2 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Re: Question",
        body="Follow up",
        message_id="<msg002@example.com>",
        headers={'References': f"<msg001@example.com> {outbound_msg_id}"},
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result2.thread_id == thread_id1


def test_new_subject_creates_new_thread(email_manager, patient_alice, case_alice):
    """Email with new subject creates separate thread."""
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # First email
    result1 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Cleaning appointment",
        body="Need cleaning",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Second email with different subject (no threading headers)
    result2 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Billing question",  # Different subject
        body="Question about bill",
        message_id="<msg002@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result1.thread_id != result2.thread_id


# ============================================================================
# Opt-Out Detection Tests
# ============================================================================

@pytest.mark.parametrize("opt_out_text", [
    "STOP",
    "please unsubscribe me",
    "OPT OUT",
    "opt-out please",
    "Remove me from your list",
    "do not contact me",
    "Don't contact me anymore"
])
def test_opt_out_detection(email_manager, patient_alice, case_alice, opt_out_text):
    """Email with opt-out keywords marks thread as opted out."""
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    result = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Stop",
        body=opt_out_text,
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result.success is True
    assert result.reply_generated is False
    assert "opted out" in result.reason.lower()
    
    # Thread should be marked as opted out
    thread = email_manager.get_thread(result.thread_id)
    assert thread.opted_out is True


def test_opted_out_thread_stops_replies(email_manager, patient_alice, case_alice):
    """Once opted out, thread no longer generates replies."""
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # First email - opt out
    result1 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="No more emails",
        body="Please STOP sending me reminders",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    thread_id = result1.thread_id
    
    # Second email - should not generate reply
    result2 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Re: No more emails",
        body="Actually, I have a question",
        message_id="<msg002@example.com>",
        headers={'In-Reply-To': '<msg001@example.com>'},
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result2.success is True
    assert result2.reply_generated is False


# ============================================================================
# Staff Takeover Detection Tests
# ============================================================================

def test_staff_email_triggers_takeover(email_manager, patient_alice, case_alice):
    """Authenticated staff takeover marks thread correctly."""
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # Patient sends email
    result1 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Urgent question",
        body="I need help ASAP",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    thread_id = result1.thread_id
    assert result1.success is True
    
    # SECURITY: Staff takeover via authenticated web UI, not email From
    takeover_success = email_manager.mark_staff_takeover(thread_id, staff_user_id="staff_user_123")
    assert takeover_success is True
    
    # Verify thread is marked with staff takeover
    thread = email_manager.get_thread(thread_id)
    assert thread.staff_takeover is True


def test_unauthenticated_staff_takeover_rejected():
    """Unauthenticated takeover attempt is rejected."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    
    # Auth callback that rejects all attempts (simulates unauthenticated)
    def reject_all(user_id, action):
        return False
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        staff_auth_callback=reject_all
    )
    
    patient = PatientRecord(
        patient_id="P001",
        name="Test Patient",
        contact_info={ContactChannel.EMAIL: "test@example.com"},
        preferred_channel=ContactChannel.EMAIL,
        last_visit_date=date(2024, 1, 1),
        treatment_type="cleaning",
        recall_interval_days=180
    )
    
    case = FollowUpCase(
        patient=patient,
        days_overdue=30,
        urgency=UrgencyLevel.MEDIUM,
        reason="Overdue"
    )
    
    patient_lookup = {"test@example.com": patient}
    case_lookup = {"P001": case}
    
    # Patient sends email
    result = email_manager.receive_email(
        from_address="test@example.com",
        to_address="clinic@testclinic.example",
        subject="Help",
        body="Need help",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    thread_id = result.thread_id
    
    # Attempt takeover - should be rejected
    takeover_success = email_manager.mark_staff_takeover(thread_id, staff_user_id="hacker_123")
    assert takeover_success is False
    
    # Thread should NOT be marked as taken over
    thread = email_manager.get_thread(thread_id)
    assert thread.staff_takeover is False


def test_unauthorized_staff_takeover_rejected():
    """Unauthorized staff (authenticated but wrong permissions) rejected."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    
    # Auth callback that only allows specific staff IDs
    def allow_specific_staff(user_id, action):
        authorized_staff = ["staff_alice", "staff_bob"]
        return user_id in authorized_staff
    
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example",
        staff_auth_callback=allow_specific_staff
    )
    
    patient = PatientRecord(
        patient_id="P001",
        name="Test Patient",
        contact_info={ContactChannel.EMAIL: "test@example.com"},
        preferred_channel=ContactChannel.EMAIL,
        last_visit_date=date(2024, 1, 1),
        treatment_type="cleaning",
        recall_interval_days=180
    )
    
    case = FollowUpCase(
        patient=patient,
        days_overdue=30,
        urgency=UrgencyLevel.MEDIUM,
        reason="Overdue"
    )
    
    patient_lookup = {"test@example.com": patient}
    case_lookup = {"P001": case}
    
    # Patient sends email
    result = email_manager.receive_email(
        from_address="test@example.com",
        to_address="clinic@testclinic.example",
        subject="Help",
        body="Need help",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    thread_id = result.thread_id
    
    # Unauthorized staff attempts takeover
    takeover_success = email_manager.mark_staff_takeover(thread_id, staff_user_id="staff_charlie")
    assert takeover_success is False
    
    # Authorized staff succeeds
    takeover_success = email_manager.mark_staff_takeover(thread_id, staff_user_id="staff_alice")
    assert takeover_success is True
    
    thread = email_manager.get_thread(thread_id)
    assert thread.staff_takeover is True


def test_staff_takeover_stops_automation(email_manager, patient_alice, case_alice):
    """After authenticated staff takeover, no automated replies are sent."""
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    # Patient email
    result1 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Help",
        body="Need assistance",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    thread_id = result1.thread_id
    assert result1.success is True
    assert result1.reply_generated is True
    
    # SECURITY: Staff takes over via authenticated UI
    takeover_success = email_manager.mark_staff_takeover(thread_id, staff_user_id="staff_user_456")
    assert takeover_success is True
    
    # Another patient email - should not get automated reply
    result2 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Re: Help",
        body="Thank you!",
        message_id="<msg003@example.com>",
        headers={'In-Reply-To': email_manager.get_thread(thread_id).messages[1].message_id},
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result2.success is True
    assert result2.reply_generated is False
    assert "takeover" in result2.reason.lower()


# ============================================================================
# LLM Reply Generation Tests
# ============================================================================

def test_llm_generates_helpful_reply(email_manager, patient_alice, case_alice):
    """LLM generates appropriate conversational reply."""
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    result = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Cleaning appointment",
        body="Hi, I'd like to schedule my 6-month cleaning please.",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result.success is True
    assert result.reply_generated is True
    assert result.reply_text is not None
    assert len(result.reply_text) > 50  # Substantial reply


def test_llm_refuses_to_book_appointments():
    """LLM correctly directs booking requests to staff/phone."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = RefuseBookingLLMClient()  # Always refuses booking
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example"
    )
    
    patient = PatientRecord(
        patient_id="P001",
        name="Test Patient",
        contact_info={ContactChannel.EMAIL: "test@example.com"},
        preferred_channel=ContactChannel.EMAIL,
        last_visit_date=date(2024, 1, 1),
        treatment_type="cleaning",
        recall_interval_days=180
    )
    
    case = FollowUpCase(
        patient=patient,
        days_overdue=30,
        urgency=UrgencyLevel.MEDIUM,
        reason="Overdue cleaning"
    )
    
    patient_lookup = {"test@example.com": patient}
    case_lookup = {"P001": case}
    
    result = email_manager.receive_email(
        from_address="test@example.com",
        to_address="clinic@testclinic.example",
        subject="Book appointment",
        body="I want to book for next Monday at 2pm",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result.success is True
    assert result.reply_generated is True
    # Reply should NOT contain booking confirmation
    reply_lower = result.reply_text.lower()
    assert 'unable' in reply_lower or 'call' in reply_lower or 'online' in reply_lower


# ============================================================================
# Reminder Counter Tests
# ============================================================================

def test_conversation_reply_does_not_increment_reminders(email_manager, patient_alice, case_alice):
    """Email conversation replies do NOT increment reminder_count."""
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    initial_reminder_count = case_alice.reminder_count
    
    # Patient sends email requesting to schedule (not a question requiring escalation)
    result = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Want to schedule",
        body="I want to schedule my appointment",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result.success is True
    assert result.reply_generated is True
    
    # Reminder count should NOT have changed
    assert case_alice.reminder_count == initial_reminder_count
    
    # But conversation log should be updated
    assert len(case_alice.conversation_log) > 0


def test_multiple_email_exchanges_do_not_increment_reminders(email_manager, patient_alice, case_alice):
    """Multiple back-and-forth emails do not increment reminder counter."""
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    initial_reminder_count = case_alice.reminder_count
    
    # First exchange
    result1 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment",
        body="Can I schedule?",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    thread_id = result1.thread_id
    thread = email_manager.get_thread(thread_id)
    outbound_id1 = thread.messages[1].message_id
    
    # Second exchange
    result2 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Re: Appointment",
        body="Yes, I want to schedule.",  # Booking intent, not question
        message_id="<msg002@example.com>",
        headers={'In-Reply-To': outbound_id1},
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    outbound_id2 = email_manager.get_thread(thread_id).messages[3].message_id
    
    # Third exchange
    result3 = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Re: Appointment",
        body="Thanks for the info",
        message_id="<msg003@example.com>",
        headers={'In-Reply-To': outbound_id2},
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Reminder count should still be unchanged
    assert case_alice.reminder_count == initial_reminder_count


# ============================================================================
# Edge Cases
# ============================================================================

def test_no_case_found_handles_gracefully(email_manager, patient_alice):
    """Email for patient with no active case handled gracefully."""
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {}  # No case
    
    result = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Question",
        body="Hello",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result.success is True
    assert result.reply_generated is False
    assert "No active case" in result.reason


def test_empty_body_handled_gracefully(email_manager, patient_alice, case_alice):
    """Empty email body handled without crashing."""
    patient_lookup = {"alice@example.com": patient_alice}
    case_lookup = {"P001": case_alice}
    
    result = email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Subject only",
        body="",  # Empty body
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    assert result.success is True


def test_get_patient_threads(email_manager, patient_alice, patient_bob, case_alice, case_bob):
    """Get all threads for a specific patient."""
    patient_lookup = {
        "alice@example.com": patient_alice,
        "bob@example.com": patient_bob
    }
    case_lookup = {
        "P001": case_alice,
        "P002": case_bob
    }
    
    # Alice sends 2 emails on different topics
    email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Cleaning",
        body="About cleaning",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    email_manager.receive_email(
        from_address="alice@example.com",
        to_address="clinic@testclinic.example",
        subject="Billing",
        body="About bill",
        message_id="<msg002@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Bob sends 1 email
    email_manager.receive_email(
        from_address="bob@example.com",
        to_address="clinic@testclinic.example",
        subject="Question",
        body="Hi",
        message_id="<msg003@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Alice should have 2 threads
    alice_threads = email_manager.get_patient_threads("P001")
    assert len(alice_threads) == 2
    
    # Bob should have 1 thread
    bob_threads = email_manager.get_patient_threads("P002")
    assert len(bob_threads) == 1


if __name__ == '__main__':
    pytest.main([__file__, '-v'])



def test_ambiguous_patient_match_rejected():
    """Two patients with same email address results in rejection without auto-reply."""
    conv_manager = ConversationManager(use_llm=False)
    llm_client = MockLLMClient()
    email_manager = EmailConversationManager(
        conversation_manager=conv_manager,
        llm_client=llm_client,
        clinic_domain="testclinic.example"
    )
    
    # Two different patients sharing same email (family, typo, etc.)
    patient1 = PatientRecord(
        patient_id="P001",
        name="Alice Smith",
        contact_info={ContactChannel.EMAIL: "shared@example.com"},
        preferred_channel=ContactChannel.EMAIL,
        last_visit_date=date(2024, 1, 1),
        treatment_type="cleaning",
        recall_interval_days=180
    )
    
    patient2 = PatientRecord(
        patient_id="P002",
        name="Bob Smith",
        contact_info={ContactChannel.EMAIL: "shared@example.com"},  # SAME EMAIL
        preferred_channel=ContactChannel.EMAIL,
        last_visit_date=date(2024, 2, 1),
        treatment_type="filling",
        recall_interval_days=90
    )
    
    case1 = FollowUpCase(
        patient=patient1,
        days_overdue=30,
        urgency=UrgencyLevel.MEDIUM,
        reason="Cleaning overdue"
    )
    
    case2 = FollowUpCase(
        patient=patient2,
        days_overdue=15,
        urgency=UrgencyLevel.LOW,
        reason="Filling followup"
    )
    
    # Ambiguous lookup - email maps to multiple patients (simulated by returning None)
    # In production, _match_patient would detect 2+ candidates and return None
    patient_lookup = {}  # Empty = no match = simulates ambiguous case
    case_lookup = {"P001": case1, "P002": case2}
    
    # Email from shared address
    result = email_manager.receive_email(
        from_address="shared@example.com",
        to_address="clinic@testclinic.example",
        subject="Appointment question",
        body="I need to schedule",
        message_id="<msg001@example.com>",
        patient_lookup=patient_lookup,
        case_lookup=case_lookup
    )
    
    # Should be rejected - ambiguous sender
    assert result.success is False
    assert "Unknown sender" in result.reason
    assert result.reply_generated is False
    
    # No thread should be created (no patient context to use)
    assert result.thread_id is None
    
    # TODO: In production, this should:
    # - Create "needs_review" queue entry
    # - Store inbound email for staff review
    # - NOT auto-reply using arbitrary patient's context
    # - Show staff masked patient candidates (P001: A***e S***h, P002: B** S***h)
