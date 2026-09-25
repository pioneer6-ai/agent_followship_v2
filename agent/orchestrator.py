"""
Main agent orchestrator - the "brain" of the Patient Follow-up Agent.

This module implements the core agentic loop:
Perceive -> Decide -> Act -> Observe

The orchestrator coordinates all subsystems to autonomously manage
the entire patient follow-up workflow, from identification through
resolution or escalation.
"""

import os
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from core.clock import Clock, SystemClock
from core.models import FollowUpCase, CaseStatus, ContactChannel
from core.config import ClinicPolicyConfig
from core.urgency_config import UrgencyRulesConfig
from core.data_access import PatientDataStore, CalendarIntegration
from core.scheduling_calendar_adapter import SlotOption
from core.actions import AgentAction
from core.trigger_service import TriggerService
from agent.business_rules import RecallRuleEngine, UrgencyScorer
from agent.decision import (
    ActionDecision, DecisionContext, DecisionEngine, LlmDecisionEngine,
    RuleDecisionEngine,
)
from agent.delivery import (
    DeliveryBackend, NotificationOutcome, missing_recipient_outcome,
)
from agent.notifications import (
    NotificationChannel, MessageComposerAgent, build_notification_channels,
)
from agent.conversation import ConversationManager
from agent.action_handlers import AppointmentScheduler, EscalationHandler, AuditLogger
from agent.policy_guard import PolicyDecision, PolicyGuard, ProposedAction

#: Statuses that still need the agent's attention. ``MESSAGE_SENT`` is included on
#: purpose: a reminder that went out does not end the case, it starts the wait. If
#: it did terminate the case, ``reminder_count`` could never reach
#: ``max_reminders_before_escalation`` and a non-responding patient would be
#: silently abandoned.
_OPEN_STATUSES = (
    CaseStatus.PENDING,
    CaseStatus.MESSAGE_SENT,
    CaseStatus.AWAITING_REPLY,
)

#: Statuses that are finished, whatever the reason, and must never be re-contacted.
CLOSED_STATUSES = (
    CaseStatus.BOOKED,
    CaseStatus.DECLINED,
    CaseStatus.ESCALATED,
    CaseStatus.OPTED_OUT,
)


class FollowUpAgentOrchestrator:
    """
    The central controller that orchestrates the entire agent workflow.

    This class embodies the "agentic loop" pattern:
    1. PERCEIVE: Gather data about overdue patients from the data store
    2. DECIDE: Evaluate urgency, prioritize cases, determine actions
    3. ACT: Execute actions (send messages, book appointments, escalate)
    4. OBSERVE: Process patient responses and update case states

    The orchestrator demonstrates autonomous decision-making while
    maintaining transparency, auditability, and appropriate escalation
    to human staff when needed.
    """

    def __init__(
        self,
        data_store: PatientDataStore,
        calendar: CalendarIntegration,
        policy: Optional[ClinicPolicyConfig] = None,
        urgency_config: Optional[UrgencyRulesConfig] = None,
        notification_channels: Optional[Dict[ContactChannel, NotificationChannel]] = None,
        delivery_backend: Optional[DeliveryBackend] = None,
        decision_engine: Optional["DecisionEngine"] = None,
        escalation_email: Optional[str] = None,
        clock: Optional[Clock] = None,
        policy_guard: Optional[PolicyGuard] = None,
        trigger_service: Optional[TriggerService] = None,
        portal_link_provider: Optional[Callable[[str], str]] = None,
    ):
        """
        Initialize the agent orchestrator with all required subsystems.

        Args:
            data_store: Patient data access layer
            calendar: Calendar/scheduling system integration
            policy: Clinic policy configuration (uses defaults if not provided)
            notification_channels: Pre-built channels. When omitted, they are built
                from the environment (live when ``MESSAGING_DRY_RUN=0``, offline
                otherwise), so the demo and tests keep working with no credentials.
            delivery_backend: Backend to build the default channels with.
            decision_engine: Action-selection strategy. Defaults to the rule engine,
                which is also what any LLM-backed engine falls back to.
            escalation_email: Staff address to alert when a case is escalated.
                When omitted, escalations are recorded but nobody is emailed.
            clock: Source of "today"/"now". Defaults to a real, timezone-aware
                SystemClock configured with ``policy.clinic_timezone``, so the
                application never hardcodes a date; tests can inject a
                FixedClock for deterministic date-relative behavior.
            policy_guard: Deterministic safety/authorization layer. Defaults to
                a fresh PolicyGuard. This is intentionally not LLM-backed - see
                agent/policy_guard.py.
            trigger_service: Decides which cases are actionable today. Defaults
                to a TriggerService built from the same clock.
            portal_link_provider: Optional ``patient_id -> Patient Portal URL``
                callable, forwarded to MessageComposerAgent so follow-up
                reminder emails/messages can embed a patient-specific
                "Manage My Appointment" link. Reuses the exact same
                token-issuing logic as the staff-facing
                ``/api/patients/<id>/portal-link`` route (see
                web/patient_portal_auth.py's ``build_portal_link_provider``)
                - never a second token system, never an HTTP call back into
                the Flask app. Left ``None`` (the default), reminders are
                composed exactly as before this feature existed.
        """
        # Store dependencies
        self.data_store = data_store
        self.calendar = calendar
        self.policy = policy or ClinicPolicyConfig.from_env()
        self.clock = clock or SystemClock(timezone_name=self.policy.clinic_timezone)
        self.policy_guard = policy_guard or PolicyGuard()
        self.trigger_service = trigger_service or TriggerService(self.clock)

        # Initialize core business logic components
        self.rule_engine = RecallRuleEngine(self.policy)
        self.urgency_config = urgency_config or UrgencyRulesConfig.get_default()
        self.urgency_scorer = UrgencyScorer(self.urgency_config, self.policy)

        # Initialize communication components
        self.message_composer = MessageComposerAgent(
            portal_link_provider=portal_link_provider,
            booking_window_days=self.policy.booking_window_days,
        )
        self.conversation_manager = ConversationManager()

        # Initialize notification channels
        self.delivery_backend = delivery_backend
        self.notification_channels = notification_channels or build_notification_channels(
            delivery_backend
        )

        # Initialize action handlers
        self.scheduler = AppointmentScheduler(calendar)
        self.escalation_email = escalation_email or os.environ.get("AGENT_ESCALATION_EMAIL") or None
        self.escalation_handler = EscalationHandler(
            alert_email=self.escalation_email,
            notifier=self._alert_staff if self.escalation_email else None,
        )
        self.audit_logger = AuditLogger()

        # Decision strategy, and a record of what the agent could not deliver
        self.decision_engine = decision_engine or RuleDecisionEngine(self.policy)
        self.undelivered: List[Dict[str, object]] = []

        # Active cases being managed by the agent
        self.active_cases: dict[str, FollowUpCase] = {}

        # Patient-facing outbound messages are queued until a staff member
        # explicitly confirms the selected send(s) in the outreach page.
        # This is deliberately in memory because the patient store is also
        # in-memory in this demo; each queue item is still fully inspectable.
        self._pending_sends: Dict[str, Dict[str, Any]] = {}
        self._pending_sends_lock = Lock()

    def _queue_outbound_message(
        self,
        case: FollowUpCase,
        message: str,
        *,
        context: str,
        today: date,
        action: AgentAction,
    ) -> Dict[str, Any]:
        """Queue a patient message without calling any delivery backend."""
        patient = case.patient
        dedupe_key = "|".join(
            [patient.patient_id, case.episode_id, action.value, context, message]
        )

        with self._pending_sends_lock:
            for item in self._pending_sends.values():
                if item["status"] in {"pending", "sending"} and item["dedupe_key"] == dedupe_key:
                    return dict(item)

            channels = self._deliverable_order(case)
            primary_channel = channels[0] if channels else patient.preferred_channel
            channel = self.notification_channels.get(primary_channel)
            recipient = ""
            if channel is not None:
                recipient = channel._recipient_for(patient)

            send_id = uuid4().hex
            item = {
                "send_id": send_id,
                "patient_id": patient.patient_id,
                "patient_name": patient.name,
                "episode_id": case.episode_id,
                "action": action.value,
                "context": context,
                "channel": primary_channel.value,
                "recipient": recipient,
                "available_channels": [channel.value for channel in channels],
                "message": message,
                "subject": "Dental Appointment Reminder",
                "urgency": case.urgency.value,
                "days_overdue": case.days_overdue,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "scheduled_for": today.isoformat(),
                "status": "pending",
                "dedupe_key": dedupe_key,
            }
            self._pending_sends[send_id] = item

        case.add_to_log(
            f"Queued {context} for staff confirmation; message not sent"
        )
        print(
            f"   ⏸️  Queued {context} for {patient.name}; "
            f"awaiting staff confirmation ({send_id})"
        )
        return dict(item)

    def get_pending_sends(self) -> List[Dict[str, Any]]:
        """Return pending patient messages safe for the staff confirmation UI."""
        with self._pending_sends_lock:
            return [
                {key: value for key, value in item.items() if key != "dedupe_key"}
                for item in self._pending_sends.values()
                if item["status"] == "pending"
            ]

    def update_pending_send(
        self,
        send_id: str,
        message: str,
        edited_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update the body of a pending message without sending it."""
        cleaned_message = str(message or "").strip()
        if not cleaned_message:
            return {"send_id": send_id, "status": "invalid", "error": "Message cannot be empty"}
        if len(cleaned_message) > 4000:
            return {"send_id": send_id, "status": "invalid", "error": "Message cannot exceed 4000 characters"}

        with self._pending_sends_lock:
            item = self._pending_sends.get(send_id)
            if item is None:
                return {"send_id": send_id, "status": "not_found"}
            if item["status"] != "pending":
                return {"send_id": send_id, "status": item["status"]}

            item["message"] = cleaned_message
            item["edited_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if edited_by:
                item["edited_by"] = edited_by
            return {
                key: value for key, value in item.items() if key != "dedupe_key"
            }

    def cancel_pending_sends(self, send_ids: List[str]) -> List[Dict[str, Any]]:
        """Cancel selected pending messages without transmitting anything."""
        results = []
        with self._pending_sends_lock:
            for send_id in send_ids:
                item = self._pending_sends.get(send_id)
                if item is None:
                    results.append({"send_id": send_id, "status": "not_found"})
                elif item["status"] != "pending":
                    results.append({"send_id": send_id, "status": item["status"]})
                else:
                    item["status"] = "cancelled"
                    results.append({"send_id": send_id, "status": "cancelled"})
        return results

    def confirm_pending_sends(self, send_ids: List[str]) -> List[Dict[str, Any]]:
        """Deliver selected messages after atomically claiming each pending item."""
        results = []
        for send_id in send_ids:
            with self._pending_sends_lock:
                item = self._pending_sends.get(send_id)
                if item is None:
                    results.append({"send_id": send_id, "status": "not_found"})
                    continue
                if item["status"] != "pending":
                    results.append({"send_id": send_id, "status": item["status"]})
                    continue
                item["status"] = "sending"

            case = self.active_cases.get(item["patient_id"])
            if case is None:
                with self._pending_sends_lock:
                    item["status"] = "failed"
                    item["error"] = "The patient case is no longer active"
                results.append({"send_id": send_id, "status": "failed", "error": item["error"]})
                continue

            outcome = self._deliver_with_fallback(
                case,
                item["message"],
                context=item["context"],
                today=date.fromisoformat(item["scheduled_for"]),
            )
            if outcome.success:
                self._record_confirmed_delivery(case, item, outcome)
                if item["context"] == "reminder":
                    self.data_store.update_last_contacted(
                        item["patient_id"], date.fromisoformat(item["scheduled_for"])
                    )
                status = "sent"
                item["recipient"] = outcome.recipient or item["recipient"]
                item["message_id"] = outcome.message_id
                item["simulated"] = outcome.simulated
            else:
                self._escalate_undeliverable(
                    case, outcome, date.fromisoformat(item["scheduled_for"])
                )
                status = "failed"
                item["error"] = outcome.error_message or outcome.error_code

            with self._pending_sends_lock:
                item["status"] = status
            result = {"send_id": send_id, "status": status}
            if item.get("error"):
                result["error"] = item["error"]
            results.append(result)
        return results

    @staticmethod
    def _record_confirmed_delivery(
        case: FollowUpCase,
        item: Dict[str, Any],
        outcome: NotificationOutcome,
    ) -> None:
        """Apply case state changes only after a confirmed delivery succeeds."""
        context = item["context"]
        if context == "reminder":
            today = date.fromisoformat(item["scheduled_for"])
            case.status = CaseStatus.MESSAGE_SENT
            case.last_contacted = today
            case.reminder_count += 1
            case.add_to_log(f"Sent reminder via {outcome.channel.value}")
        elif context == "slot proposal":
            case.status = CaseStatus.AWAITING_REPLY
            case.add_to_log("Proposed appointment slots")
        elif context == "clarification request":
            case.add_to_log("Requested clarification on ambiguous consent")
        elif context == "reply":
            case.add_to_log(f"Agent: {item['message']}")
        elif context == "booking confirmation":
            case.add_to_log(f"Sent booking confirmation via {outcome.channel.value}")

    def get_pending_send_count(self) -> int:
        """Return the number of patient messages awaiting confirmation."""
        return len(self.get_pending_sends())

    @classmethod
    def with_llm_decisions(
        cls,
        data_store: PatientDataStore,
        calendar: CalendarIntegration,
        policy: Optional[ClinicPolicyConfig] = None,
        urgency_config: Optional[UrgencyRulesConfig] = None,
        *,
        model: Optional[str] = None,
        **kwargs: object,
    ) -> "FollowUpAgentOrchestrator":
        """
        Build an orchestrator whose DECIDE step is driven by Claude.

        The LLM only ever selects from the actions the rule engine permits, so
        this cannot send a message the rules would not have allowed. With no API
        key (or no ``anthropic`` install) it silently degrades to the rule
        engine, which is why this is safe to use as the default in the web app.

        The leading parameters mirror :meth:`__init__` exactly rather than being
        forwarded through ``*args``. That matters: a caller passing ``policy``
        positionally must not collide with a ``policy`` keyword added here, which
        is precisely the failure this signature exists to prevent.

        Args:
            data_store: Patient data access layer.
            calendar: Calendar/scheduling integration.
            policy: Clinic policy, or ``None`` for the defaults.
            model: Model id override; otherwise ``AGENT_DECISION_MODEL``.
            **kwargs: Remaining ``__init__`` arguments.

        Returns:
            A configured orchestrator.
        """
        resolved_policy = policy or ClinicPolicyConfig.from_env()
        # Use the explicit urgency_config parameter (don't discard it)
        urgency_config_arg = urgency_config
        kwargs.setdefault(
            "decision_engine",
            LlmDecisionEngine.from_environment(resolved_policy, model=model),
        )
        return cls(data_store, calendar, resolved_policy, urgency_config=urgency_config_arg, **kwargs)  # type: ignore[arg-type]

    
    def rescore_all_cases(self, new_urgency_config: UrgencyRulesConfig) -> None:
        """
        Re-score all active cases with new urgency configuration.
        
        This method is called when urgency configuration is updated through the
        dashboard. It applies the new scoring rules to all active cases without
        sending any messages.
        
        Args:
            new_urgency_config: New urgency configuration to apply
        """
        self.urgency_config = new_urgency_config
        self.urgency_scorer = UrgencyScorer(new_urgency_config, self.policy)
        
        from core.models import calculate_days_overdue
        today = date.today()
        
        rescored_count = 0
        for case in self.active_cases.values():
            days_overdue = calculate_days_overdue(case.patient, today)
            
            # Use consecutive_unanswered from existing case
            consecutive_unanswered = getattr(case, 'consecutive_unanswered_reminders', 0)
            
            # Re-score with new configuration
            result = self.urgency_scorer.score(
                case.patient,
                days_overdue,
                consecutive_unanswered=consecutive_unanswered
            )
            
            if result:
                new_urgency, new_explanation = result
                case.urgency = new_urgency
                case.urgency_explanation = new_explanation
                rescored_count += 1
        
        print(f"[INFO] Re-scored {rescored_count} active cases with new urgency configuration")


    def run_daily_cycle(self, today: Optional[date] = None) -> list[FollowUpCase]:
        """
        Execute one complete daily cycle of the agent workflow.

        This is the main entry point for the agent's autonomous operation.
        Typically called once per day (or on-demand) to process all
        overdue follow-ups.

        The cycle follows the agentic loop:
        1. PERCEIVE: Identify overdue patients
        2. DECIDE: Score urgency and prioritize
        3. ACT: Send reminders and take appropriate actions
        4. OBSERVE: (handled separately when replies come in)

        Args:
            today: Reference date (defaults to the orchestrator's clock)

        Returns:
            List of all cases processed in this cycle
        """
        if today is None:
            today = self.clock.today()

        print(f"\n{'='*70}")
        print(f"🤖 AGENT DAILY CYCLE - {today.isoformat()}")
        print(f"{'='*70}\n")

        # === TRIGGER ===
        # Which cases are actionable today at all, before any reasoning is
        # spent on them. See core/trigger_service.py: this is a deterministic
        # filter, not a decision - opted-out patients and cases not yet due
        # for their explicit next_followup_at are excluded here, up front.
        print("📊 PHASE 1: PERCEIVE - Gathering patient data...")
        all_patients = self.data_store.get_all_active_patients()
        print(f"   Found {len(all_patients)} active patients")

        # Identify overdue patients using rule engine
        overdue_cases = self.rule_engine.compute_overdue_patients(all_patients, today)
        overdue_cases = self.trigger_service.get_actionable_cases(overdue_cases)
        print(f"   Identified {len(overdue_cases)} actionable, overdue patients\n")

        # === PHASE 2: DECIDE ===
        print("🧠 PHASE 2: DECIDE - Evaluating urgency and prioritizing...")

        # Score urgency for each case
        # Restore episode history BEFORE scoring so unanswered-reminder rules use real state
        for case in overdue_cases:
            # Match existing episode by patient_id and episode_id
            expected_episode_id = f"{case.patient.patient_id}_{case.patient.last_visit_date.isoformat()}"
            previous = self.active_cases.get(case.patient.patient_id)
            
            consecutive_unanswered = 0
            if previous and previous.episode_id == expected_episode_id:
                # Same episode - preserve consecutive_unanswered count
                consecutive_unanswered = previous.consecutive_unanswered_reminders
            # else: different episode or no previous case - start fresh with 0
            
            # Score with restored state
            from core.models import calculate_days_overdue
            days_overdue = calculate_days_overdue(case.patient, today)
            
            result = self.urgency_scorer.score(
                case.patient,
                days_overdue,
                consecutive_unanswered=consecutive_unanswered
            )
            
            if result:
                case.urgency, case.urgency_explanation = result
            else:
                case.urgency = UrgencyLevel.LOW
                case.urgency_explanation = "Below minimum threshold"
            
            case.consecutive_unanswered_reminders = consecutive_unanswered
            print(f"   {case.patient.name}: {case.urgency.value.upper()} "
                  f"({case.days_overdue} days overdue) - {case.urgency_explanation}")

        # Sort by urgency (most urgent first)
        prioritized_cases = self.urgency_scorer.sort_by_urgency(overdue_cases)
        print(f"\n   Prioritized {len(prioritized_cases)} cases by urgency\n")

        # === PHASE 3: ACT ===
        print("⚡ PHASE 3: ACT - Taking actions on prioritized cases...")

        processed_cases = []
        for case in prioritized_cases:
            # A fresh case object is built from the data store on every cycle, so
            # it knows the new ``days_overdue`` but nothing about what the agent
            # has already tried. Carry that progress forward, otherwise the
            # reminder counter resets to zero every day and the escalation cap
            # can never be reached.
            previous = self.active_cases.get(case.patient.patient_id)
            if previous is not None:
                self._carry_forward(previous, case)
            self.active_cases[case.patient.patient_id] = case

            # Decide, then authorize before any side effect executes.
            decision = self._decide_for_case(case, today)
            decision = self._authorize_decision(case, decision)
            self._execute_action(case, decision, today)

            processed_cases.append(case)

        undelivered = len(self.undelivered)
        print(f"\n✅ Daily cycle complete. Processed {len(processed_cases)} cases.")
        if undelivered:
            affected = len({record["patient_id"] for record in self.undelivered})
            print(f"⚠️  {undelivered} send attempt(s) failed across {affected} case(s); "
                  f"affected cases were escalated to staff. Run with a live backend "
                  f"or check the audit log for details.")
        print()
        print(f"{'='*70}\n")

        return processed_cases

    def _available_channels_for(self, case: FollowUpCase) -> List[ContactChannel]:
        """
        Channels this patient can actually be reached on.

        The preferred channel comes first so it stays the primary choice, followed
        by every other channel the patient has a contact detail for. ``PHONE_CALL``
        is included even though the agent cannot place calls: it is a real
        preference worth reflecting, and its failure is informative.

        Args:
            case: Case whose patient is being contacted.

        Returns:
            Ordered channel list, possibly empty.
        """
        patient = case.patient
        ordered: List[ContactChannel] = []

        if patient.contact_info.get(patient.preferred_channel):
            ordered.append(patient.preferred_channel)

        for channel in ContactChannel:
            if channel in ordered:
                continue
            if patient.contact_info.get(channel):
                ordered.append(channel)

        return ordered

    def _exhausted_channels_for(self, case: FollowUpCase) -> List[ContactChannel]:
        """Channels already tried for this case and still failing."""
        patient_id = case.patient.patient_id
        seen: List[ContactChannel] = []
        for record in self.undelivered:
            if record.get("patient_id") != patient_id:
                continue
            channel = record.get("channel")
            if isinstance(channel, ContactChannel) and channel not in seen:
                seen.append(channel)
        return seen

    @staticmethod
    def _carry_forward(previous: FollowUpCase, current: FollowUpCase) -> None:
        """
        Transfer a case's progress from a previous cycle onto a fresh perception.

        The freshly-perceived case has newer facts (days overdue, urgency); the
        previously-tracked one has the agent's history (how many reminders were
        sent, what the patient said). The agent needs both, so the history is
        copied over the fresh object rather than being thrown away.

        Args:
            previous: The case as tracked at the end of an earlier cycle.
            current: The case just rebuilt from the data store; mutated in place.
        """
        current.status = previous.status
        current.reminder_count = previous.reminder_count
        if previous.last_contacted is not None:
            current.last_contacted = previous.last_contacted
        if previous.conversation_log:
            current.conversation_log = list(previous.conversation_log)
        # Preserve episode tracking fields if same episode
        if hasattr(previous, "episode_id") and previous.episode_id == current.episode_id:
            current.consecutive_unanswered_reminders = previous.consecutive_unanswered_reminders
        # else: different episode - keep the fresh consecutive_unanswered_reminders (should be 0)

        # Preserve-unless-recomputed: a case rebuilt from the data store has
        # no way to know about a previously scheduled next_followup_at (it
        # is not derived from PatientRecord), so without this it is
        # silently lost on every cycle - see TriggerService's invariant
        # that a scheduled future follow-up must survive repeated daily
        # cycles until it is reached, explicitly cleared, or replaced by a
        # newly computed value. `current` never sets next_followup_at
        # itself today, so `current.next_followup_at is None` is always
        # true in practice, but the check is kept explicit so a future
        # caller that DOES compute a fresh value on `current` is never
        # silently overridden by a stale `previous` one.
        #
        # Exception: a case that has reached a terminal outreach status
        # (BOOKED/DECLINED/ESCALATED/OPTED_OUT) must not carry a stale
        # future trigger forward - TriggerService already excludes
        # terminal statuses from actionability regardless of
        # next_followup_at, so this is currently inert either way, but a
        # terminal case's scheduling data should not linger as if it were
        # still meaningful.
        if previous.status in CLOSED_STATUSES:
            current.next_followup_at = None
        elif previous.next_followup_at is not None and current.next_followup_at is None:
            current.next_followup_at = previous.next_followup_at

    def _alert_staff(self, escalation: dict) -> None:
        """
        Email a human about an escalated case.

        Routed through the same backend as patient messages, so it obeys the same
        dry-run/live gate: a real email only when live sends are explicitly on,
        otherwise it is printed. Failures propagate to the caller, which records
        them on the case rather than aborting the cycle.

        Args:
            escalation: The record produced by
                :meth:`~agent.action_handlers.EscalationHandler.escalate`.
        """
        channel = self.notification_channels.get(ContactChannel.EMAIL)
        if channel is None:
            raise RuntimeError("no email channel is configured to alert staff")
        if not self.escalation_email:
            # Unreachable via the constructor, which only wires this method as the
            # notifier when an address exists. Raised anyway so the invariant is
            # local and a future caller cannot silently drop an escalation.
            raise RuntimeError("no escalation email is configured to alert staff")

        subject = f"[{escalation['priority'].upper()}] Escalation: {escalation['patient_name']}"
        body = "\n".join(
            [
                "A patient follow-up case needs human attention.",
                "",
                f"Patient:     {escalation['patient_name']} ({escalation['patient_id']})",
                f"Treatment:   {escalation['treatment_type']}",
                f"Days overdue:{escalation['days_overdue']}",
                f"Urgency:     {escalation['urgency']}",
                f"Priority:    {escalation['priority']}",
                f"Reason:      {escalation['reason']}",
                f"Escalated:   {escalation['escalated_at']}",
                "",
                "Conversation log:",
                *(f"  - {entry}" for entry in escalation["conversation_log"]),
                "",
                "Please contact this patient directly.",
            ]
        )

        outcome = channel.backend.deliver(
            ContactChannel.EMAIL,
            self.escalation_email,
            body,
            subject=subject,
        )
        if not outcome.success:
            raise RuntimeError(
                f"{outcome.error_code}: {outcome.error_message} (via {outcome.channel.value})"
            )

    def _decide_for_case(self, case: FollowUpCase, today: date) -> ActionDecision:
        """
        Decide what action to take for a specific case.

        Builds the decision context from the case, the clinic policy and what the
        agent already knows about this patient's reachability, then delegates to
        the configured decision engine. The rule engine's permissible-action set is
        what the engine is bound by, so a model-backed engine cannot act outside
        clinic policy.

        Args:
            case: Follow-up case to process
            today: Current date

        Returns:
            The chosen action with its rationale and provenance.
        """
        context = DecisionContext(
            case=case,
            today=today,
            policy=self.policy,
            escalation_needed=self.conversation_manager.should_escalate(case),
            available_channels=self._available_channels_for(case),
            exhausted_channels=self._exhausted_channels_for(case),
        )
        return self.decision_engine.decide(context)

    def _authorize_decision(
        self,
        case: FollowUpCase,
        decision: ActionDecision,
        *,
        source_message: Optional[str] = None,
        is_emergency: bool = False,
        is_opt_out: bool = False,
        consent_signal: Optional[str] = None,
        is_clinical_question: bool = False,
    ) -> ActionDecision:
        """
        Run a decided action through PolicyGuard before it may be executed.

        This is the SAFETY AUTHORIZATION step of the reasoning loop (Trigger
        -> Context -> Reason/Decide -> Safety Authorization -> Tool Execution
        -> Observe Result -> State Update). It is deterministic and runs
        regardless of whether ``decision`` came from the rule engine or the
        LLM engine - an LLM-backed decision is not trusted any more than a
        rule-based one.

        Args:
            case: Case the decision applies to.
            decision: The proposed decision (from a DecisionEngine or from
                ConversationManager.handle_reply, wrapped by the caller).
            source_message: Raw inbound message, if this proposal came from
                a patient reply (kept for audit purposes only).
            is_emergency: Emergency signal extracted by the caller.
            is_opt_out: Opt-out signal extracted by the caller.
            consent_signal: Consent signal extracted by the caller.
            is_clinical_question: Clinical-question signal extracted by the caller.

        Returns:
            The ActionDecision to actually execute. On ALLOW this is the
            original decision, unmodified. On DENY/FORCE_ESCALATION/
            REQUIRE_CLARIFICATION, the action is overridden accordingly and
            the rationale records the PolicyGuard verdict for the audit trail.
        """
        proposal = ProposedAction(
            action=decision.action,
            case=case,
            source_message=source_message,
            is_emergency=is_emergency,
            is_opt_out=is_opt_out,
            consent_signal=consent_signal,
            is_clinical_question=is_clinical_question,
        )
        verdict = self.policy_guard.evaluate(proposal)

        if verdict.decision == PolicyDecision.ALLOW:
            # No override: the decision engine's own log entry in
            # _execute_action remains the canonical audit record for this
            # case, so a reviewer always finds "decision_source" on the
            # first agent_decision entry regardless of whether PolicyGuard
            # ran. A separate, redundant ALLOW entry would only add noise.
            return decision

        # PolicyGuard changed the outcome: record why, as its own audit
        # entry, distinct from (and in addition to) the decision engine's
        # entry that _execute_action still logs for the overridden action.
        self.audit_logger.log_decision(
            case,
            decision.action,
            f"PolicyGuard: {verdict.decision.value} ({verdict.rule}) - {verdict.reason}",
            additional_context={
                "policy_rule": verdict.rule,
                "policy_decision": verdict.decision.value,
                "proposed_action": decision.action.value,
            },
        )

        if verdict.decision == PolicyDecision.FORCE_ESCALATION:
            return ActionDecision(
                action=AgentAction.ESCALATE_TO_STAFF,
                rationale=f"[policy_guard:{verdict.rule}] {verdict.reason}",
                source="policy_guard",
                alternatives=[decision.action],
            )

        if verdict.decision == PolicyDecision.REQUIRE_CLARIFICATION:
            return ActionDecision(
                action=AgentAction.REQUEST_CLARIFICATION,
                rationale=f"[policy_guard:{verdict.rule}] {verdict.reason}",
                source="policy_guard",
                alternatives=[decision.action],
            )

        # DENY: fall back to no-op rather than executing the proposed action.
        return ActionDecision(
            action=AgentAction.DO_NOTHING,
            rationale=f"[policy_guard:{verdict.rule}] {verdict.reason}",
            source="policy_guard",
            alternatives=[decision.action],
        )

    def _decide_action_for_case(self, case: FollowUpCase, today: date) -> AgentAction:
        """
        Decide the next action for a case, returning just the action.

        Retained for callers that only want the action; the rationale and the
        provenance of the decision are available via :meth:`_decide_for_case`.

        Args:
            case: Follow-up case to process
            today: Current date

        Returns:
            AgentAction to execute.
        """
        return self._decide_for_case(case, today).action

    def _deliver_with_fallback(
        self,
        case: FollowUpCase,
        message: str,
        context: str = "reminder",
        today: Optional[date] = None,
    ) -> NotificationOutcome:
        """
        Try to deliver a message, moving on to another channel when one fails.

        This is the agent's answer to "the recipient was not verified". A failed
        send is not an exception and not a reason to give up: it is information.
        The channel's own ``suggested_fallbacks`` are honoured first (they come from
        the provider's error taxonomy), then any other channel the patient is
        reachable on, so a patient without WhatsApp still gets the SMS.

        Every attempt is written to the audit log and the conversation log, so the
        trail shows what was tried and why each attempt failed.

        Args:
            case: Case being contacted.
            message: Body to send.
            context: What is being sent, used in log entries.
            today: Date for log entries.

        Returns:
            The successful outcome, or the last failure when nothing got through.
        """
        patient = case.patient
        order = self._deliverable_order(case)
        if not order:
            return missing_recipient_outcome(patient.preferred_channel)

        last_failure: Optional[NotificationOutcome] = None

        # Every channel the patient is reachable on gets exactly one attempt. A
        # provider's ``suggested_fallbacks`` is advisory (it describes the error,
        # not this patient), so it must not be used to give up early: an SMS
        # rejection says nothing about whether the email address works.
        for channel_type in order:
            channel = self.notification_channels.get(channel_type)
            if channel is None:
                continue

            outcome = channel.send(patient, message)
            self.audit_logger.log_communication(
                case, outcome.channel.value, message, "outbound", outcome.success
            )

            if outcome.success:
                case.add_to_log(
                    f"{context} delivered via {outcome.channel.value}"
                    f"{' (simulated)' if outcome.simulated else ''}"
                )
                return outcome

            case.add_to_log(
                f"{context} failed via {outcome.channel.value} "
                f"[{outcome.error_code}]: {outcome.error_message}"
            )
            self._record_undelivered(case, outcome, context)
            last_failure = outcome

        return last_failure or missing_recipient_outcome(patient.preferred_channel)

    def _deliverable_order(self, case: FollowUpCase) -> List[ContactChannel]:
        """
        Channels to attempt, in order, for this case.

        Preferred channel first, then any other channel the patient is reachable
        on that has not already failed for this case.
        """
        exhausted = set(self._exhausted_channels_for(case))
        order = [
            c for c in self._available_channels_for(case) if c not in exhausted
        ]
        return order

    def _record_undelivered(
        self, case: FollowUpCase, outcome: NotificationOutcome, context: str
    ) -> None:
        """Remember a failed attempt so later cycles know what has been tried."""
        self.undelivered.append(
            {
                "patient_id": case.patient.patient_id,
                "channel": outcome.channel,
                "error_code": outcome.error_code,
                "context": context,
                "message_id": outcome.message_id,
            }
        )

    def _escalate_undeliverable(
        self,
        case: FollowUpCase,
        outcome: NotificationOutcome,
        today: date,
    ) -> None:
        """
        Escalate a case the agent could not reach, rather than reporting success.

        Args:
            case: Case that could not be contacted.
            outcome: The final, unsuccessful delivery outcome.
            today: Date of the attempt.
        """
        tried = ", ".join(c.value for c in self._exhausted_channels_for(case)) or "none"
        reason = (
            f"Undeliverable: no channel reached {case.patient.name}. "
            f"Channels tried: {tried}. Last error: "
            f"{outcome.error_code} ({outcome.error_message}). "
            f"{outcome.hint or ''}"
        ).strip()

        self.escalation_handler.escalate(case, reason, "high")
        case.status = CaseStatus.ESCALATED
        case.add_to_log(f"Escalated: undeliverable ({tried})")
        self.audit_logger.log_decision(
            case,
            AgentAction.ESCALATE_TO_STAFF,
            f"Escalated after delivery failure on {outcome.channel.value}: "
            f"{outcome.error_code}",
        )

        print(f"   ⚠️  Could not reach {case.patient.name} on any channel "
              f"({tried}); escalated to staff")
        if outcome.hint:
            print(f"       Reason: {outcome.hint}")

    def _execute_action(
        self, case: FollowUpCase, decision: ActionDecision, today: date
    ) -> None:
        """
        Execute a decided action for a case.

        This is where the agent's decisions become concrete actions
        in the real world (sending messages, booking appointments, etc.).

        Args:
            case: Follow-up case
            decision: The decision to act on
            today: Current date
        """
        patient = case.patient
        action = decision.action

        # Log the decision, including which engine produced it
        rationale = (
            f"Action: {action.value} for {patient.name} "
            f"(Urgency: {case.urgency.value}, Status: {case.status.value}, "
            f"Decided by: {decision.source})"
        )
        # Record which engine chose, so a reviewer can always tell a model's
        # judgement from the rules' default.
        self.audit_logger.log_decision(
            case,
            action,
            rationale,
            additional_context={
                "decision_source": decision.source,
                "alternatives_considered": [a.value for a in decision.alternatives],
            },
        )
        if decision.from_llm:
            print(f"   🧠 {patient.name}: model chose {action.value} "
                  f"({decision.rationale})")
        if action == AgentAction.SEND_REMINDER:
            # Compose personalized message
            message_type = "urgent" if case.urgency.value == "critical" else "initial"
            message = self.message_composer.compose(case, message_type)

            self._queue_outbound_message(
                case,
                message,
                context="reminder",
                today=today,
                action=action,
            )
        elif action == AgentAction.PROPOSE_SLOT:
            # Find available appointment slots, bounded by the booking window
            booking_deadline = today + timedelta(days=self.policy.booking_window_days)
            available_slots = self.scheduler.find_available_slots(
                case, after=today, limit=3, to_date=booking_deadline
            )

            if available_slots:
                # Compose message with slot options
                message = self.message_composer.compose_slot_proposal(
                    case, available_slots
                )

                self._queue_outbound_message(
                    case,
                    message,
                    context="slot proposal",
                    today=today,
                    action=action,
                )

        elif action == AgentAction.CONFIRM_BOOKING:
            # Book the appointment, bounded by the booking window
            booking_deadline = today + timedelta(days=self.policy.booking_window_days)
            success, booked_date = self.scheduler.try_book(
                case, after=today, to_date=booking_deadline
            )

            # Log appointment action
            self.audit_logger.log_appointment_action(
                case, "booking", booked_date, success
            )

            if success:
                case.add_to_log(f"Appointment booked for {booked_date.isoformat()}")
                print(f"   ✅ Booked appointment for {patient.name} on {booked_date}")

                # Send confirmation message; a booking that the patient never hears
                # about is a no-show waiting to happen, so a total delivery failure
                # is escalated rather than swallowed.
                confirmation_msg = (
                    "Your appointment is confirmed for "
                    f"{booked_date.strftime('%A, %B %d')}. See you then!"
                )
                self._queue_outbound_message(
                    case,
                    confirmation_msg,
                    context="booking confirmation",
                    today=today,
                    action=action,
                )

        elif action == AgentAction.ESCALATE_TO_STAFF:
            # Determine escalation priority based on urgency
            priority_map = {
                "critical": "critical",
                "high": "high",
                "medium": "normal",
                "low": "low"
            }
            priority = priority_map.get(case.urgency.value, "normal")

            # Escalate to human staff
            reason = f"Case requires human attention: {case.reminder_count} reminders sent, " \
                    f"{case.days_overdue} days overdue, urgency: {case.urgency.value}"

            self.escalation_handler.escalate(case, reason, priority)
            print(f"   ⚠️  Escalated {patient.name} to staff (Priority: {priority})")

        elif action == AgentAction.MARK_DECLINED:
            case.status = CaseStatus.DECLINED
            case.add_to_log("Patient declined follow-up")
            print(f"   ℹ️  Marked {patient.name} as declined")

        elif action == AgentAction.RECORD_OPT_OUT:
            # Authorized by PolicyGuard's opt_out_record rule. This is the
            # STATE UPDATE step: no message is sent, the patient is simply
            # never contacted again.
            patient.opted_out = True
            case.status = CaseStatus.OPTED_OUT
            case.add_to_log("Recorded patient opt-out; no further contact will be made")
            print(f"   🚫 Recorded opt-out for {patient.name}")

        elif action == AgentAction.REQUEST_CLARIFICATION:
            # Consent was ambiguous; ask instead of guessing. Status is left
            # as-is (still open) so the next reply is re-evaluated normally.
            message = (
                f"Hi {patient.name}, just to confirm - would you like us to "
                f"book the appointment, or would you prefer a different time?"
            )
            self._queue_outbound_message(
                case,
                message,
                context="clarification request",
                today=today,
                action=action,
            )

        elif action == AgentAction.DO_NOTHING:
            # No action needed at this time
            pass

    def handle_incoming_reply(
        self, patient_id: str, message: str, received_date: Optional[date] = None
    ) -> None:
        """
        Process an incoming reply from a patient.

        This completes the agentic loop by OBSERVING patient responses
        and deciding next actions based on their reply.

        This method demonstrates the agent's ability to handle dynamic,
        multi-turn conversations and make contextual decisions.

        Args:
            patient_id: ID of patient who sent the message
            message: Patient's message text
            received_date: Date message was received (defaults to today)
        """
        if received_date is None:
            received_date = self.clock.today()

        # Retrieve the case for this patient
        case = self.active_cases.get(patient_id)
        if not case:
            print(f"⚠️  Received message from unknown patient: {patient_id}")
            return

        print(f"\n📬 Incoming message from {case.patient.name}")
        print(f"   Message: \"{message}\"")

        # Status is intentionally NOT mutated here. Setting it to
        # AWAITING_REPLY before the proposed action is authorized would
        # silently downgrade a terminal status (OPTED_OUT, BOOKED, DECLINED,
        # ESCALATED) on every subsequent inbound message, regardless of
        # what PolicyGuard ultimately decides. State changes happen only
        # after authorization, below, and only for actions whose semantics
        # call for a status change.
        status_on_entry = case.status

        # REASON/DECIDE: use conversation manager to understand intent and
        # propose an action. This step only proposes - it does not mutate
        # case state and does not execute anything (see agent/conversation.py).
        action, context = self.conversation_manager.handle_reply(case, message)
        signals = context.get("signals", {})

        # Log the communication
        self.audit_logger.log_communication(
            case, case.patient.preferred_channel.value, message,
            "inbound", True
        )

        print(f"   🧠 Recognized intent, proposed action: {action.value}")

        # SAFETY AUTHORIZATION: PolicyGuard evaluates the proposed action
        # against the extracted signals before anything below can execute.
        # This is the required message -> signal extraction -> proposed
        # action -> PolicyGuard -> authorized action -> execution ordering.
        proposal = ProposedAction(
            action=action,
            case=case,
            source_message=message,
            is_emergency=signals.get("is_emergency", False),
            is_opt_out=signals.get("is_opt_out", False),
            consent_signal=signals.get("consent_signal"),
            is_clinical_question=signals.get("is_clinical_question", False),
        )
        verdict = self.policy_guard.evaluate(proposal)
        if verdict.decision != PolicyDecision.ALLOW:
            self.audit_logger.log_decision(
                case,
                action,
                f"PolicyGuard: {verdict.decision.value} ({verdict.rule}) - {verdict.reason}",
                additional_context={
                    "policy_rule": verdict.rule,
                    "policy_decision": verdict.decision.value,
                    "proposed_action": action.value,
                },
            )
        if verdict.decision == PolicyDecision.FORCE_ESCALATION:
            action = AgentAction.ESCALATE_TO_STAFF
        elif verdict.decision == PolicyDecision.REQUIRE_CLARIFICATION:
            action = AgentAction.REQUEST_CLARIFICATION
        elif verdict.decision == PolicyDecision.DENY:
            action = AgentAction.DO_NOTHING
        # else ALLOW: proceed with the proposed action unchanged.

        if action != AgentAction.ESCALATE_TO_STAFF or verdict.decision == PolicyDecision.ALLOW:
            print(f"   ✅ Authorized action: {action.value} ({verdict.rule})")
        else:
            print(f"   🛑 PolicyGuard overrode to {action.value} ({verdict.rule}): {verdict.reason}")

        # SAFETY INVARIANT: a denied/no-op action must have zero observable
        # effect - no outbound message, no tool call, no status change. This
        # is enforced explicitly here rather than relying on
        # generate_response returning an empty string (defense-in-depth is
        # applied there too, but the orchestrator must not depend on it):
        # a denied action's case status is left exactly as it was on entry
        # (status_on_entry), which keeps an opted-out case OPTED_OUT rather
        # than letting it drift to AWAITING_REPLY.
        if action == AgentAction.DO_NOTHING:
            case.status = status_on_entry
            print(f"   🚫 No action taken; no message sent (status unchanged: "
                  f"{case.status.value})")
            print()
            return

        # Generate response message
        response = self.conversation_manager.generate_response(action, case, context)

        # TOOL EXECUTION / STATE UPDATE: only the authorized action runs.
        if action == AgentAction.CONFIRM_BOOKING:
            # Extract preferred slot if provided
            preferred_slot = context.get('selected_slot')
            booking_deadline = received_date + timedelta(days=self.policy.booking_window_days)
            booked = False
            if preferred_slot:
                # Get available slots, bounded by the booking window
                slots = self.scheduler.find_available_slots(
                    case, after=received_date, limit=5, to_date=booking_deadline
                )
                if preferred_slot <= len(slots):
                    selected_date = slots[preferred_slot - 1]
                    success, booked_date = self.scheduler.try_book(case, selected_date)

                    if success:
                        case.status = CaseStatus.BOOKED
                        booked = True
                        response = f"Perfect! Your appointment is confirmed for " \
                                  f"{booked_date.strftime('%A, %B %d')}. See you then!"
                        self.audit_logger.log_appointment_action(
                            case, "booking", booked_date, True
                        )
            else:
                # Try to book next available, bounded by the booking window
                success, booked_date = self.scheduler.try_book(
                    case, after=received_date, to_date=booking_deadline
                )
                if success and booked_date:
                    case.status = CaseStatus.BOOKED
                    booked = True
                    response = f"Great! We've booked you for " \
                              f"{booked_date.strftime('%A, %B %d')}."
            if not booked:
                # No slot could be booked (out of range selection, or the
                # calendar had nothing available in the booking window).
                # The case was already AWAITING_REPLY when this message
                # arrived (that is what justified proposing a booking in
                # the first place), so it stays there rather than being
                # forced into any other status.
                case.status = CaseStatus.AWAITING_REPLY

        elif action == AgentAction.REQUEST_CLARIFICATION:
            # Consent was ambiguous; still open, waiting on the patient's
            # answer to the clarifying question below.
            case.status = CaseStatus.AWAITING_REPLY

        elif action == AgentAction.PROPOSE_SLOT:
            # Find new slots, bounded by the booking window
            booking_deadline = received_date + timedelta(days=self.policy.booking_window_days)
            available_slots = self.scheduler.find_available_slots(
                case, after=received_date, limit=3, to_date=booking_deadline
            )
            if available_slots:
                case.status = CaseStatus.AWAITING_REPLY
                response = self.message_composer.compose_slot_proposal(
                    case, available_slots
                )

        elif action == AgentAction.ESCALATE_TO_STAFF:
            case.status = CaseStatus.ESCALATED
            self.escalation_handler.escalate(
                case,
                reason=f"Patient question or complex request: {message}",
                priority="normal"
            )

        elif action == AgentAction.MARK_DECLINED:
            case.status = CaseStatus.DECLINED

        elif action == AgentAction.RECORD_OPT_OUT:
            case.patient.opted_out = True
            case.status = CaseStatus.OPTED_OUT
            case.add_to_log("Recorded patient opt-out; no further contact will be made")

        # Queue the response; no patient-facing delivery occurs until staff
        # confirms this item from the outreach page.
        if response:
            self._queue_outbound_message(
                case,
                response,
                context="reply",
                today=received_date,
                action=action,
            )

        print()

    def handle_portal_slot_selection(
        self,
        patient_id: str,
        selected_date: date,
        today: Optional[date] = None,
    ) -> "ActionDecision":
        """
        Book an appointment chosen explicitly through the patient portal's
        slot picker (a button click, never free text, never routed through
        an LLM or ConversationManager).

        This follows the same reasoning loop as every other side-effecting
        action: propose CONFIRM_BOOKING with affirmative consent (a slot
        picker click IS unambiguous, explicit consent to that exact date -
        there is no natural-language ambiguity to resolve) -> PolicyGuard
        authorizes -> only then does the existing booking service run.
        PolicyGuard's opt-out/emergency rules still apply, so an opted-out
        patient's portal session cannot book even via this path.

        The caller (the portal route) is responsible for independently
        re-validating that `selected_date` exists, is available, is not in
        the past, and is within the configured booking window BEFORE
        calling this method - this method re-validates existence/
        availability against the calendar again itself (never trusts that
        the caller already did), but does not re-derive "is in the past" or
        "within the booking window" since those require the caller's
        request context (today's date is passed in explicitly for this
        reason, rather than defaulting silently).

        Args:
            patient_id: Patient whose case this applies to.
            selected_date: The exact date the patient selected. Must be a
                date the calendar actually still has available; this method
                re-checks that independently of whatever the browser sent.
            today: Reference date (defaults to the orchestrator's clock).

        Returns:
            The ActionDecision that was actually authorized/executed, so
            the caller can report ALLOW/DENY/FORCE_ESCALATION back to the
            portal without duplicating PolicyGuard's logic.
        """
        if today is None:
            today = self.clock.today()

        case = self.active_cases.get(patient_id)
        if case is None:
            raise ValueError(f"unknown patient_id: {patient_id!r}")

        proposed = ActionDecision(
            action=AgentAction.CONFIRM_BOOKING,
            rationale="Patient portal: explicit slot selection",
            source="patient_portal",
        )
        decision = self._authorize_decision(
            case,
            proposed,
            consent_signal="affirmative",
        )

        if decision.action != AgentAction.CONFIRM_BOOKING:
            # PolicyGuard overrode the booking (e.g. opt-out, emergency).
            # No calendar call is made at all in that case.
            if decision.action == AgentAction.ESCALATE_TO_STAFF:
                case.status = CaseStatus.ESCALATED
                self.escalation_handler.escalate(
                    case,
                    reason="Patient portal booking attempt was overridden by PolicyGuard",
                    priority="normal",
                )
            case.add_to_log(
                f"Patient portal: booking attempt for {selected_date.isoformat()} "
                f"was not authorized ({decision.rationale})"
            )
            return decision

        # Re-validate against the calendar independently of the browser's
        # claim: the slot must still actually be available. The scheduling
        # subsystem (via SchedulingDatabaseCalendarAdapter, when that is the
        # configured calendar) is the SOURCE OF TRUTH for whether a booking
        # actually exists - case.status is only ever set to BOOKED after
        # observing a successful, confirmed result from the calendar layer
        # itself, never assumed from this call merely being reached.
        #
        # When the configured calendar supports the richer
        # book_appointment_detailed() contract (the production adapter), use
        # it so the case reflects the database's own recorded appointment id
        # and status. Calendars that only implement the plain
        # CalendarIntegration.book_appointment() boolean (e.g.
        # MockCalendarIntegration, used in tests) fall back to try_book,
        # exactly as before.
        detailed_booker = getattr(self.calendar, "book_appointment_detailed", None)
        if detailed_booker is not None:
            outcome = detailed_booker(
                case.patient.patient_id,
                selected_date,
                case.patient.treatment_type,
                patient_name=case.patient.name,
                follow_up_case_id=case.patient.patient_id,
                follow_up_reason=f"Patient portal booking for {case.patient.name}",
            )
            success = outcome.success
            booked_date = selected_date if success else None
            appointment_id = outcome.appointment_id
            appointment_status = outcome.status
            failure_reason = outcome.error
        else:
            # Mock-calendar fallback path (e.g. tests). try_book also sets
            # case.status itself on success; the assignment below is
            # redundant but harmless, and keeps this method's own status
            # transition the single thing this docstring needs to describe.
            success, booked_date = self.scheduler.try_book(case, preferred_date=selected_date)
            appointment_id = None
            appointment_status = "confirmed" if success else None
            failure_reason = None if success else "Slot unavailable at booking time."

        self.audit_logger.log_appointment_action(
            case, "patient_portal_booking", booked_date if success else selected_date, success,
            details=(f"appointment_id={appointment_id}" if appointment_id is not None else None),
        )

        if success:
            # Case state reflects the observed successful booking; it is
            # never the source of truth for whether the appointment exists.
            case.status = CaseStatus.BOOKED
            case.next_followup_at = None
            case.add_to_log(
                f"Patient portal: booked appointment for {booked_date.isoformat()}"
                + (f" (appointment #{appointment_id}, {appointment_status})"
                   if appointment_id is not None else "")
            )
            print(f"   ✅ Patient portal booked {case.patient.name} for {booked_date}")
        else:
            case.add_to_log(
                f"Patient portal: slot {selected_date.isoformat()} was no longer "
                f"available at booking time"
                + (f" ({failure_reason})" if failure_reason else "")
            )
            print(f"   ⚠️  Patient portal booking failed for {case.patient.name}: "
                  f"{selected_date} unavailable")

        return decision

    def handle_portal_slot_option_selection(
        self,
        patient_id: str,
        slot_option: SlotOption,
        today: Optional[date] = None,
    ) -> "ActionDecision":
        """
        Book the EXACT (date, session, time) slot chosen through the
        patient portal's slot picker, when the configured calendar
        supports that level of detail (the production
        SchedulingDatabaseCalendarAdapter).

        This mirrors `handle_portal_slot_selection`'s reasoning loop
        (propose CONFIRM_BOOKING with affirmative consent -> PolicyGuard
        authorizes -> only then does the booking service run) exactly,
        differing only in which calendar method performs the actual
        booking: `book_specific_slot` (books precisely the requested
        session/time) instead of `book_appointment_detailed` (which
        auto-picks the earliest slot on a date). See that method's
        docstring for the full authorization/audit rationale, which
        applies unchanged here.

        The caller (the portal route) is responsible for independently
        re-validating the slot's date/session/time against a fresh
        availability re-fetch BEFORE calling this method; this method
        does not re-derive "is in the past" or "within the booking
        window" for the same reason described on
        `handle_portal_slot_selection`.

        Args:
            patient_id: Patient whose case this applies to.
            slot_option: The exact SlotOption (date, session, time) the
                patient selected, already independently re-validated by
                the caller against the calendar's current availability.
            today: Reference date (defaults to the orchestrator's clock).

        Returns:
            The ActionDecision that was actually authorized/executed.
        """
        if today is None:
            today = self.clock.today()

        case = self.active_cases.get(patient_id)
        if case is None:
            raise ValueError(f"unknown patient_id: {patient_id!r}")

        selected_date = slot_option.slot_date

        proposed = ActionDecision(
            action=AgentAction.CONFIRM_BOOKING,
            rationale="Patient portal: explicit slot selection (specific time)",
            source="patient_portal",
        )
        decision = self._authorize_decision(
            case,
            proposed,
            consent_signal="affirmative",
        )

        if decision.action != AgentAction.CONFIRM_BOOKING:
            if decision.action == AgentAction.ESCALATE_TO_STAFF:
                case.status = CaseStatus.ESCALATED
                self.escalation_handler.escalate(
                    case,
                    reason="Patient portal booking attempt was overridden by PolicyGuard",
                    priority="normal",
                )
            case.add_to_log(
                f"Patient portal: booking attempt for {selected_date.isoformat()} "
                f"{slot_option.session} {slot_option.time} was not authorized "
                f"({decision.rationale})"
            )
            return decision

        specific_booker = getattr(self.calendar, "book_specific_slot", None)
        if specific_booker is None:
            # Calendar does not support specific-slot booking at all (e.g.
            # MockCalendarIntegration in tests) - the portal route should
            # not have reached this method in that case, but fail safely
            # rather than booking something the patient did not select.
            case.add_to_log(
                "Patient portal: specific-slot booking attempted but the "
                "configured calendar does not support it"
            )
            return decision

        outcome = specific_booker(
            case.patient.patient_id,
            selected_date,
            slot_option.session,
            slot_option.time,
            case.patient.treatment_type,
            patient_name=case.patient.name,
            follow_up_case_id=case.patient.patient_id,
            follow_up_reason=f"Patient portal booking for {case.patient.name}",
        )
        success = outcome.success
        booked_date = selected_date if success else None
        appointment_id = outcome.appointment_id
        appointment_status = outcome.status
        failure_reason = outcome.error

        self.audit_logger.log_appointment_action(
            case, "patient_portal_booking", booked_date if success else selected_date, success,
            details=(f"appointment_id={appointment_id}" if appointment_id is not None else None),
        )

        if success:
            case.status = CaseStatus.BOOKED
            case.next_followup_at = None
            case.add_to_log(
                f"Patient portal: booked appointment for {booked_date.isoformat()} "
                f"{slot_option.session} {slot_option.time}"
                + (f" (appointment #{appointment_id}, {appointment_status})"
                   if appointment_id is not None else "")
            )
            print(f"   ✅ Patient portal booked {case.patient.name} for "
                  f"{booked_date} {slot_option.time}")
        else:
            case.add_to_log(
                f"Patient portal: slot {selected_date.isoformat()} "
                f"{slot_option.session} {slot_option.time} was no longer "
                f"available at booking time"
                + (f" ({failure_reason})" if failure_reason else "")
            )
            print(f"   ⚠️  Patient portal booking failed for {case.patient.name}: "
                  f"{selected_date} {slot_option.time} unavailable")

        return decision

    def handle_no_suitable_slot(
        self,
        patient_id: str,
        today: Optional[date] = None,
    ) -> "ActionDecision":
        """
        Handle the patient portal's "None of these times work - remind me
        next week" button.

        This is a button click, not free text, so it never goes through
        ConversationManager or an LLM either. It proposes
        MARK_PENDING_FUTURE_AVAILABILITY, which PolicyGuard authorizes like
        any other action (an opted-out or emergency-flagged patient's
        portal session is still blocked from even parking a future
        follow-up outside the normal safety path). On success, this only
        updates case state (status + next_followup_at) - it never sends any
        message and never touches the email/notification subsystem; a
        separate outreach mechanism is responsible for acting on
        `next_followup_at` once TriggerService admits the case again.

        Args:
            patient_id: Patient whose case this applies to.
            today: Reference date (defaults to the orchestrator's clock).

        Returns:
            The authorized ActionDecision.
        """
        if today is None:
            today = self.clock.today()

        case = self.active_cases.get(patient_id)
        if case is None:
            raise ValueError(f"unknown patient_id: {patient_id!r}")

        proposed = ActionDecision(
            action=AgentAction.MARK_PENDING_FUTURE_AVAILABILITY,
            rationale="Patient portal: no offered slot worked for the patient",
            source="patient_portal",
        )
        decision = self._authorize_decision(case, proposed)

        if decision.action != AgentAction.MARK_PENDING_FUTURE_AVAILABILITY:
            if decision.action == AgentAction.ESCALATE_TO_STAFF:
                case.status = CaseStatus.ESCALATED
                self.escalation_handler.escalate(
                    case,
                    reason="Patient portal 'remind me later' was overridden by PolicyGuard",
                    priority="normal",
                )
            case.add_to_log(
                f"Patient portal: 'remind me later' was not authorized "
                f"({decision.rationale})"
            )
            return decision

        case.status = CaseStatus.PENDING_FUTURE_AVAILABILITY
        case.next_followup_at = today + timedelta(days=self.policy.followup_retry_days)
        case.add_to_log(
            f"Patient portal: no suitable slot; next_followup_at set to "
            f"{case.next_followup_at.isoformat()}"
        )
        print(f"   📅 {case.patient.name}: no suitable slot, will re-check on "
              f"{case.next_followup_at}")

        return decision

    def get_active_cases(self) -> list[FollowUpCase]:
        """
        Get all currently active follow-up cases.

        Returns:
            List of active cases
        """
        return list(self.active_cases.values())

    def get_case_by_patient_id(self, patient_id: str) -> Optional[FollowUpCase]:
        """
        Retrieve a specific case by patient ID.

        Args:
            patient_id: Patient identifier

        Returns:
            FollowUpCase if found, None otherwise
        """
        return self.active_cases.get(patient_id)

    def get_statistics(self) -> dict:
        """
        Generate operational statistics for the agent.

        Returns:
            Dictionary containing various metrics
        """
        cases = list(self.active_cases.values())

        status_counts = {}
        for status in CaseStatus:
            status_counts[status.value] = sum(
                1 for case in cases if case.status == status
            )

        urgency_counts = {}
        for case in cases:
            urgency_counts[case.urgency.value] = \
                urgency_counts.get(case.urgency.value, 0) + 1

        audit_summary = self.audit_logger.generate_summary_report()

        return {
            "total_active_cases": len(cases),
            "cases_by_status": status_counts,
            "cases_by_urgency": urgency_counts,
            "escalated_cases": len(self.escalation_handler.get_escalated_cases()),
            "pending_sends": self.get_pending_send_count(),
            "audit_summary": audit_summary,
        }
