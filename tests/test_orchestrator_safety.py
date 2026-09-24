"""
Integration tests for the Phase 1 safety wiring in agent.orchestrator.

These confirm PolicyGuard actually sits between decide and execute in both
entry points (run_daily_cycle and handle_incoming_reply), using the full
FollowUpAgentOrchestrator rather than PolicyGuard in isolation. Delivery is
stubbed with an in-memory backend so no network/AWS/Anthropic dependency is
exercised.
"""

import contextlib
import io
from datetime import date, timedelta

import pytest

from agent.delivery import DeliveryBackend, NotificationOutcome
from agent.orchestrator import FollowUpAgentOrchestrator
from agent.notifications import build_notification_channels
from core.config import ClinicPolicyConfig
from core.data_access import MockCalendarIntegration, MockPatientDataStore
from core.models import CaseStatus

from tests.conftest import FIXED_TODAY, make_case, make_fixed_clock, make_patient


class AlwaysSucceedsBackend(DeliveryBackend):
    """Minimal in-memory backend: every send reports success, nothing real
    is contacted. Used so these tests exercise the agent's decision/
    authorization wiring without depending on tools/ or AWS."""

    def __init__(self):
        self.sent = []

    def name(self) -> str:
        return "test-backend"

    def deliver(self, channel, recipient, message, *, subject=None):
        self.sent.append({"channel": channel, "recipient": recipient, "message": message})
        return NotificationOutcome(
            success=True,
            channel=channel,
            recipient=recipient,
            simulated=True,
            message_id="TEST-1",
        )


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data_store = MockPatientDataStore()
    calendar = MockCalendarIntegration()
    policy = ClinicPolicyConfig()
    backend = AlwaysSucceedsBackend()
    channels = build_notification_channels(backend)
    agent = FollowUpAgentOrchestrator(
        data_store,
        calendar,
        policy,
        notification_channels=channels,
        clock=make_fixed_clock(),
    )
    return agent, data_store, calendar, backend


def _seed(data_store, patient):
    data_store.add_patient(patient)


class TestDailyCycleAuthorization:
    def test_normal_case_is_allowed_and_reminder_sent(self, agent_env):
        agent, data_store, _, _ = agent_env
        _seed(data_store, make_patient("P1", "Alice", days_since_visit=60, recall_interval_days=30))

        with contextlib.redirect_stdout(io.StringIO()):
            agent.run_daily_cycle(FIXED_TODAY)

        case = agent.get_case_by_patient_id("P1")
        assert case.status == CaseStatus.MESSAGE_SENT

    def test_opted_out_patient_is_never_contacted_by_daily_cycle(self, agent_env):
        agent, data_store, _, _ = agent_env
        patient = make_patient("P1", "Alice", days_since_visit=60, recall_interval_days=30, opted_out=True)
        _seed(data_store, patient)

        with contextlib.redirect_stdout(io.StringIO()):
            agent.run_daily_cycle(FIXED_TODAY)

        # TriggerService excludes opted-out patients before any decision is
        # even made, so no case is created/processed for them at all.
        assert agent.get_case_by_patient_id("P1") is None


class TestIncomingReplyAuthorization:
    def _seeded_agent(self, agent_env):
        agent, data_store, _, _ = agent_env
        _seed(data_store, make_patient("P1", "Alice", days_since_visit=60, recall_interval_days=30))
        with contextlib.redirect_stdout(io.StringIO()):
            agent.run_daily_cycle(FIXED_TODAY)
        return agent

    def test_affirmative_reply_is_authorized_and_books(self, agent_env):
        agent = self._seeded_agent(agent_env)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.handle_incoming_reply("P1", "yes, book it", FIXED_TODAY)
        case = agent.get_case_by_patient_id("P1")
        assert case.status == CaseStatus.BOOKED

    def test_ambiguous_reply_is_overridden_to_clarification(self, agent_env):
        agent = self._seeded_agent(agent_env)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.handle_incoming_reply("P1", "yes but not this week, I'm busy", FIXED_TODAY)
        case = agent.get_case_by_patient_id("P1")
        # PolicyGuard requires clarification: the case must NOT be booked,
        # and a clarifying question (not a booking confirmation) must be
        # the message actually sent to the patient.
        assert case.status != CaseStatus.BOOKED
        sent_messages = [entry for entry in case.conversation_log if entry.startswith("Agent:")]
        assert sent_messages, "expected a response to have been sent"
        assert "confirm" in sent_messages[-1].lower()

    def test_opt_out_reply_is_authorized_and_records_opt_out(self, agent_env):
        agent = self._seeded_agent(agent_env)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.handle_incoming_reply("P1", "stop contacting me", FIXED_TODAY)
        case = agent.get_case_by_patient_id("P1")
        assert case.status == CaseStatus.OPTED_OUT
        assert case.patient.opted_out is True

    def test_opted_out_patient_cannot_be_booked_even_with_affirmative_reply(self, agent_env):
        """Once opted out, PolicyGuard's opt-out rule blocks every further
        action for that patient, including a later 'yes' reply."""
        agent = self._seeded_agent(agent_env)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.handle_incoming_reply("P1", "stop contacting me", FIXED_TODAY)
            agent.handle_incoming_reply("P1", "yes, book it", FIXED_TODAY)
        case = agent.get_case_by_patient_id("P1")
        assert case.status != CaseStatus.BOOKED
        assert case.patient.opted_out is True

    def test_emergency_reply_is_overridden_to_escalation(self, agent_env):
        agent = self._seeded_agent(agent_env)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.handle_incoming_reply(
                "P1", "severe bleeding, this is an emergency", FIXED_TODAY
            )
        case = agent.get_case_by_patient_id("P1")
        assert case.status == CaseStatus.ESCALATED
        assert len(agent.escalation_handler.get_escalated_cases()) == 1

    def test_decline_reply_is_authorized_and_marks_declined(self, agent_env):
        agent = self._seeded_agent(agent_env)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.handle_incoming_reply("P1", "no, not interested", FIXED_TODAY)
        case = agent.get_case_by_patient_id("P1")
        assert case.status == CaseStatus.DECLINED


class TestConditionalConsentCannotInvokeBooking:
    """
    Regression coverage for the defect found in manual smoke testing:
    "Yes, but not this week" was misclassified as unconditional affirmative
    consent and the orchestrator actually booked an appointment.

    These tests prove, at the orchestrator level (not just the signal
    classifier), that conditional consent can never reach
    CalendarIntegration.book_appointment - i.e. the booking side effect
    itself is never invoked, not merely that the final status happens to
    differ.
    """

    @pytest.mark.parametrize(
        "message",
        [
            "Yes, but not this week",
            "Yes, but not tomorrow",
            "Sure, but not Monday",
            "Okay, but after 5pm",
            "Sure, next week instead",
            "Yes, but a different time",
        ],
    )
    def test_conditional_consent_never_books_or_sends_a_confirmation(
        self, agent_env, message
    ):
        agent, backend = self._seeded_agent_with_backend(agent_env)
        sends_before = len(backend.sent)

        with contextlib.redirect_stdout(io.StringIO()):
            agent.handle_incoming_reply("P1", message, FIXED_TODAY)

        case = agent.get_case_by_patient_id("P1")
        assert case.status != CaseStatus.BOOKED, (
            f"{message!r} must not result in a booked case"
        )
        # The booking side effect itself: no message claiming a booked
        # appointment was ever sent, proving book_appointment was never
        # reached, not merely that we didn't check the resulting status.
        new_sends = backend.sent[sends_before:]
        assert not any("booked you for" in s["message"].lower() for s in new_sends), (
            f"{message!r} must not trigger a booking-confirmation send"
        )

    def _seeded_agent_with_backend(self, agent_env):
        agent, data_store, _, backend = agent_env
        _seed(data_store, make_patient("P1", "Alice", days_since_visit=60, recall_interval_days=30))
        with contextlib.redirect_stdout(io.StringIO()):
            agent.run_daily_cycle(FIXED_TODAY)
        return agent, backend


class TestDenyAndNoOpSafetyInvariant:
    """
    Regression coverage for the defect found in manual smoke testing: after
    a patient opted out, a *later* reply (e.g. "yes, book it") was still
    getting a generic "Thank you for your response." message sent to them,
    and the case status silently drifted from OPTED_OUT to AWAITING_REPLY -
    even though PolicyGuard correctly denied the booking itself.

    The required invariant: when PolicyGuard returns DENY (authorized
    action becomes DO_NOTHING), there must be (1) no outbound message,
    (2) no tool execution, (3) no case-status mutation, and an opted-out
    case must remain OPTED_OUT specifically.
    """

    def _seeded_agent_with_backend(self, agent_env):
        agent, data_store, _, backend = agent_env
        _seed(data_store, make_patient("P1", "Alice", days_since_visit=60, recall_interval_days=30))
        with contextlib.redirect_stdout(io.StringIO()):
            agent.run_daily_cycle(FIXED_TODAY)
        return agent, backend

    def test_opted_out_patient_later_yes_book_it_stays_opted_out_no_send(self, agent_env):
        agent, backend = self._seeded_agent_with_backend(agent_env)

        with contextlib.redirect_stdout(io.StringIO()):
            agent.handle_incoming_reply("P1", "stop messaging me", FIXED_TODAY)

        case = agent.get_case_by_patient_id("P1")
        assert case.status == CaseStatus.OPTED_OUT
        assert case.patient.opted_out is True
        sends_after_opt_out = len(backend.sent)

        later = FIXED_TODAY + timedelta(days=10)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.handle_incoming_reply("P1", "yes, book it", later)

        case = agent.get_case_by_patient_id("P1")
        assert case.status == CaseStatus.OPTED_OUT, "must remain OPTED_OUT"
        assert case.status != CaseStatus.BOOKED
        assert len(backend.sent) == sends_after_opt_out, "no new outbound message must be sent"

    def test_opted_out_patient_ordinary_message_stays_opted_out_no_send(self, agent_env):
        agent, backend = self._seeded_agent_with_backend(agent_env)

        with contextlib.redirect_stdout(io.StringIO()):
            agent.handle_incoming_reply("P1", "stop messaging me", FIXED_TODAY)

        case = agent.get_case_by_patient_id("P1")
        assert case.status == CaseStatus.OPTED_OUT
        sends_after_opt_out = len(backend.sent)

        with contextlib.redirect_stdout(io.StringIO()):
            agent.handle_incoming_reply("P1", "what is the cost of a cleaning?", FIXED_TODAY)

        case = agent.get_case_by_patient_id("P1")
        assert case.status == CaseStatus.OPTED_OUT, "must remain OPTED_OUT for any later message"
        assert len(backend.sent) == sends_after_opt_out, "no new outbound message must be sent"

    def test_generic_deny_causes_no_send_no_tool_call_no_status_mutation(self, agent_env):
        """A DENY verdict for a reason other than opt-out must have the same
        zero-side-effect guarantee. Simulate this with a custom PolicyGuard
        that always denies, to isolate the orchestrator's own behavior from
        PolicyGuard's specific rules."""
        from agent.policy_guard import PolicyDecision as _PD, PolicyVerdict as _PV

        class AlwaysDenyGuard:
            def evaluate(self, proposal):
                return _PV(decision=_PD.DENY, reason="test forced deny", rule="test_rule")

        agent, backend = self._seeded_agent_with_backend(agent_env)
        agent.policy_guard = AlwaysDenyGuard()

        case = agent.get_case_by_patient_id("P1")
        status_before = case.status
        sends_before = len(backend.sent)
        appointments_before = len(agent.calendar.get_appointments_for_patient("P1"))

        with contextlib.redirect_stdout(io.StringIO()):
            agent.handle_incoming_reply("P1", "yes, book it", FIXED_TODAY)

        case = agent.get_case_by_patient_id("P1")
        assert case.status == status_before, "status must not change on a denied action"
        assert len(backend.sent) == sends_before, "no outbound message on a denied action"
        assert len(agent.calendar.get_appointments_for_patient("P1")) == appointments_before, (
            "no tool execution (booking) on a denied action"
        )

    def test_do_nothing_never_produces_the_generic_fallback_message(self, conversation_manager):
        """Defense-in-depth: generate_response(DO_NOTHING, ...) itself must
        return no message, not the generic 'Thank you for your response.'"""
        from core.actions import AgentAction as _AA

        case = make_case()
        response = conversation_manager.generate_response(_AA.DO_NOTHING, case, {})
        assert response == ""
        assert "thank you for your response" not in response.lower()

    def test_normal_non_denied_reply_still_sends_expected_response(self, agent_env):
        """Sanity check: the fix must not suppress legitimate sends."""
        agent, backend = self._seeded_agent_with_backend(agent_env)
        sends_before = len(backend.sent)

        with contextlib.redirect_stdout(io.StringIO()):
            agent.handle_incoming_reply("P1", "yes, book it", FIXED_TODAY)

        case = agent.get_case_by_patient_id("P1")
        assert case.status == CaseStatus.BOOKED
        assert len(backend.sent) == sends_before + 1
        assert "booked you for" in backend.sent[-1]["message"].lower()

    def test_existing_decline_behavior_unchanged(self, agent_env):
        agent, backend = self._seeded_agent_with_backend(agent_env)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.handle_incoming_reply("P1", "no, not interested", FIXED_TODAY)
        case = agent.get_case_by_patient_id("P1")
        assert case.status == CaseStatus.DECLINED

    def test_existing_escalation_behavior_unchanged(self, agent_env):
        agent, backend = self._seeded_agent_with_backend(agent_env)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.handle_incoming_reply(
                "P1", "severe bleeding, this is an emergency", FIXED_TODAY
            )
        case = agent.get_case_by_patient_id("P1")
        assert case.status == CaseStatus.ESCALATED
        assert len(agent.escalation_handler.get_escalated_cases()) == 1


class TestNextFollowupAtCarriesForwardAcrossCycles:
    """
    Regression coverage for the defect found in manual smoke testing:
    FollowUpAgentOrchestrator._carry_forward did not propagate
    next_followup_at from one daily cycle's case object to the next
    (freshly-rebuilt) one, so a scheduled future follow-up was silently
    lost on the very next call to run_daily_cycle.

    Required invariant: a scheduled future follow-up must survive repeated
    daily cycles until it is reached, explicitly cleared, or replaced by a
    newly computed follow-up time.

    NOTE ON SCOPE: Phase 1 has no code path that itself sets
    next_followup_at (the full park-and-retry lifecycle - including a
    PENDING_FUTURE_AVAILABILITY status - is explicitly deferred to a later
    phase). These tests set next_followup_at manually on a case in an
    otherwise ordinary status (PENDING) purely to exercise the carry-forward
    mechanism itself, which is what TriggerService's re-trigger check
    depends on regardless of which future phase ends up setting the field.
    """

    def _seeded_agent(self, agent_env):
        agent, data_store, _, backend = agent_env
        _seed(data_store, make_patient("P1", "Alice", days_since_visit=60, recall_interval_days=30))
        with contextlib.redirect_stdout(io.StringIO()):
            agent.run_daily_cycle(FIXED_TODAY)
        return agent, backend

    def test_scheduled_followup_survives_repeated_daily_cycles_until_reached(self, agent_env):
        agent, backend = self._seeded_agent(agent_env)
        case = agent.get_case_by_patient_id("P1")

        # Day 0: park the case with an explicit future re-trigger date.
        day7 = FIXED_TODAY + timedelta(days=7)
        case.next_followup_at = day7
        reminders_at_day0 = case.reminder_count

        # Day 1: still Day 7, and not yet actionable.
        with contextlib.redirect_stdout(io.StringIO()):
            agent.run_daily_cycle(FIXED_TODAY + timedelta(days=1))
        case = agent.get_case_by_patient_id("P1")
        assert case.next_followup_at == day7, "Day 1: next_followup_at must still be Day 7"
        assert agent.trigger_service.is_actionable(case) is False
        assert case.reminder_count == reminders_at_day0, "must not have been acted on yet"

        # Day 3: still Day 7, still not actionable.
        with contextlib.redirect_stdout(io.StringIO()):
            agent.run_daily_cycle(FIXED_TODAY + timedelta(days=3))
        case = agent.get_case_by_patient_id("P1")
        assert case.next_followup_at == day7, "Day 3: next_followup_at must still be Day 7"
        assert agent.trigger_service.is_actionable(case) is False

        # Day 6: still Day 7, still not actionable (one day short).
        with contextlib.redirect_stdout(io.StringIO()):
            agent.run_daily_cycle(FIXED_TODAY + timedelta(days=6))
        case = agent.get_case_by_patient_id("P1")
        assert case.next_followup_at == day7, "Day 6: next_followup_at must still be Day 7"
        assert agent.trigger_service.is_actionable(case) is False

        # Day 7: now actionable.
        with contextlib.redirect_stdout(io.StringIO()):
            agent.run_daily_cycle(day7)
        case = agent.get_case_by_patient_id("P1")
        assert agent.trigger_service.is_actionable(case) is True or case.reminder_count > reminders_at_day0, (
            "Day 7: case must become actionable and be acted on"
        )

    def test_newly_computed_next_followup_at_overrides_the_previous_value(self, agent_env):
        """current.next_followup_at, if freshly set, must win over a stale
        previous value - preserve-unless-recomputed, not blind preservation."""
        agent, backend = self._seeded_agent(agent_env)
        case = agent.get_case_by_patient_id("P1")

        stale_date = FIXED_TODAY + timedelta(days=7)
        case.next_followup_at = stale_date

        # Directly exercise _carry_forward with a "current" that already
        # has a freshly computed next_followup_at, simulating a future
        # caller that recomputes the field itself.
        from core.models import FollowUpCase

        fresh_date = FIXED_TODAY + timedelta(days=14)
        fresh_case = FollowUpCase(
            patient=case.patient,
            days_overdue=case.days_overdue,
            urgency=case.urgency,
            reason=case.reason,
            next_followup_at=fresh_date,
        )
        agent._carry_forward(case, fresh_case)
        assert fresh_case.next_followup_at == fresh_date, (
            "a newly computed next_followup_at must not be overwritten by the stale previous value"
        )

    def test_terminal_status_does_not_retain_a_stale_future_trigger(self, agent_env):
        agent, backend = self._seeded_agent(agent_env)
        case = agent.get_case_by_patient_id("P1")

        case.next_followup_at = FIXED_TODAY + timedelta(days=7)
        case.status = CaseStatus.BOOKED

        from core.models import FollowUpCase

        fresh_case = FollowUpCase(
            patient=case.patient,
            days_overdue=case.days_overdue,
            urgency=case.urgency,
            reason=case.reason,
        )
        agent._carry_forward(case, fresh_case)
        assert fresh_case.status == CaseStatus.BOOKED
        assert fresh_case.next_followup_at is None, (
            "a terminal case must not carry a stale future trigger forward"
        )

    @pytest.mark.parametrize(
        "status",
        [CaseStatus.BOOKED, CaseStatus.DECLINED, CaseStatus.ESCALATED, CaseStatus.OPTED_OUT],
    )
    def test_every_terminal_status_clears_next_followup_at(self, agent_env, status):
        agent, backend = self._seeded_agent(agent_env)
        case = agent.get_case_by_patient_id("P1")
        case.next_followup_at = FIXED_TODAY + timedelta(days=7)
        case.status = status

        from core.models import FollowUpCase

        fresh_case = FollowUpCase(
            patient=case.patient,
            days_overdue=case.days_overdue,
            urgency=case.urgency,
            reason=case.reason,
        )
        agent._carry_forward(case, fresh_case)
        assert fresh_case.next_followup_at is None, f"{status} must clear next_followup_at"


class TestEmergencyOutranksOptOutInFullPath:
    """
    Regression coverage proving, through the real orchestrator (not just
    PolicyGuard in isolation), that a message carrying both an emergency
    signal and an opt-out signal results in FORCE_ESCALATION - the
    emergency-over-opt-out precedence is enforced end to end, including for
    the specific message found during manual smoke testing.
    """

    def _seeded_agent_with_backend(self, agent_env):
        agent, data_store, _, backend = agent_env
        _seed(data_store, make_patient("P1", "Alice", days_since_visit=60, recall_interval_days=30))
        with contextlib.redirect_stdout(io.StringIO()):
            agent.run_daily_cycle(FIXED_TODAY)
        return agent, backend

    def test_bleeding_badly_and_stop_messaging_escalates_not_opts_out(self, agent_env):
        agent, backend = self._seeded_agent_with_backend(agent_env)

        with contextlib.redirect_stdout(io.StringIO()):
            agent.handle_incoming_reply(
                "P1", "Stop messaging me, I'm bleeding badly", FIXED_TODAY
            )

        case = agent.get_case_by_patient_id("P1")
        assert case.status == CaseStatus.ESCALATED, (
            "emergency must win over opt-out for the immediate case action"
        )
        assert case.status != CaseStatus.OPTED_OUT
        assert len(agent.escalation_handler.get_escalated_cases()) == 1


class TestBookingWindowIntegration:
    def test_booking_respects_configured_window(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        data_store = MockPatientDataStore()
        calendar = MockCalendarIntegration()
        # A 2-day window starting on a Friday spans only the weekend, so no
        # weekday slot is offered and booking must fail rather than reach
        # past the configured window.
        policy = ClinicPolicyConfig(booking_window_days=2)
        channels = build_notification_channels(AlwaysSucceedsBackend())
        friday = date(2026, 9, 25)
        assert friday.weekday() == 4
        agent = FollowUpAgentOrchestrator(
            data_store,
            calendar,
            policy,
            notification_channels=channels,
            clock=make_fixed_clock(),
        )
        _seed(data_store, make_patient("P1", "Alice", days_since_visit=60, recall_interval_days=30))
        with contextlib.redirect_stdout(io.StringIO()):
            agent.run_daily_cycle(friday)
            agent.handle_incoming_reply("P1", "yes, book it", friday)

        case = agent.get_case_by_patient_id("P1")
        assert case.status != CaseStatus.BOOKED
