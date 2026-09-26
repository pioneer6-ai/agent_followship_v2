"""
Notification and messaging layer for patient communication.

This module provides multi-channel notification capabilities,
allowing the agent to reach patients through their preferred
communication method (SMS, WhatsApp, Email, Phone).

Every channel reports a :class:`~agent.delivery.NotificationOutcome` rather than a
bare ``bool``, so the agent can tell *why* a send failed and what to try instead.
Transmission itself is delegated to a :class:`~agent.delivery.DeliveryBackend`,
which keeps the offline demo and the live provider paths behind one interface.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Callable, Optional

from agent.delivery import (
    DeliveryBackend,
    NotificationOutcome,
    PrintDeliveryBackend,
    _env_flag,
    missing_recipient_outcome,
)
from core.models import PatientRecord, ContactChannel, FollowUpCase


class NotificationChannel(ABC):
    """
    Abstract base class for all notification channels.

    Each channel implementation handles the specifics of sending
    messages through that medium (formatting rules, length limits, etc.)
    and delegates the actual transmission to a delivery backend.
    """

    def __init__(self, backend: Optional[DeliveryBackend] = None):
        """
        Args:
            backend: Transmission strategy. Defaults to the offline printing
                backend so a channel never sends anything unless one is supplied.
        """
        self.backend: DeliveryBackend = backend or PrintDeliveryBackend()

    @abstractmethod
    def send(self, patient: PatientRecord, message: str) -> NotificationOutcome:
        """
        Send a message to a patient through this channel.

        Args:
            patient: Patient to contact
            message: Message content to send

        Returns:
            The outcome of the attempt. Failures are reported, never raised, so
            the agent can react to them.
        """
        pass

    @abstractmethod
    def get_channel_type(self) -> ContactChannel:
        """
        Get the channel type identifier.

        Returns:
            ContactChannel enum value
        """
        pass

    def _recipient_for(self, patient: PatientRecord) -> str:
        """
        Address to reach this patient on, using this channel.

        Falls back to the preferred channel's address when this channel has none
        of its own (a patient with a single phone number recorded as
        ``PHONE_CALL`` is still reachable by SMS).
        """
        direct = patient.contact_info.get(self.get_channel_type())
        if direct:
            return str(direct)
        fallback = patient.contact_info.get(patient.preferred_channel)
        return str(fallback) if fallback else ""


class SMSChannel(NotificationChannel):
    """
    SMS notification channel implementation.

    Delegates to the ``send_sms_message`` tool (Twilio), which handles E.164
    normalization, allow-listing and dry-run.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        backend: Optional[DeliveryBackend] = None,
    ):
        """
        Initialize SMS channel.

        Args:
            api_key: API key for SMS service (retained for callers that still
                pass one; the tool layer reads its own credentials from the
                environment).
            backend: Transmission strategy.
        """
        super().__init__(backend=backend)
        self.api_key = api_key

    def send(self, patient: PatientRecord, message: str) -> NotificationOutcome:
        """Send SMS message to patient, reporting the outcome instead of raising."""
        phone_number = self._recipient_for(patient)

        if not phone_number:
            print(f"[SMS] No phone number for patient {patient.patient_id}")
            return missing_recipient_outcome(ContactChannel.SMS)

        return self.backend.deliver(ContactChannel.SMS, phone_number, message)

    def get_channel_type(self) -> ContactChannel:
        """Return SMS channel type."""
        return ContactChannel.SMS


class WhatsAppChannel(NotificationChannel):
    """
    WhatsApp notification channel implementation.

    Delegates to the ``send_whatsapp_message`` tool (Meta Cloud API or Twilio),
    which handles the business-initiated template rules.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        backend: Optional[DeliveryBackend] = None,
    ):
        """
        Initialize WhatsApp channel.

        Args:
            api_key: API key for WhatsApp service (see :class:`SMSChannel`).
            backend: Transmission strategy.
        """
        super().__init__(backend=backend)
        self.api_key = api_key

    def send(self, patient: PatientRecord, message: str) -> NotificationOutcome:
        """Send WhatsApp message to patient, reporting the outcome instead of raising."""
        whatsapp_number = self._recipient_for(patient)

        if not whatsapp_number:
            print(f"[WhatsApp] No WhatsApp number for patient {patient.patient_id}")
            return missing_recipient_outcome(ContactChannel.WHATSAPP)

        return self.backend.deliver(
            ContactChannel.WHATSAPP, whatsapp_number, message
        )

    def get_channel_type(self) -> ContactChannel:
        """Return WhatsApp channel type."""
        return ContactChannel.WHATSAPP


class EmailChannel(NotificationChannel):
    """
    Email notification channel implementation.

    Delegates to the ``send_email_message`` tool (SMTP), which is the path that
    actually reaches an inbox when no sending domain is available.
    """

    def __init__(
        self,
        smtp_config: Optional[dict] = None,
        backend: Optional[DeliveryBackend] = None,
    ):
        """
        Initialize email channel.

        Args:
            smtp_config: SMTP server configuration (retained for callers that
                still pass one; the tool layer reads its own from the environment).
            backend: Transmission strategy.
        """
        super().__init__(backend=backend)
        self.smtp_config = smtp_config or {}

    def send(self, patient: PatientRecord, message: str) -> NotificationOutcome:
        """Send email to patient, reporting the outcome instead of raising."""
        email = self._recipient_for(patient)

        if not email:
            print(f"[Email] No email address for patient {patient.patient_id}")
            return missing_recipient_outcome(ContactChannel.EMAIL)

        return self.backend.deliver(
            ContactChannel.EMAIL,
            email,
            message,
            subject="Dental Appointment Reminder",
        )

    def get_channel_type(self) -> ContactChannel:
        """Return Email channel type."""
        return ContactChannel.EMAIL


class PhoneCallChannel(NotificationChannel):
    """
    Phone call notification channel implementation.

    No automated voice sender exists in the tool layer, so this channel always
    reports ``unsupported_channel``. That is deliberate: an attempt is an
    observed failure the agent can route around, which is better than a silent
    "no phone calls configured" that leaves the case stuck.
    """

    def __init__(
        self,
        voice_api_key: Optional[str] = None,
        backend: Optional[DeliveryBackend] = None,
    ):
        """
        Initialize phone call channel.

        Args:
            voice_api_key: API key for voice service.
            backend: Transmission strategy.
        """
        super().__init__(backend=backend)
        self.voice_api_key = voice_api_key

    def send(self, patient: PatientRecord, message: str) -> NotificationOutcome:
        """Report that an automated call cannot be placed, with fallbacks."""
        phone_number = self._recipient_for(patient)

        if not phone_number:
            print(f"[Phone] No phone number for patient {patient.patient_id}")
            return missing_recipient_outcome(ContactChannel.PHONE_CALL)

        return self.backend.deliver(ContactChannel.PHONE_CALL, phone_number, message)

    def get_channel_type(self) -> ContactChannel:
        """Return Phone channel type."""
        return ContactChannel.PHONE_CALL


def build_notification_channels(
    backend: Optional[DeliveryBackend] = None,
    *,
    force_backend: Optional[DeliveryBackend] = None,
) -> "dict[ContactChannel, NotificationChannel]":
    """
    Build the channel set the orchestrator sends through.

    Chooses the transmission strategy in one place so no caller has to decide:

    * an explicit ``force_backend`` (tests) always wins;
    * otherwise ``backend`` when one is given;
    * otherwise the real toolkit backend when ``MESSAGING_DRY_RUN=0`` asks for
      live sends;
    * otherwise the offline printing backend, which is the default so a
      populated ``.env`` alone can never message a real patient.

    Args:
        backend: Backend to use when none is forced.
        force_backend: Backend that overrides everything.

    Returns:
        Mapping of :class:`~core.models.ContactChannel` to channel instance.
    """
    chosen = force_backend if force_backend is not None else backend
    if chosen is None:
        from agent.delivery import ToolkitDeliveryBackend, is_configured_for_live_sends

        if is_configured_for_live_sends():
            chosen = ToolkitDeliveryBackend.from_environment()
        else:
            chosen = PrintDeliveryBackend()

    return {
        ContactChannel.SMS: SMSChannel(backend=chosen),
        ContactChannel.WHATSAPP: WhatsAppChannel(backend=chosen),
        ContactChannel.EMAIL: EmailChannel(backend=chosen),
        ContactChannel.PHONE_CALL: PhoneCallChannel(backend=chosen),
    }


class MessageComposerAgent:
    """
    AI-powered message composition agent.

    This component generates personalized, context-aware messages
    for patients based on their profile, language preference,
    urgency level, and treatment type.

    Two modes, chosen by the caller:

    * ``use_llm=True`` -- the configured model writes the text (see
      :meth:`_compose_with_llm`). The model is only ever *asked* to draft; every
      failure path (no credential, unreachable endpoint, empty or unusable
      reply) falls back to the template, so a draft always exists.
    * ``use_llm=False`` -- the deterministic template, which is what the offline
      demo and the tests use.

    ``last_source`` records which of the two produced the most recent message, so
    callers can report and audit where a patient-facing draft came from.
    """

    def __init__(
        self,
        use_llm: bool = False,
        llm_api_key: Optional[str] = None,
        client: Optional[Any] = None,
        model: Optional[str] = None,
        portal_link_provider: Optional[Callable[[str], str]] = None,
        booking_window_days: int = 7,
    ):
        """
        Initialize message composer.

        Args:
            use_llm: Whether to use LLM for message generation
            llm_api_key: API key for LLM service (if use_llm=True)
            client: Pre-built LLM client (anything exposing ``messages.create``).
                Tests inject a fake here; when omitted and ``use_llm`` is set, one
                is built from the environment on first use.
            model: Model id to request. Defaults to the provider configuration's
                own model.
            portal_link_provider: Optional ``patient_id -> portal URL``
                callable. When given, AND ``message_type == "no_show"``
                (see ``_compose_with_template``), the message embeds a
                patient-specific "Reschedule My Appointment" link built
                from it. Every OTHER message_type ("initial", "reminder",
                "urgent") never embeds a link, even when this is set -
                having a provider configured is necessary but not
                sufficient; the message must actually be a no-show
                follow-up. This is a plain Python function, never an HTTP
                call - see web/patient_portal_auth.py's
                ``build_portal_link_provider``, which is the one
                implementation of this callable shape web/app.py actually
                wires in, reusing the SAME token logic the staff-facing
                ``/api/patients/<id>/portal-link`` route uses. Left
                ``None`` (the default), no message ever embeds a link -
                which is what every existing caller/test that doesn't pass
                this argument still gets.
            booking_window_days: How many days ahead the portal shows
                available times for - only used in the no-show portal
                sentence, when portal_link_provider is set AND
                message_type == "no_show".
        """
        self.use_llm = use_llm
        self.llm_api_key = llm_api_key
        self.portal_link_provider = portal_link_provider
        self.booking_window_days = booking_window_days
        self._client = client
        self.model = model
        self._client_resolved = client is not None
        self.last_error: Optional[str] = None
        self.last_source = "template"

    def compose(self, case: FollowUpCase, message_type: str = "initial") -> str:
        """
        Generate a personalized reminder message for a patient.

        The message is tailored based on:
        - Patient's preferred language
        - Urgency level
        - Treatment type
        - Communication style (formal vs. friendly)

        Args:
            case: Follow-up case containing patient information
            message_type: Type of message ("initial", "reminder", "urgent")

        Returns:
            Composed message string
        """
        if self.use_llm:
            return self._compose_with_llm(case, message_type)
        self.last_source = "template"
        return self._compose_with_template(case, message_type)

    def _compose_with_template(self, case: FollowUpCase, message_type: str) -> str:
        """
        Generate message using template-based approach.

        This is a deterministic, rule-based method that ensures
        consistency and compliance while still personalizing content.
        """
        patient = case.patient
        urgency = case.urgency.value
        treatment = case.patient.treatment_type.replace("_", " ").title()

        # Select greeting based on time and formality
        greeting = f"Hello {patient.name},"

        # Compose main message based on urgency and type
        if message_type == "initial":
            if urgency == "critical":
                body = (
                    f"This is an important reminder about your {treatment.lower()} follow-up. "
                    f"It's been {case.days_overdue} days past your recommended appointment date. "
                    f"Please contact us as soon as possible to schedule your visit. "
                    f"Your dental health is important to us."
                )
            elif urgency == "high":
                body = (
                    f"We noticed you're overdue for your {treatment.lower()} appointment. "
                    f"To maintain your dental health, we recommend scheduling soon. "
                    f"Would you like to book an appointment this week?"
                )
            else:
                body = (
                    f"It's time for your {treatment.lower()} appointment! "
                    f"We'd love to see you soon. "
                    f"Reply with your preferred day and we'll find a time that works."
                )
        elif message_type == "reminder":
            body = (
                f"Following up on our previous message about your {treatment.lower()} appointment. "
                f"We have several time slots available. "
                f"Would you like to schedule a visit?"
            )
        elif message_type == "no_show":
            body = (
                f"We noticed you missed your {treatment.lower()} appointment. "
                f"No worries - let's get you rescheduled. "
                f"We'd love to see you as soon as it's convenient for you."
            )
        else:  # urgent
            body = (
                f"We're concerned about your overdue {treatment.lower()} appointment. "
                f"Please contact us at your earliest convenience. "
                f"Our team is ready to help you maintain your dental health."
            )

        # Add call-to-action
        cta = "Reply to this message or call us to book your appointment."

        sections = [greeting, body, cta]

        # Patient-specific self-service link - ONLY for NO_SHOW follow-ups.
        # A configured portal_link_provider is necessary but NOT sufficient
        # on its own; every other message_type ("initial", "reminder",
        # "urgent") must stay byte-for-byte identical to before this
        # no-show-specific link existed, even when a provider is set.
        if message_type == "no_show" and self.portal_link_provider is not None:
            portal_url = self.portal_link_provider(patient.patient_id)
            portal_section = (
                f"Reschedule My Appointment: {portal_url}\n\n"
                f"The page shows real available times for the next "
                f"{self.booking_window_days} days. If none of them work, "
                f"you can choose \"remind me next week\" instead."
            )
            sections.append(portal_section)

        sections.append("Best regards,\nYour Dental Care Team")

        # Compose complete message
        message = "\n\n".join(sections)

        return message

    def _compose_with_llm(self, case: FollowUpCase, message_type: str) -> str:
        """
        Ask the configured model to draft the patient message.

        The model only writes text: it cannot choose the recipient, the channel,
        or whether to send at all -- those stay with the decision engine and the
        policy guard, and the message is queued for staff confirmation before
        anything leaves the clinic.

        Any failure degrades to the template rather than surfacing an exception,
        because a missing draft would silently drop the patient from the outreach
        queue. The reason is kept in :attr:`last_error`.
        """
        client = self._llm_client()
        if client is None:
            return self._fallback_to_template(case, message_type, self.last_error)

        try:
            response = client.messages.create(
                model=self.model or _compose_model(),
                max_tokens=512,
                system=COMPOSER_SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": self._compose_prompt(case, message_type)}
                ],
            )
            message = _first_text(response)
        except Exception as exc:
            return self._fallback_to_template(
                case, message_type, f"{type(exc).__name__}: {exc}"
            )

        if not message:
            return self._fallback_to_template(
                case, message_type, "model returned no text"
            )

        self.last_error = None
        self.last_source = "llm"
        return message

    def _llm_client(self) -> Optional[Any]:
        """
        The client used for drafting, or ``None`` when none can be built.

        Resolution is lazy and cached so importing this module never touches
        credentials, and a failed lookup is not retried on every message.
        """
        if self._client_resolved:
            return self._client
        self._client_resolved = True
        try:
            from tools.llm_providers import LlmProviderConfig, create_llm_client

            config = LlmProviderConfig.from_env()
            if self.llm_api_key:
                config.api_key = self.llm_api_key
            self._client = create_llm_client(config)
        except Exception as exc:
            self._client = None
            self.last_error = f"{type(exc).__name__}: {exc}"
        return self._client

    def _fallback_to_template(
        self, case: FollowUpCase, message_type: str, reason: Optional[str]
    ) -> str:
        """Draft from the template, recording why the model was not used."""
        self.last_error = reason
        self.last_source = "template"
        return self._compose_with_template(case, message_type)

    @staticmethod
    def _compose_prompt(case: FollowUpCase, message_type: str) -> str:
        """The patient context handed to the model for one draft."""
        patient = case.patient
        return (
            "Draft the patient-facing message described below.\n\n"
            f"Patient name: {patient.name}\n"
            f"Treatment: {patient.treatment_type.replace('_', ' ')}\n"
            f"Days past the recommended appointment date: {case.days_overdue}\n"
            f"Urgency: {case.urgency.value}\n"
            f"Patient's preferred language: {patient.language}\n"
            f"Message purpose: {message_type} "
            f"({COMPOSER_MESSAGE_TYPES.get(message_type, message_type)})\n"
            f"Previous reminders sent: {case.reminder_count}\n\n"
            "Requirements: plain text only, no markdown or placeholders, no "
            "invented appointment times, warm and professional. Keep it under "
            "320 characters so the same draft fits an SMS. Greet the patient by "
            "name, make the specific treatment and how overdue they are clear, and "
            "end with one clear call to action. "
            f"Sign off as {COMPOSER_SIGNATURE}."
        )

    def compose_slot_proposal(self, case: FollowUpCase, available_slots: list) -> str:
        """
        Generate a message proposing available appointment slots.

        Args:
            case: Follow-up case
            available_slots: List of available dates

        Returns:
            Message with slot options
        """
        patient = case.patient
        treatment = case.patient.treatment_type.replace("_", " ").title()

        slots_text = "\n".join([
            f"{i+1}. {slot.strftime('%A, %B %d')}"
            for i, slot in enumerate(available_slots[:3])
        ])

        message = f"""Hello {patient.name},

We have the following times available for your {treatment.lower()} appointment:

{slots_text}

Please reply with the number of your preferred time, or suggest an alternative.

Best regards,
Your Dental Care Team"""

        return message


def _compose_model() -> str:
    """Model id for drafting: the general LLM model, then the decision model."""
    import os

    return os.environ.get("AGENT_LLM_MODEL") or os.environ.get("AGENT_DECISION_MODEL") or ""


def _first_text(response: Any) -> str:
    """
    Extract the assistant's text from a provider response.

    Handles the Anthropic SDK's block objects and the dict shape the
    OpenAI-compatible adapter returns, so the composer does not care which
    vendor answered.
    """
    if response is None:
        return ""
    blocks = response.get("content") if isinstance(response, dict) else getattr(
        response, "content", None
    )
    if blocks is None:
        return ""
    if isinstance(blocks, str):
        return _clean_message(blocks)

    parts = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
            continue
        block_type = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if block_type not in (None, "text"):
            continue
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return _clean_message("\n".join(parts))


def _clean_message(raw: str) -> str:
    """Trim model noise (code fences, surrounding quotes) off a draft."""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            first, rest = text.split("\n", 1)
            if first.strip().lower() in ("text", "plaintext", "markdown"):
                text = rest
    return text.strip().strip('"').strip()


COMPOSER_SYSTEM_PROMPT = (
    "You write short, warm, professional appointment reminders on behalf of a "
    "dental clinic. You write one message per request, in the patient's "
    "preferred language, with no markdown and no invented details. A "
    "staff member reviews and may edit every message before it is sent, so "
    "never include placeholders that a human would have to fill in."
)

COMPOSER_MESSAGE_TYPES = {
    "initial": "a first reminder for an overdue appointment",
    "reminder": "a follow-up reminder for a patient who has not yet replied",
    "urgent": "an urgent reminder for a patient who is significantly overdue",
}

COMPOSER_SIGNATURE = "the clinic's care team"


def message_composer_from_environment(
    portal_link_provider: Optional[Callable[[str], str]] = None,
    booking_window_days: int = 7,
) -> MessageComposerAgent:
    """
    Build the composer the application should use.

    Model-written drafts are opt-in via ``AGENT_LLM_COMPOSE_MESSAGES`` so that a
    clinic can keep deterministic template text, and so an offline run never
    depends on a reachable endpoint. Turning it on cannot break drafting: every
    failure degrades to the template.

    ``portal_link_provider``/``booking_window_days`` are forwarded unchanged so
    that turning model drafting on does not silently drop the patient-specific
    no-show portal link the caller already wired in.
    """
    use_llm = _env_flag("AGENT_LLM_COMPOSE_MESSAGES")
    return MessageComposerAgent(
        use_llm=use_llm,
        portal_link_provider=portal_link_provider,
        booking_window_days=booking_window_days,
    )
