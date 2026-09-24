"""
Tests for the demo safety-signal extraction in agent.conversation.

IMPORTANT: the emergency/opt-out keyword detection tested here is a small,
configurable DEMO SAFETY RULE (see the module note in agent/conversation.py),
not a clinical triage system. These tests confirm the plumbing - that
signals reach `handle_reply`'s returned context so the orchestrator can hand
them to PolicyGuard - not clinical completeness of the keyword list.
"""

import pytest

from core.actions import AgentAction
from core.models import CaseStatus
from agent.conversation import is_emergency_signal, is_opt_out_signal

from tests.conftest import make_case


class TestEmergencyKeywordDetection:
    def test_recognizes_configured_emergency_keywords(self):
        assert is_emergency_signal("this is an emergency, please help")
        assert is_emergency_signal("i have severe bleeding")
        assert is_emergency_signal("having chest pain right now")

    def test_ordinary_messages_are_not_flagged(self):
        assert not is_emergency_signal("yes please book me in")
        assert not is_emergency_signal("what is the cost of a cleaning")

    @pytest.mark.parametrize(
        "message",
        [
            "Stop messaging me, I'm bleeding badly",
            "My bleeding won't stop",
            "My face is badly swollen",
            "I'm having trouble breathing",
            "The pain is unbearable",
        ],
    )
    def test_recognizes_natural_language_emergency_variants(self, message):
        """
        Regression coverage for the gap found in manual smoke testing:
        "Stop messaging me, I'm bleeding badly" was not recognized as an
        emergency because "bleeding badly" is not the same phrase as
        "severe bleeding". Emergency detection must cover common
        natural-language variants of each existing concept
        (bleeding/breathing/swelling/pain), not just one fixed wording.
        """
        assert is_emergency_signal(message.lower()), (
            f"{message!r} must be recognized as a potential emergency"
        )

    @pytest.mark.parametrize(
        "message",
        [
            "I had a little bleeding after brushing",
            "My gums are slightly swollen",
        ],
    )
    def test_mild_symptom_mentions_are_not_automatically_emergencies(self, message):
        """
        The expanded pattern set must remain conservative: ordinary, mild
        symptom mentions (no intensity/duration/uncontrollability language)
        must not trigger emergency escalation.
        """
        assert not is_emergency_signal(message.lower()), (
            f"{message!r} must NOT be treated as an emergency"
        )


class TestOptOutKeywordDetection:
    def test_recognizes_configured_opt_out_keywords(self):
        assert is_opt_out_signal("stop")
        assert is_opt_out_signal("please unsubscribe me")
        assert is_opt_out_signal("do not contact me again")

    def test_ordinary_messages_are_not_flagged(self):
        assert not is_opt_out_signal("yes please book me in")


class TestHandleReplySignals:
    def test_emergency_message_surfaces_is_emergency_signal(self, conversation_manager):
        case = make_case()
        action, context = conversation_manager.handle_reply(
            case, "I have severe bleeding, this is an emergency"
        )
        assert context["signals"]["is_emergency"] is True

    def test_opt_out_message_surfaces_is_opt_out_signal_and_proposes_record(
        self, conversation_manager
    ):
        case = make_case()
        action, context = conversation_manager.handle_reply(case, "stop contacting me")
        assert context["signals"]["is_opt_out"] is True
        assert action == AgentAction.RECORD_OPT_OUT

    def test_affirmative_reply_has_affirmative_consent_signal(self, conversation_manager):
        case = make_case()
        action, context = conversation_manager.handle_reply(case, "yes, book it")
        assert context["signals"]["consent_signal"] == "affirmative"
        assert action == AgentAction.CONFIRM_BOOKING

    def test_contradictory_reply_has_ambiguous_consent_signal(self, conversation_manager):
        case = make_case()
        action, context = conversation_manager.handle_reply(
            case, "yes but not this week, I'm busy"
        )
        assert context["signals"]["consent_signal"] == "ambiguous"
        assert action == AgentAction.CONFIRM_BOOKING

    def test_question_surfaces_is_clinical_question_signal(self, conversation_manager):
        case = make_case()
        action, context = conversation_manager.handle_reply(
            case, "what is the cost of this treatment?"
        )
        assert context["signals"]["is_clinical_question"] is True
        assert action == AgentAction.ESCALATE_TO_STAFF

    def test_proposing_an_action_does_not_mutate_case_status(self, conversation_manager):
        """Status mutation is a side effect; it must wait for PolicyGuard's
        authorization in the orchestrator, not happen during proposal."""
        case = make_case(status=CaseStatus.AWAITING_REPLY)
        conversation_manager.handle_reply(case, "yes, book it")
        assert case.status == CaseStatus.AWAITING_REPLY


class TestConditionalConsentIsNeverUnconditional:
    """
    Safety rule: an affirmative expression combined with a scheduling
    constraint, negation, reschedule request, or temporal qualification
    must NOT be treated as unconditional booking consent. This must hold
    regardless of the specific wording used to express the condition - the
    detector is structural (any qualifying language + any affirmative word
    -> ambiguous), not a list of specific phrases.
    """

    def test_plain_affirmative_is_unconditional(self, conversation_manager):
        case = make_case()
        action, context = conversation_manager.handle_reply(case, "Yes, book it")
        assert context["signals"]["consent_signal"] == "affirmative"
        assert action == AgentAction.CONFIRM_BOOKING

    @pytest.mark.parametrize(
        "message",
        [
            "Yes, but not this week",
            "Yes, but not tomorrow",
            "Sure, but not Monday",
            "Okay, but not in the morning",
            "Yes, but after 5pm",
            "Sure, next week instead",
            "Yes, but a different time",
        ],
    )
    def test_conditional_affirmative_is_never_unconditional(self, conversation_manager, message):
        case = make_case()
        action, context = conversation_manager.handle_reply(case, message)
        assert context["signals"]["consent_signal"] != "affirmative", (
            f"{message!r} must not be treated as unconditional consent"
        )
        # Phase 1 preference: uncertainty resolves to ambiguous, not a guess.
        assert context["signals"]["consent_signal"] == "ambiguous"

    def test_negative_with_temporal_qualifier_is_decline_never_booking(
        self, conversation_manager
    ):
        case = make_case()
        action, context = conversation_manager.handle_reply(case, "No, not this week")
        assert action != AgentAction.CONFIRM_BOOKING
        assert context["signals"]["consent_signal"] == "negative"


class TestEmergencyAndOptOutConflictingSignals:
    """
    Regression coverage for the conflicting-signal case exercised in manual
    smoke testing: a message can legitimately carry both an emergency
    signal and an opt-out signal at once ("Stop messaging me, I'm bleeding
    badly"). Both signals must be extracted and surfaced independently;
    resolving the conflict between them is PolicyGuard's job (emergency
    outranks opt-out - see agent/policy_guard.py's rule precedence and
    tests/test_policy_guard.py::TestEmergencyOverride), not this module's.
    """

    def test_message_with_both_signals_surfaces_both(self, conversation_manager):
        case = make_case()
        action, context = conversation_manager.handle_reply(
            case, "Stop messaging me, I'm bleeding badly"
        )
        assert context["signals"]["is_emergency"] is True
        assert context["signals"]["is_opt_out"] is True
