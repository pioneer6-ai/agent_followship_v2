"""
Unit tests for agent.policy_guard.PolicyGuard.

These tests exercise PolicyGuard in isolation, feeding it pre-built
ProposedAction objects rather than going through ConversationManager or the
decision engines. This confirms PolicyGuard's decisions are correct as a
pure function of (action, signals, case state), independent of who proposed
the action.
"""

from core.actions import AgentAction
from core.models import CaseStatus
from agent.policy_guard import PolicyDecision, ProposedAction

from tests.conftest import make_case, make_patient


class TestActionAllowlist:
    def test_do_nothing_is_allowed(self, policy_guard):
        case = make_case()
        proposal = ProposedAction(action=AgentAction.DO_NOTHING, case=case)
        verdict = policy_guard.evaluate(proposal)
        assert verdict.decision == PolicyDecision.ALLOW
        assert verdict.rule == "noop_allow"

    def test_every_agentaction_value_passes_the_allowlist(self, policy_guard):
        """The allow-list is derived from AgentAction itself, so every
        member must pass rule 1 (though later rules may still gate it)."""
        case = make_case()
        for action in AgentAction:
            proposal = ProposedAction(action=action, case=case)
            verdict = policy_guard.evaluate(proposal)
            assert verdict.rule != "action_allowlist"


class TestEmergencyOverride:
    def test_emergency_forces_escalation_even_if_action_was_something_else(self, policy_guard):
        case = make_case()
        proposal = ProposedAction(
            action=AgentAction.SEND_REMINDER,
            case=case,
            is_emergency=True,
        )
        verdict = policy_guard.evaluate(proposal)
        assert verdict.decision == PolicyDecision.FORCE_ESCALATION
        assert verdict.rule == "emergency_override"

    def test_emergency_outranks_opt_out(self, policy_guard):
        """An emergency must be escalated even if the patient also opted out."""
        patient = make_patient(opted_out=True)
        case = make_case(patient=patient)
        proposal = ProposedAction(
            action=AgentAction.ESCALATE_TO_STAFF,
            case=case,
            is_emergency=True,
            is_opt_out=True,
        )
        verdict = policy_guard.evaluate(proposal)
        assert verdict.decision == PolicyDecision.FORCE_ESCALATION
        assert verdict.rule == "emergency_override"


class TestOptOut:
    def test_recording_opt_out_is_allowed(self, policy_guard):
        case = make_case()
        proposal = ProposedAction(
            action=AgentAction.RECORD_OPT_OUT,
            case=case,
            is_opt_out=True,
        )
        verdict = policy_guard.evaluate(proposal)
        assert verdict.decision == PolicyDecision.ALLOW
        assert verdict.rule == "opt_out_record"

    def test_other_actions_denied_when_opt_out_signaled(self, policy_guard):
        case = make_case()
        proposal = ProposedAction(
            action=AgentAction.SEND_REMINDER,
            case=case,
            is_opt_out=True,
        )
        verdict = policy_guard.evaluate(proposal)
        assert verdict.decision == PolicyDecision.DENY
        assert verdict.rule == "opt_out_enforced"

    def test_previously_opted_out_patient_blocks_new_outreach(self, policy_guard):
        """Even without a fresh opt-out signal, a patient.opted_out=True case
        must block further outbound/booking actions."""
        patient = make_patient(opted_out=True)
        case = make_case(patient=patient)
        proposal = ProposedAction(action=AgentAction.SEND_REMINDER, case=case)
        verdict = policy_guard.evaluate(proposal)
        assert verdict.decision == PolicyDecision.DENY
        assert verdict.rule == "opt_out_enforced"

    def test_do_nothing_always_allowed_even_when_opted_out(self, policy_guard):
        patient = make_patient(opted_out=True)
        case = make_case(patient=patient)
        proposal = ProposedAction(action=AgentAction.DO_NOTHING, case=case)
        verdict = policy_guard.evaluate(proposal)
        assert verdict.decision == PolicyDecision.ALLOW
        assert verdict.rule == "noop_allow"


class TestClinicalQuestionBoundary:
    def test_clinical_question_forces_escalation(self, policy_guard):
        case = make_case()
        proposal = ProposedAction(
            action=AgentAction.ESCALATE_TO_STAFF,
            case=case,
            is_clinical_question=True,
        )
        verdict = policy_guard.evaluate(proposal)
        assert verdict.decision == PolicyDecision.FORCE_ESCALATION
        assert verdict.rule == "clinical_question_boundary"


class TestBookingConsent:
    def test_affirmative_consent_allows_booking(self, policy_guard):
        case = make_case()
        proposal = ProposedAction(
            action=AgentAction.CONFIRM_BOOKING,
            case=case,
            consent_signal="affirmative",
        )
        verdict = policy_guard.evaluate(proposal)
        assert verdict.decision == PolicyDecision.ALLOW
        assert verdict.rule == "booking_consent_clear"

    def test_ambiguous_consent_requires_clarification(self, policy_guard):
        case = make_case()
        proposal = ProposedAction(
            action=AgentAction.CONFIRM_BOOKING,
            case=case,
            consent_signal="ambiguous",
        )
        verdict = policy_guard.evaluate(proposal)
        assert verdict.decision == PolicyDecision.REQUIRE_CLARIFICATION
        assert verdict.rule == "booking_consent_ambiguous"

    def test_missing_consent_denies_booking(self, policy_guard):
        case = make_case()
        proposal = ProposedAction(
            action=AgentAction.CONFIRM_BOOKING,
            case=case,
            consent_signal=None,
        )
        verdict = policy_guard.evaluate(proposal)
        assert verdict.decision == PolicyDecision.DENY
        assert verdict.rule == "booking_consent_missing"

    def test_negative_consent_denies_booking(self, policy_guard):
        case = make_case()
        proposal = ProposedAction(
            action=AgentAction.CONFIRM_BOOKING,
            case=case,
            consent_signal="negative",
        )
        verdict = policy_guard.evaluate(proposal)
        assert verdict.decision == PolicyDecision.DENY


class TestDefaultAllow:
    def test_ordinary_action_with_no_special_signals_is_allowed(self, policy_guard):
        case = make_case()
        proposal = ProposedAction(action=AgentAction.MARK_DECLINED, case=case)
        verdict = policy_guard.evaluate(proposal)
        assert verdict.decision == PolicyDecision.ALLOW
        assert verdict.rule == "default_allow"

    def test_evaluate_does_not_mutate_case(self, policy_guard):
        """PolicyGuard must be a pure decision function - it must never
        change case state itself."""
        case = make_case(status=CaseStatus.MESSAGE_SENT)
        proposal = ProposedAction(
            action=AgentAction.CONFIRM_BOOKING, case=case, consent_signal="affirmative"
        )
        policy_guard.evaluate(proposal)
        assert case.status == CaseStatus.MESSAGE_SENT
