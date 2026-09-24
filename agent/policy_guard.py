"""
PolicyGuard - the centralized, deterministic safety and authorization layer.

This module is the single authority that decides whether a *proposed* action
may execute. It is intentionally simple, rule-based, and has NO dependency on
any LLM. That is a deliberate safety property: the checks implemented here
(emergency handling, opt-out, booking consent, clinical-question boundaries,
action allow-listing) must remain deterministic and auditable regardless of
which decision engine (rule-based or LLM-backed, see agent/decision.py)
proposed the action.

Position in the reasoning loop:

    Trigger -> Context -> Reason/Decide -> Safety Authorization -> Tool Execution -> Observe Result -> State Update
                                                  ^^^^^^^^^^^^^^^^
                                                   PolicyGuard sits here

Concretely, in agent/orchestrator.py:
  - run_daily_cycle: decision_engine.decide() produces an ActionDecision,
    which PolicyGuard authorizes before _execute_action() runs.
  - handle_incoming_reply: conversation_manager.handle_reply() produces a
    proposed AgentAction plus extracted signals, which PolicyGuard
    authorizes before any booking/messaging/escalation side effect runs.

PolicyGuard never performs language understanding itself (no regex over raw
message text, no NLU). It only reasons over already-extracted, structured
signals (e.g. `is_emergency`, `is_opt_out`, `consent_signal`) and case state.
Whoever proposes the action (the rule engine, the LLM engine, or the
ConversationManager's intent recognition) is responsible for understanding
the message; PolicyGuard is responsible for deciding what's safe to do
about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.actions import AgentAction
from core.models import FollowUpCase


class PolicyDecision(Enum):
    """
    The set of verdicts PolicyGuard can return for a proposed action.

    ALLOW: The proposed action may execute as-is.
    DENY: The proposed action must not execute. No substitute action is
        implied; the caller should treat this as a hard stop for that
        specific proposal (e.g. an action naming something that isn't
        allow-listed, or outreach blocked by an opt-out).
    FORCE_ESCALATION: The proposed action is overridden. Regardless of what
        was proposed, the case must be escalated to human staff and no
        autonomous conversational or booking action should be taken.
    REQUIRE_CLARIFICATION: The proposed action is not safe to execute yet
        because the input that justified it was ambiguous or insufficient
        (e.g. contradictory reply, no clear consent). The agent should ask
        a clarifying question instead of proceeding.
    """
    ALLOW = "allow"
    DENY = "deny"
    FORCE_ESCALATION = "force_escalation"
    REQUIRE_CLARIFICATION = "require_clarification"


@dataclass
class ProposedAction:
    """
    A structured request from an agent ("what I want to do and why").

    This is the contract between whatever proposed the action (the rule
    engine, the LLM engine, or ConversationManager's intent recognition) and
    PolicyGuard. The caller is responsible for populating the `signals` it
    extracted from the patient's message or from the current case context;
    PolicyGuard does not re-derive these signals from raw text, it only
    trusts what's handed to it here.

    Attributes:
        action: The AgentAction the caller wants to execute.
        case: The FollowUpCase this action applies to.
        source_message: The raw inbound patient message that triggered this
            proposal, if any (kept for audit purposes only - PolicyGuard
            must not parse it).
        is_emergency: True if the proposing caller detected a potential
            medical emergency signal in the patient's message. See
            agent/conversation.py's demo safety-signal note: this is a
            configurable demo rule, not a clinical triage judgment, and
            PolicyGuard treats it as an opaque boolean regardless of how
            it was produced.
        is_opt_out: True if the proposing caller detected an opt-out /
            "stop contacting me" request.
        consent_signal: One of "affirmative", "negative", "ambiguous", or
            None (no consent-relevant signal). Used to gate booking actions.
        is_clinical_question: True if the proposing caller detected a
            question that requires clinical/financial/administrative
            expertise the agent does not have.
        context: Free-form extra context, kept for audit purposes only.
    """
    action: AgentAction
    case: FollowUpCase
    source_message: Optional[str] = None
    is_emergency: bool = False
    is_opt_out: bool = False
    consent_signal: Optional[str] = None  # "affirmative" | "negative" | "ambiguous" | None
    is_clinical_question: bool = False
    context: dict = field(default_factory=dict)

    def is_action(self, agent_action: AgentAction) -> bool:
        """True if this proposal's action is the given AgentAction."""
        return self.action == agent_action


@dataclass
class PolicyVerdict:
    """
    The result of PolicyGuard.evaluate().

    Attributes:
        decision: The PolicyDecision reached.
        reason: Human-readable, audit-ready explanation for the decision.
        rule: The identifier of the specific rule that produced this verdict
            (useful for audit logs and for tests asserting on behavior).
    """
    decision: PolicyDecision
    reason: str
    rule: str


class PolicyGuard:
    """
    The single authoritative safety/authorization layer.

    PolicyGuard.evaluate() is a pure function of (ProposedAction) -> Verdict.
    It does not mutate the case, does not send messages, and does not call
    any tool. It only decides whether the caller may proceed. All decision
    rules here are deterministic and independent of any LLM.

    Rule precedence (checked in this order, first match wins):
        1. Action allow-list    -> DENY if the action isn't a known AgentAction
        2. No-op                 -> ALLOW (nothing to authorize)
        3. Emergency              -> FORCE_ESCALATION
        4. Opt-out                -> ALLOW only RECORD_OPT_OUT, DENY everything else
        5. Clinical question      -> FORCE_ESCALATION
        6. Booking consent        -> ALLOW / REQUIRE_CLARIFICATION / DENY
        7. Default                -> ALLOW
    """

    #: The only actions the agent is currently allowed to request execution
    #: of. Anything else is DENY. This is intentionally just the AgentAction
    #: enum's own values - unlike the OLD Gemini-era PolicyGuard, there is
    #: only one action vocabulary in this architecture, so no additional
    #: tool-name allow-list or V1/V2 equivalence mapping is needed here.
    ALLOWED_ACTIONS = frozenset(a.value for a in AgentAction)

    def evaluate(self, proposal: ProposedAction) -> PolicyVerdict:
        """
        Evaluate a proposed action and return an authorization verdict.

        Args:
            proposal: The action the agent wants to take, with the signals
                it extracted to justify it.

        Returns:
            PolicyVerdict describing whether/how the action may proceed.
        """
        # Rule 1: action allow-list. An unrecognized action is never
        # allowed, no matter what triggered it.
        action_name = proposal.action.value
        if action_name not in self.ALLOWED_ACTIONS:
            return PolicyVerdict(
                decision=PolicyDecision.DENY,
                reason=f"'{action_name}' is not an allow-listed agent action.",
                rule="action_allowlist",
            )

        # DO_NOTHING is always safe regardless of any other signal - it has
        # no observable effect, so there is nothing to authorize against.
        if proposal.is_action(AgentAction.DO_NOTHING):
            return PolicyVerdict(
                decision=PolicyDecision.ALLOW,
                reason="No-op action requires no authorization.",
                rule="noop_allow",
            )

        # Rule 2: medical emergency takes absolute priority over everything
        # else, including opt-out. An agent must never attempt to diagnose,
        # give treatment advice, or "handle" an emergency conversationally -
        # it must escalate to human staff. NOTE: `is_emergency` is produced
        # by a demo-only keyword rule (see agent/conversation.py); it is not
        # a clinical judgment, and this rule's strictness does not make the
        # detection clinically complete.
        if proposal.is_emergency:
            return PolicyVerdict(
                decision=PolicyDecision.FORCE_ESCALATION,
                reason=(
                    "Message contains a potential medical emergency signal. "
                    "The agent must not provide diagnosis or treatment advice; "
                    "escalating to human staff / emergency guidance is required."
                ),
                rule="emergency_override",
            )

        # Rule 3: opt-out. Once a patient has opted out (or is opting out in
        # this message), the only actions permitted are recording the
        # opt-out - never further outbound contact or booking attempts.
        if proposal.is_opt_out or proposal.case.patient.opted_out:
            if proposal.is_action(AgentAction.RECORD_OPT_OUT):
                return PolicyVerdict(
                    decision=PolicyDecision.ALLOW,
                    reason="Recording patient opt-out request.",
                    rule="opt_out_record",
                )
            return PolicyVerdict(
                decision=PolicyDecision.DENY,
                reason=(
                    "Patient has opted out of contact. No further outbound "
                    "communication or booking action is permitted."
                ),
                rule="opt_out_enforced",
            )

        # Rule 4: clinical / financial / administrative questions are
        # outside the agent's competence and must go to a human.
        if proposal.is_clinical_question:
            return PolicyVerdict(
                decision=PolicyDecision.FORCE_ESCALATION,
                reason=(
                    "Message requires clinical, financial, or administrative "
                    "expertise the agent does not have. Escalating to staff."
                ),
                rule="clinical_question_boundary",
            )

        # Rule 5: booking requires unambiguous, affirmative consent.
        if proposal.is_action(AgentAction.CONFIRM_BOOKING):
            if proposal.consent_signal == "affirmative":
                return PolicyVerdict(
                    decision=PolicyDecision.ALLOW,
                    reason="Unambiguous affirmative consent to book was observed.",
                    rule="booking_consent_clear",
                )
            if proposal.consent_signal == "ambiguous":
                return PolicyVerdict(
                    decision=PolicyDecision.REQUIRE_CLARIFICATION,
                    reason=(
                        "Consent signal is ambiguous or contradictory "
                        "(e.g. both affirmative and negative language present). "
                        "The agent must ask a clarifying question before booking."
                    ),
                    rule="booking_consent_ambiguous",
                )
            return PolicyVerdict(
                decision=PolicyDecision.DENY,
                reason="No affirmative consent signal was observed for a booking action.",
                rule="booking_consent_missing",
            )

        # Default: no special rule applies, action may proceed.
        return PolicyVerdict(
            decision=PolicyDecision.ALLOW,
            reason="No safety rule restricts this action.",
            rule="default_allow",
        )
