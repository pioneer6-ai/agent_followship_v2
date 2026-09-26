"""
Tests for the patient-message confirmation workflow.

The contract this file exists to pin down: the agent *drafts*, a human
*approves*, and only then does anything reach a patient. An agent that messages
patients on its own initiative is not acceptable in a clinic, so these tests
assert the two halves separately -- that drafting transmits nothing, and that
confirmation transmits exactly what a staff member selected, with their edits.

Both halves are exercised through the real orchestrator with an in-memory
backend; the LLM drafting path is driven by an injected fake client.
"""

from __future__ import annotations
import json
import os
import sys
from datetime import date, timedelta

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agent.delivery import DeliveryBackend, NotificationOutcome  # noqa: E402
from agent.notifications import MessageComposerAgent  # noqa: E402
from agent.orchestrator import FollowUpAgentOrchestrator  # noqa: E402
from core.actions import AgentAction  # noqa: E402
from core.config import ClinicPolicyConfig  # noqa: E402
from core.data_access import MockCalendarIntegration, MockPatientDataStore  # noqa: E402
from core.models import CaseStatus, ContactChannel, PatientRecord  # noqa: E402

TODAY = date(2026, 6, 1)


class RecordingBackend(DeliveryBackend):
    """In-memory backend that records every transmission."""

    def __init__(self, *, succeeds: bool = True):
        self.succeeds = succeeds
        self.sent = []

    def name(self) -> str:
        return "recording-backend"

    def deliver(self, channel, recipient, message, *, subject=None):
        self.sent.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "subject": subject,
            }
        )
        return NotificationOutcome(
            success=self.succeeds,
            channel=channel,
            recipient=recipient,
            simulated=True,
            message_id="TEST-1" if self.succeeds else None,
            error_code=None if self.succeeds else "provider_error",
            error_message=None if self.succeeds else "scripted failure",
        )


def make_agent(backend: RecordingBackend, *, composer=None) -> FollowUpAgentOrchestrator:
    """Build an orchestrator over one overdue patient."""
    store, calendar = MockPatientDataStore(), MockCalendarIntegration()
    store.add_patient(
        PatientRecord(
            patient_id="P100",
            name="Alice Tan",
            contact_info={
                ContactChannel.SMS: "+6583536885",
                ContactChannel.EMAIL: "alice@example.com",
            },
            preferred_channel=ContactChannel.SMS,
            last_visit_date=TODAY - timedelta(days=60),
            treatment_type="cleaning",
            recall_interval_days=30,
            language="en",
        )
    )
    agent = FollowUpAgentOrchestrator(
        store,
        calendar,
        ClinicPolicyConfig(),
        delivery_backend=backend,
        message_composer=composer,
    )
    return agent


def run_cycle(agent) -> None:
    """Run one daily cycle, discarding the console narration."""
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        agent.run_daily_cycle(TODAY)


# ---------------------------------------------------------------------------
# Drafting must never transmit
# ---------------------------------------------------------------------------


class TestDraftingTransmitsNothing:
    """The agent proposes; it does not send."""

    def test_a_cycle_queues_a_draft_instead_of_sending(self):
        backend = RecordingBackend()
        agent = make_agent(backend)

        run_cycle(agent)

        assert backend.sent == [], "a cycle must not transmit anything by itself"
        pending = agent.get_pending_sends()
        assert len(pending) == 1
        assert pending[0]["patient_id"] == "P100"
        assert pending[0]["status"] == "pending"
        assert pending[0]["message"]

    def test_the_queued_draft_records_its_provenance(self):
        """Staff reviewing a draft must be able to tell what wrote it."""
        agent = make_agent(RecordingBackend())

        run_cycle(agent)

        draft = agent.get_pending_sends()[0]
        assert draft["composed_by"] == "template"
        assert draft["subject"]
        assert draft["channel"] == "sms"
        assert draft["recipient"] == "+6583536885"

    def test_the_queue_is_json_serialisable_for_the_outreach_page(self):
        """The panel is fed straight from this payload, so no internals may leak."""
        agent = make_agent(RecordingBackend())
        run_cycle(agent)

        payload = agent.get_pending_sends()
        encoded = json.dumps(payload)

        assert "dedupe_key" not in encoded
        assert json.loads(encoded) == payload

    def test_the_case_does_not_advance_until_a_human_confirms(self):
        agent = make_agent(RecordingBackend())
        run_cycle(agent)

        case = agent.get_case_by_patient_id("P100")
        assert case.status is CaseStatus.PENDING
        assert case.reminder_count == 0

    def test_the_same_message_is_not_queued_twice(self):
        """Re-running a cycle before staff act must not pile up duplicates."""
        agent = make_agent(RecordingBackend())

        run_cycle(agent)
        run_cycle(agent)

        assert agent.get_pending_send_count() == 1


# ---------------------------------------------------------------------------
# Editing is a first-class step
# ---------------------------------------------------------------------------


class TestDoctorEditsAreHonoured:
    """What the doctor wrote is what goes out."""

    def test_an_edit_is_persisted_with_its_author(self):
        agent = make_agent(RecordingBackend())
        run_cycle(agent)
        send_id = agent.get_pending_sends()[0]["send_id"]

        updated = agent.update_pending_send(
            send_id, "Please come in on Tuesday at 3pm.", edited_by="dr.lee"
        )

        assert updated["message"] == "Please come in on Tuesday at 3pm."
        assert updated["edited_by"] == "dr.lee"
        assert updated["edited_at"]
        assert updated["status"] == "pending", "editing must not send"

    def test_editing_transmits_nothing(self):
        backend = RecordingBackend()
        agent = make_agent(backend)
        run_cycle(agent)
        send_id = agent.get_pending_sends()[0]["send_id"]

        agent.update_pending_send(send_id, "Edited text", edited_by="dr.lee")

        assert backend.sent == []

    def test_the_edited_text_is_what_actually_gets_sent(self):
        backend = RecordingBackend()
        agent = make_agent(backend)
        run_cycle(agent)
        send_id = agent.get_pending_sends()[0]["send_id"]
        agent.update_pending_send(send_id, "Edited by the doctor", edited_by="dr.lee")

        agent.confirm_pending_sends([send_id])

        assert len(backend.sent) == 1
        assert backend.sent[0]["message"] == "Edited by the doctor"

    def test_a_blank_edit_is_rejected_rather_than_sending_nothing(self):
        agent = make_agent(RecordingBackend())
        run_cycle(agent)
        send_id = agent.get_pending_sends()[0]["send_id"]

        result = agent.update_pending_send(send_id, "   ", edited_by="dr.lee")

        assert result["status"] == "invalid"
        assert agent.get_pending_sends()[0]["message"], "the original draft must survive"

    def test_an_overlong_edit_is_rejected(self):
        agent = make_agent(RecordingBackend())
        run_cycle(agent)
        send_id = agent.get_pending_sends()[0]["send_id"]

        result = agent.update_pending_send(send_id, "x" * 4001, edited_by="dr.lee")

        assert result["status"] == "invalid"

    def test_editing_an_unknown_id_is_reported_not_raised(self):
        agent = make_agent(RecordingBackend())

        assert agent.update_pending_send("nope", "hi")["status"] == "not_found"


# ---------------------------------------------------------------------------
# Confirmation is the only send
# ---------------------------------------------------------------------------


class TestConfirmationSends:
    """Only the selected rows are transmitted."""

    @staticmethod
    def agent_with_two_drafts():
        store, calendar = MockPatientDataStore(), MockCalendarIntegration()
        for patient_id, name in (("P1", "Alice"), ("P2", "Bob")):
            store.add_patient(
                PatientRecord(
                    patient_id=patient_id,
                    name=name,
                    contact_info={ContactChannel.SMS: f"+658353688{patient_id[-1]}"},
                    preferred_channel=ContactChannel.SMS,
                    last_visit_date=TODAY - timedelta(days=60),
                    treatment_type="cleaning",
                    recall_interval_days=30,
                    language="en",
                )
            )
        backend = RecordingBackend()
        agent = FollowUpAgentOrchestrator(
            store, calendar, ClinicPolicyConfig(), delivery_backend=backend
        )
        run_cycle(agent)
        return agent, backend

    def test_confirming_sends_and_advances_the_case(self):
        agent = make_agent(RecordingBackend())
        run_cycle(agent)
        send_id = agent.get_pending_sends()[0]["send_id"]
        backend = agent.delivery_backend

        results = agent.confirm_pending_sends([send_id])

        assert results == [{"send_id": send_id, "status": "sent"}]
        assert len(backend.sent) == 1
        case = agent.get_case_by_patient_id("P100")
        assert case.status is CaseStatus.MESSAGE_SENT
        assert case.reminder_count == 1

    def test_only_the_selected_patient_is_contacted(self):
        """Confirming one row must not sweep up the other 20."""
        agent, backend = self.agent_with_two_drafts()
        drafts = agent.get_pending_sends()
        assert len(drafts) == 2

        chosen = drafts[0]
        agent.confirm_pending_sends([chosen["send_id"]])

        assert [sent["recipient"] for sent in backend.sent] == [chosen["recipient"]]
        assert agent.get_pending_send_count() == 1, "the other draft must stay queued"

    def test_a_confirmed_draft_leaves_the_queue(self):
        agent = make_agent(RecordingBackend())
        run_cycle(agent)
        send_id = agent.get_pending_sends()[0]["send_id"]

        agent.confirm_pending_sends([send_id])

        assert agent.get_pending_sends() == []

    def test_confirming_twice_does_not_send_twice(self):
        backend = RecordingBackend()
        agent = make_agent(backend)
        run_cycle(agent)
        send_id = agent.get_pending_sends()[0]["send_id"]

        agent.confirm_pending_sends([send_id])
        second = agent.confirm_pending_sends([send_id])

        assert second == [{"send_id": send_id, "status": "sent"}]
        assert len(backend.sent) == 1, "a double click must not double-send"

    def test_an_unknown_id_is_reported_not_raised(self):
        agent = make_agent(RecordingBackend())

        assert agent.confirm_pending_sends(["nope"]) == [
            {"send_id": "nope", "status": "not_found"}
        ]

    def test_a_failed_send_escalates_and_is_not_reported_as_sent(self):
        backend = RecordingBackend(succeeds=False)
        agent = make_agent(backend)
        run_cycle(agent)
        send_id = agent.get_pending_sends()[0]["send_id"]

        results = agent.confirm_pending_sends([send_id])

        assert results[0]["status"] == "failed"
        assert agent.get_case_by_patient_id("P100").status is CaseStatus.ESCALATED


class TestCancellation:
    """Discarding a draft must be silent and final."""

    def test_cancelling_transmits_nothing_and_persists_nothing(self):
        backend = RecordingBackend()
        agent = make_agent(backend)
        run_cycle(agent)
        send_id = agent.get_pending_sends()[0]["send_id"]

        results = agent.cancel_pending_sends([send_id])

        assert results == [{"send_id": send_id, "status": "cancelled"}]
        assert backend.sent == []
        assert agent.get_pending_sends() == []
        assert agent.get_case_by_patient_id("P100").reminder_count == 0

    def test_a_cancelled_draft_cannot_be_confirmed_later(self):
        backend = RecordingBackend()
        agent = make_agent(backend)
        run_cycle(agent)
        send_id = agent.get_pending_sends()[0]["send_id"]
        agent.cancel_pending_sends([send_id])

        assert agent.confirm_pending_sends([send_id]) == [
            {"send_id": send_id, "status": "cancelled"}
        ]
        assert backend.sent == []


class TestTerminalStatusesSurviveLateConfirmation:
    """A stale draft must not re-open a case that has already moved on."""

    def test_confirming_an_old_reminder_does_not_reopen_a_booked_case(self):
        agent = make_agent(RecordingBackend())
        run_cycle(agent)
        send_id = agent.get_pending_sends()[0]["send_id"]

        # The patient books before staff get around to the reminder.
        case = agent.get_case_by_patient_id("P100")
        case.status = CaseStatus.BOOKED

        agent.confirm_pending_sends([send_id])

        assert agent.get_case_by_patient_id("P100").status is CaseStatus.BOOKED


# ---------------------------------------------------------------------------
# Who writes the drafts
# ---------------------------------------------------------------------------


class _Response:
    """Stand-in for a provider response; ``content`` is text or a list of blocks."""

    def __init__(self, text):
        self.content = text


class _Messages:
    def __init__(self, owner):
        self._owner = owner

    def create(self, **kwargs):
        self._owner.calls.append(kwargs)
        if isinstance(self._owner.reply, Exception):
            raise self._owner.reply
        return _Response(self._owner.reply)


class FakeLlmClient:
    """Minimal ``client.messages.create`` surface for the composer."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []
        self.messages = _Messages(self)


class TestComposerUsesTheModel:
    """The model writes the draft when it can, the template when it cannot."""

    def _case(self):
        from tests.conftest import make_case

        return make_case()

    def test_the_model_text_is_used_verbatim_with_its_source_recorded(self):
        client = FakeLlmClient("Hi Alice, please book your cleaning this week.")
        composer = MessageComposerAgent(use_llm=True, client=client, model="test-model")

        message = composer.compose(self._case(), "initial")

        assert message == "Hi Alice, please book your cleaning this week."
        assert composer.last_source == "llm"
        assert composer.last_error is None
        assert client.calls[0]["model"] == "test-model"

    def test_markdown_fences_are_stripped_from_the_draft(self):
        client = FakeLlmClient("```text\nHi Alice.\n```")
        composer = MessageComposerAgent(use_llm=True, client=client)

        assert composer.compose(self._case(), "initial") == "Hi Alice."

    def test_a_thinking_only_reply_falls_back_to_the_template(self):
        """A reasoning block with no text must not become a blank patient message."""
        client = FakeLlmClient(
            [
                type("Block", (), {"type": "thinking", "thinking": "hmm"})(),
            ]
        )
        composer = MessageComposerAgent(use_llm=True, client=client)

        message = composer.compose(self._case(), "initial")

        assert "Hello" in message
        assert composer.last_source == "template"
        assert composer.last_error == "model returned no text"

    def test_a_provider_crash_falls_back_to_the_template(self):
        client = FakeLlmClient(RuntimeError("endpoint down"))
        composer = MessageComposerAgent(use_llm=True, client=client)

        message = composer.compose(self._case(), "initial")

        assert "Hello" in message
        assert composer.last_source == "template"
        assert "RuntimeError" in composer.last_error

    def test_the_template_path_never_calls_a_model(self):
        client = FakeLlmClient("should not be used")
        composer = MessageComposerAgent(use_llm=False, client=client)

        message = composer.compose(self._case(), "initial")

        assert "Hello" in message
        assert client.calls == []
        assert composer.last_source == "template"


class TestComposerIsOptIn:
    """Model drafting is a switch a clinic turns on deliberately."""

    def test_it_is_off_unless_asked_for(self, monkeypatch):
        from agent.notifications import message_composer_from_environment

        monkeypatch.delenv("AGENT_LLM_COMPOSE_MESSAGES", raising=False)
        assert message_composer_from_environment().use_llm is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
    def test_it_turns_on_for_the_documented_truthy_values(self, monkeypatch, value):
        from agent.notifications import message_composer_from_environment

        monkeypatch.setenv("AGENT_LLM_COMPOSE_MESSAGES", value)
        assert message_composer_from_environment().use_llm is True

    def test_a_composer_without_a_credential_still_produces_a_draft(self, monkeypatch):
        """The whole point of the fallback: drafting never depends on a network."""
        monkeypatch.setenv("AGENT_PROVIDER_KIND", "anthropic")
        monkeypatch.delenv("AGENT_LLM_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        composer = MessageComposerAgent(use_llm=True)

        message = composer.compose(self._case(), "initial")

        assert "Hello" in message
        assert composer.last_source == "template"
        assert composer.last_error

    def _case(self):
        from tests.conftest import make_case

        return make_case()


class TestLiveSendGate:
    """Real transmissions stay behind two independent switches.

    Staff confirmation is what authorises a *patient* message; it does not
    authorise *live network traffic*. That remains gated separately so a demo
    or a test run can never email a real inbox by accident.
    """

    def _config(self, monkeypatch, *, dry_run, live_sends):
        for name in list(os.environ):
            if name.startswith("MESSAGING_") or name.startswith("AGENT_"):
                monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("MESSAGING_DRY_RUN", "0" if dry_run is False else "1")
        monkeypatch.setenv("AGENT_LIVE_SENDS", "1" if live_sends else "0")

        from tools.config import MessagingConfig

        return MessagingConfig.from_env()

    @staticmethod
    def _gate(config, monkeypatch, *, live_sends):
        """Compose the agent's gate from its two documented halves."""
        from agent.delivery import _env_flag

        monkeypatch.setenv("AGENT_LIVE_SENDS", "1" if live_sends else "0")
        return _env_flag("AGENT_LIVE_SENDS") and config.live_requested

    def test_live_traffic_needs_both_switches(self, monkeypatch):
        assert self._gate(
            self._config(monkeypatch, dry_run=True, live_sends=True),
            monkeypatch,
            live_sends=True,
        ) is False
        assert self._gate(
            self._config(monkeypatch, dry_run=False, live_sends=False),
            monkeypatch,
            live_sends=False,
        ) is False
        assert self._gate(
            self._config(monkeypatch, dry_run=False, live_sends=True),
            monkeypatch,
            live_sends=True,
        ) is True

    def test_the_gate_is_closed_without_the_agent_switch(self, monkeypatch):
        """A populated .env alone must never be enough to reach a patient."""
        from agent.delivery import is_configured_for_live_sends

        monkeypatch.delenv("AGENT_LIVE_SENDS", raising=False)

        assert is_configured_for_live_sends() is False

    def test_dry_run_is_the_default(self):
        """An untouched environment must never be able to send."""
        from tools.config import MessagingConfig

        assert MessagingConfig().live_requested is False
