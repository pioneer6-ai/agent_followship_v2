"""
Conversation management and intent recognition for patient interactions.

This module handles multi-turn conversations with patients, understanding
their intent and deciding on appropriate follow-up actions.
"""

import re
from datetime import datetime, date
from typing import Optional, Tuple

from core.models import FollowUpCase, CaseStatus
from core.actions import AgentAction


# ---------------------------------------------------------------------------
# Demo safety-signal detection
#
# NOT A CLINICAL TRIAGE SYSTEM. This is a small, intentionally simple
# keyword/pattern set for demonstration purposes only: it shows where
# emergency and opt-out detection plug into the conversation pipeline before
# PolicyGuard authorizes any action. A real clinic deployment must replace
# these patterns with clinic-approved, reviewed policy/configuration (e.g. a
# maintained keyword list signed off by clinical staff, or a dedicated
# classifier) before going anywhere near real patients. Keeping this
# detection isolated in its own functions, rather than inlined into
# `_recognize_intent`, is deliberate so that swap can happen without
# touching the rest of the intent-recognition pipeline.
# ---------------------------------------------------------------------------

#: Keywords suggesting a potential medical emergency in the patient's own
#: words. Deliberately small and conservative for a demo: false positives
#: (over-escalating) are the safe failure mode here, not false negatives.
#: Each concept below is expressed with a couple of common natural-language
#: variants (e.g. "severe bleeding" and "bleeding badly" both count), but
#: this remains a fixed keyword list, not an exhaustive or clinically
#: validated one - see the module note above.
EMERGENCY_KEYWORDS = (
    r"\bemergency\b",
    r"\b911\b",
    r"\bsevere (pain|bleeding|swelling)\b",
    # Bleeding: intensity ("bleeding badly"/"heavy bleeding") and duration/
    # uncontrollability ("bleeding won't stop", "can't stop bleeding"), in
    # either word order since patients phrase this both ways.
    r"\bbleeding (badly|a lot|heavily|profusely)\b",
    r"\bheavy bleeding\b",
    r"\bbleeding (won.?t|will not|does.?n.?t|cannot|can.?t) stop\b",
    r"\b(cannot|can.?t) stop (the )?bleeding\b",
    # Breathing difficulty, and swelling severity, again in either order.
    r"\b(trouble|difficulty) breathing\b",
    r"\bcan.?t breathe\b",
    r"\b(badly|severely) swollen\b",
    # Pain intensity, adjective before or after "pain".
    r"\b(unbearable|excruciating|extreme|intense) pain\b",
    r"\bpain (is|.s) (unbearable|excruciating|extreme|intense)\b",
    r"\bchest pain\b",
    r"\bpassed out\b",
    r"\bunconscious\b",
)

#: Keywords suggesting the patient wants to stop being contacted.
OPT_OUT_KEYWORDS = (
    r"\bstop\b",
    r"\bunsubscribe\b",
    r"\bopt.?out\b",
    r"\bremove me\b",
    r"\bdo not contact me\b",
    r"\bdon.?t contact me\b",
    r"\bdon.?t message me\b",
)


def is_emergency_signal(message: str) -> bool:
    """
    Demo-only emergency keyword check. See module note above: this is not a
    clinical triage system, just a small pattern set that lets the safety
    pipeline (PolicyGuard's emergency_override rule) be exercised end to end.

    Args:
        message: Normalized (lowercased, stripped) patient message.

    Returns:
        True if a configured emergency keyword/pattern was found.
    """
    return any(re.search(pattern, message) for pattern in EMERGENCY_KEYWORDS)


def is_opt_out_signal(message: str) -> bool:
    """
    Demo-only opt-out keyword check. See module note above.

    Args:
        message: Normalized (lowercased, stripped) patient message.

    Returns:
        True if a configured opt-out keyword/pattern was found.
    """
    return any(re.search(pattern, message) for pattern in OPT_OUT_KEYWORDS)


# ---------------------------------------------------------------------------
# Consent qualification detection
#
# Safety rule: an affirmative word ("yes", "sure", "okay", ...) is NEVER by
# itself unconditional consent to book a specific appointment. If the same
# message also contains a negation, a scheduling constraint, a temporal
# qualifier, or a reschedule request, consent is conditional and must be
# treated as ambiguous - never as plain affirmative. This is deliberately a
# structural rule (detect "some qualifying language is present"), not an
# enumeration of phrases like "not this week": the goal is to catch the
# *category* of conditional replies ("yes, but not tomorrow", "sure, but
# after 5pm", "yes, but a different time", "sure, next week instead", ...)
# rather than a specific wording of it.
# ---------------------------------------------------------------------------

#: Any of these appearing alongside an affirmative word means the patient
#: qualified, negated, or conditioned their agreement - so it is not
#: unconditional consent to the specific slot/appointment being discussed.
QUALIFYING_LANGUAGE_PATTERNS = (
    # Negation words, wherever they appear in the message (not just
    # attached to a specific decline phrase like "not now").
    r'\b(not|n.t|never|don.t|won.t|can.t|isn.t|wasn.t)\b',
    # Explicit rescheduling / alternative-time requests.
    r'\b(reschedule|different time|different day|another time|another day|'
    r'instead|change (the|my) (time|date|appointment))\b',
    # Temporal qualifiers that condition *when*, rather than confirming the
    # specific time already on offer. "after/before <digit>" intentionally
    # has no trailing \b: a time like "5pm" is one contiguous word-run, so
    # a trailing boundary after the digit would never match it.
    r'\b(next week|next month|later|in the (morning|afternoon|evening))\b',
    r'\b(after|before) \d',
)


def _is_qualified_affirmation(message: str) -> bool:
    """
    True if the message contains qualifying language (see module note
    above) that means an accompanying affirmative word is not unconditional
    consent.

    Args:
        message: Normalized (lowercased, stripped) patient message.

    Returns:
        True if any qualifying pattern is present.
    """
    return any(re.search(pattern, message) for pattern in QUALIFYING_LANGUAGE_PATTERNS)


class ConversationManager:
    """
    Manages multi-turn conversations with patients.
    
    This is the core "autonomous decision-making" component of the agent.
    It analyzes patient replies, recognizes intent, and determines the
    next action to take - demonstrating the agent's ability to handle
    dynamic, unscripted interactions.
    
    Key capabilities:
    - Intent recognition (booking, declining, rescheduling, questions)
    - Context tracking across conversation turns
    - Escalation detection (when human intervention is needed)
    """

    def __init__(self, use_llm: bool = False):
        """
        Initialize conversation manager.
        
        Args:
            use_llm: Whether to use LLM for intent recognition (vs. rule-based)
        """
        self.use_llm = use_llm

    def handle_reply(
        self, case: FollowUpCase, incoming_message: str
    ) -> Tuple[AgentAction, Optional[dict]]:
        """
        Process a patient's reply and determine the next action.
        
        This is the heart of the autonomous agent - it "observes" the patient's
        response and "decides" what to do next without human intervention.

        Safety signals (emergency, opt-out, consent ambiguity - see the demo
        safety-signal note near the top of this module) are extracted here
        and carried in the returned context under "signals", so the caller
        can hand them to PolicyGuard before executing the proposed action.
        This function only proposes; it never authorizes.
        
        Args:
            case: Current follow-up case
            incoming_message: Patient's message text
            
        Returns:
            Tuple of (AgentAction to take, Optional context dictionary)
        """
        # Log the incoming message
        case.add_to_log(f"Patient: {incoming_message}")
        
        # Normalize message for analysis
        message_lower = incoming_message.lower().strip()
        
        # Recognize intent and decide action
        intent, context = self._recognize_intent(message_lower)

        # Extract the safety signals PolicyGuard needs. These are computed
        # independently of `intent` so an emergency/opt-out phrase is caught
        # even if it happens to also match another pattern (e.g. "stop, this
        # is an emergency" should surface both signals; PolicyGuard decides
        # precedence, not this method).
        context["signals"] = {
            "is_emergency": is_emergency_signal(message_lower),
            "is_opt_out": is_opt_out_signal(message_lower),
            "consent_signal": context.get("consent_signal"),
            "is_clinical_question": intent == "ask_question",
        }
        
        # Map intent to agent action
        action = self._intent_to_action(intent, case, context)
        
        # Log the decision
        case.add_to_log(f"Agent Decision: {action.value} (Intent: {intent})")
        
        return action, context

    def _recognize_intent(self, message: str) -> Tuple[str, dict]:
        """
        Recognize the patient's intent from their message.
        
        Intent categories:
        - confirm_booking: Patient agrees to schedule appointment
        - decline: Patient doesn't want appointment now
        - reschedule: Patient wants different time
        - ask_question: Patient has questions
        - unclear: Cannot determine intent (needs escalation)
        
        Args:
            message: Normalized patient message
            
        Returns:
            Tuple of (intent string, context dictionary)
        """
        context = {}

        # Opt-out takes priority over every other intent: a patient who says
        # "stop" alongside anything else must not be routed into booking or
        # decline handling first. See the demo safety-signal note above -
        # this reuses the same keyword check PolicyGuard's signal relies on.
        if is_opt_out_signal(message):
            return "opt_out", context

        # Check for booking confirmation patterns
        confirm_patterns = [
            r'\b(yes|yeah|sure|ok|okay|sounds good|that works|perfect)\b',
            r'\b(book|schedule|confirm|i.ll take|i want)\b',
            r'\b([1-3])\b',  # Selecting a slot number
        ]
        # Check for decline patterns (also used to detect ambiguous consent
        # when they co-occur with a confirm pattern in the same message).
        decline_patterns = [
            r'\b(no|not now|maybe later|cancel|don.t need|not interested)\b',
            r'\b(busy|away|traveling|out of town)\b',
        ]

        confirm_matched = any(re.search(pattern, message) for pattern in confirm_patterns)
        decline_matched = any(re.search(pattern, message) for pattern in decline_patterns)
        qualified = _is_qualified_affirmation(message)

        if confirm_matched and (decline_matched or qualified):
            # Contradictory or conditioned consent in the same message (e.g.
            # "yes but not this week", "sure, but after 5pm", "yes but a
            # different time"). An affirmative word alone is never enough:
            # any co-occurring negation, scheduling constraint, temporal
            # qualifier, or reschedule request means the patient did not
            # unconditionally agree to a specific appointment. PolicyGuard
            # must require clarification rather than booking or declining
            # on a guess - see the module-level note on this rule below.
            context['consent_signal'] = 'ambiguous'
            return "confirm_booking", context

        if confirm_matched:
            # Check if they specified a slot number
            slot_match = re.search(r'\b([1-3])\b', message)
            if slot_match:
                context['selected_slot'] = int(slot_match.group(1))
            context['consent_signal'] = 'affirmative'
            return "confirm_booking", context

        if decline_matched:
            context['consent_signal'] = 'negative'
            return "decline", context
        
        # Check for reschedule patterns
        reschedule_patterns = [
            r'\b(different time|another day|reschedule|change)\b',
            r'\b(next week|next month|later)\b',
            r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',
        ]
        
        if any(re.search(pattern, message) for pattern in reschedule_patterns):
            # Try to extract preferred day
            days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
            for day in days:
                if day in message:
                    context['preferred_day'] = day
                    break
            return "reschedule", context
        
        # Check for questions
        question_patterns = [
            r'\?',
            r'\b(what|when|where|how|why|who)\b',
            r'\b(cost|price|insurance|covered)\b',
            r'\b(question|ask|wondering|curious)\b',
        ]
        
        if any(re.search(pattern, message) for pattern in question_patterns):
            context['question_text'] = message
            return "ask_question", context
        
        # If none of the above, intent is unclear
        return "unclear", context

    def _intent_to_action(
        self, intent: str, case: FollowUpCase, context: dict
    ) -> AgentAction:
        """
        Map recognized intent to concrete agent *proposed* action.

        This is the "propose" step, not "execute": it deliberately does not
        mutate `case.status`. Status changes are side effects, and per the
        required reasoning loop (message -> signal extraction -> proposed
        action -> PolicyGuard -> authorized action -> execution), state
        mutation happens only after PolicyGuard authorizes the action, in
        the orchestrator. This mirrors the OLD ConversationManager, which
        never touched case.status for the same reason.

        Args:
            intent: Recognized intent
            case: Current follow-up case
            context: Additional context from intent recognition
            
        Returns:
            AgentAction to execute
        """
        if intent == "opt_out":
            return AgentAction.RECORD_OPT_OUT

        if intent == "confirm_booking":
            return AgentAction.CONFIRM_BOOKING

        elif intent == "decline":
            return AgentAction.MARK_DECLINED

        elif intent == "reschedule":
            return AgentAction.PROPOSE_SLOT

        elif intent == "ask_question":
            # Patient has questions - this needs human expertise
            # Agent knows its boundaries and escalates appropriately
            return AgentAction.ESCALATE_TO_STAFF
        
        else:  # unclear
            # Cannot understand intent
            # Check if we've already sent multiple reminders
            if case.reminder_count >= 2:
                # After 2 unclear responses, escalate to human
                return AgentAction.ESCALATE_TO_STAFF
            else:
                # Try sending another reminder with clearer options
                return AgentAction.SEND_REMINDER

    def generate_response(
        self, action: AgentAction, case: FollowUpCase, context: dict
    ) -> str:
        """
        Generate appropriate response message based on action.
        
        Args:
            action: Action being taken
            case: Current follow-up case
            context: Context from conversation
            
        Returns:
            Response message to send to patient
        """
        patient_name = case.patient.name
        
        if action == AgentAction.CONFIRM_BOOKING:
            return f"Great, {patient_name}! We've confirmed your appointment. You'll receive a confirmation shortly with the details."
        
        elif action == AgentAction.MARK_DECLINED:
            return f"Understood, {patient_name}. If you'd like to schedule in the future, just let us know. Take care!"
        
        elif action == AgentAction.PROPOSE_SLOT:
            return "Let me check our available times and get back to you with options."
        
        elif action == AgentAction.ESCALATE_TO_STAFF:
            return f"Thank you for your message, {patient_name}. One of our team members will contact you shortly to help with your request."
        
        elif action == AgentAction.SEND_REMINDER:
            return f"Hi {patient_name}, just following up - would you like to schedule your appointment? Please reply 'yes' to book or 'no' if not needed right now."

        elif action == AgentAction.RECORD_OPT_OUT:
            return f"Understood, {patient_name}. We won't contact you about this again. Take care!"

        elif action == AgentAction.REQUEST_CLARIFICATION:
            return f"Just to confirm, {patient_name} - would you like us to book the appointment, or would you prefer a different time?"

        elif action == AgentAction.DO_NOTHING:
            # Defense-in-depth only: a denied/no-op action must produce no
            # outbound message. The orchestrator enforces this explicitly
            # and must not rely on this empty string alone (see
            # agent/orchestrator.py's handle_incoming_reply).
            return ""

        else:
            return "Thank you for your response."

    def should_escalate(self, case: FollowUpCase) -> bool:
        """
        Determine if a case should be escalated to human staff.
        
        Escalation criteria demonstrate the agent's self-awareness
        of its limitations - a critical feature for healthcare applications.
        
        Escalate when:
        - Too many unanswered reminders
        - Patient asks complex questions
        - Critical urgency with no response
        - Patient seems confused or frustrated
        
        Args:
            case: Follow-up case to evaluate
            
        Returns:
            True if case should be escalated
        """
        # Too many reminders without clear response
        if case.reminder_count >= 3 and case.status == CaseStatus.AWAITING_REPLY:
            return True
        
        # Critical cases need human attention
        if case.urgency.value == "critical" and case.reminder_count >= 1:
            return True
        
        # Check conversation log for confusion indicators
        recent_messages = case.conversation_log[-3:] if case.conversation_log else []
        confusion_keywords = ["don't understand", "confused", "what do you mean", "unclear"]
        
        for msg in recent_messages:
            if any(keyword in msg.lower() for keyword in confusion_keywords):
                return True
        
        return False
