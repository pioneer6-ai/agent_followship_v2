"""
Tests for the NO_SHOW -> Patient Portal email wiring.

Scope: this feature wires up the ONE missing piece - detecting that a
FollowUpCase's SEND_REMINDER is for a patient with a REAL, current missed
appointment, and only then calling
`MessageComposerAgent.compose(case, "no_show")` instead of the existing
"urgent"/"initial" selection. `MessageComposerAgent` already knew how to
render the no_show template + embed the patient's Patient Portal link
(see agent/notifications.py and tests/test_portal_link_in_reminders.py) -
nothing about that composer logic changes here.

The real no-show signal (agent/orchestrator.py's SEND_REMINDER handling,
via SchedulingDatabaseCalendarAdapter.has_missed_appointment) is a
CONFIRMED appointment_requests row whose slot_date has already passed.
This is deliberately NOT:
  - PatientRecord.no_show_history (core/models.py) - a historical
    counter, unrelated to any specific appointment. A patient with
    no_show_history > 0 but no current missed appointment must get a
    normal recall email with NO portal link.
  - The CANCELLED/EXPIRED heuristic used by get_last_missed_slot_hint /
    core/slot_ranking.py for slot-preference ranking - unrelated to this
    feature, untouched by it.

Covers:
  1. A real past-dated CONFIRMED appointment causes the case's reminder
     to be composed as message_type="no_show".
  2. The orchestrator passes "no_show" (not "initial"/"urgent") to the
     composer for such a case.
  3. The resulting message contains that patient's own Portal URL.
  4. Two different NO_SHOW patients get two different, correctly
     attributed URLs.
  5. A patient with historical no_show_history > 0 but NO current missed
     appointment gets a normal message with NO portal link.
  6. initial/reminder/urgent message types remain link-free (regression
     guard for the existing gating in agent/notifications.py).

Does not touch SMTP/SES, Patient Portal routes, Calendar Unification
architecture, or the Patient Master Database - only the SEND_REMINDER
message_type selection in agent/orchestrator.py, plus the two narrowly
scoped read-only helpers it calls
(SchedulingDatabase.get_overdue_confirmed_appointment_for_patient,
SchedulingDatabaseCalendarAdapter.has_missed_appointment).
"""

from __future__ import annotations

import contextlib
import io
from datetime import date, datetime, timedelta, timezone

import pytest

from agent.orchestrator import FollowUpAgentOrchestrator
from core.config import ClinicPolicyConfig
from core.data_access import MockCalendarIntegration, MockPatientDataStore
from core.models import ContactChannel, PatientRecord
from core.scheduling_calendar_adapter import SchedulingDatabaseCalendarAdapter
from scheduling.calendar_service import CalendarService
from scheduling.database import SchedulingDatabase
from web.patient_portal_auth import PatientAccessTokenStore, build_portal_link_provider


@pytest.fixture
def scheduling_db(tmp_path):
    db_path = str(tmp_path / "no_show_wiring_test.db")
    return SchedulingDatabase(db_path)


@pytest.fixture
def calendar_service(scheduling_db):
    return CalendarService(scheduling_db)


@pytest.fixture
def calendar(scheduling_db):
    return SchedulingDatabaseCalendarAdapter(scheduling_db, requested_by="patient_portal")


@pytest.fixture
def token_store():
    return PatientAccessTokenStore()


@pytest.fixture
def agent_env(scheduling_db, calendar, token_store):
    data_store = MockPatientDataStore()
    policy = ClinicPolicyConfig()
    portal_link_provider = build_portal_link_provider(
        token_store, base_url="https://clinic.example.com"
    )
    orchestrator = FollowUpAgentOrchestrator.with_llm_decisions(
        data_store, calendar, policy, portal_link_provider=portal_link_provider,
    )
    return orchestrator, data_store


def make_patient(
    patient_id="P1",
    name="Sarah",
    days_since_visit=60,
    recall_interval_days=30,
    no_show_history=0,
):
    return PatientRecord(
        patient_id=patient_id,
        name=name,
        contact_info={ContactChannel.SMS: "+15550000000", ContactChannel.EMAIL: f"{patient_id.lower()}@example.com"},
        preferred_channel=ContactChannel.EMAIL,
        last_visit_date=date.today() - timedelta(days=days_since_visit),
        treatment_type="cleaning",
        recall_interval_days=recall_interval_days,
        no_show_history=no_show_history,
    )


def seed_case(orchestrator, data_store, patient_id="P1", name="Sarah", **patient_kwargs):
    data_store.add_patient(make_patient(patient_id, name, **patient_kwargs))
    with contextlib.redirect_stdout(io.StringIO()):
        orchestrator.run_daily_cycle(date.today())
    return orchestrator.get_case_by_patient_id(patient_id)


def _book_and_confirm_past_appointment(calendar_service, scheduling_db, patient_id, patient_name, days_ago):
    """
    Seed a REAL, current no-show: book a real slot on a past date through
    the normal CalendarService path (the same atomic capacity/validation
    check every booking in this system goes through), then approve it to
    CONFIRMED - exactly the state a booked-and-then-unattended appointment
    would be left in, since nothing in this system auto-transitions a
    CONFIRMED appointment when its date passes (see
    SchedulingDatabase.get_overdue_confirmed_appointment_for_patient's
    docstring).
    """
    probe = date.today() - timedelta(days=days_ago)
    slots = None
    for _ in range(10):
        candidate_slots = calendar_service.generate_slots_for_date(probe.isoformat())
        if candidate_slots:
            slots = candidate_slots
            break
        probe -= timedelta(days=1)  # walk further into the past, never into the future
    if not slots:
        raise AssertionError("no working day with slots found searching backward from days_ago")
    slot = slots[0]

    result = calendar_service.check_capacity_and_book(
        patient_id=patient_id,
        patient_name=patient_name,
        slot_datetime_utc=slot["datetime_utc"],
        slot_date=slot["date"],
        slot_session=slot["session"],
        slot_time=slot["time"],
        requested_by="staff",
        source="staff",
    )
    assert result["success"], result
    approved = scheduling_db.approve_appointment(result["appointment_id"], actor="staff")
    assert approved
    return result["appointment_id"]


def _extract_portal_url(message: str):
    for word in message.replace("\n", " ").split(" "):
        if "/patient/" in word:
            return word.rstrip(".,)")
    return None


# ---------------------------------------------------------------------------
# 1 & 2: a real missed appointment routes the reminder to message_type="no_show".
# ---------------------------------------------------------------------------

class TestRealMissedAppointmentTriggersNoShowMessageType:
    def test_a_past_confirmed_appointment_is_detected_as_a_missed_appointment(
        self, calendar, calendar_service, scheduling_db
    ):
        """Unit-level check of the new signal itself, independent of the
        orchestrator, before checking the end-to-end wiring below."""
        _book_and_confirm_past_appointment(
            calendar_service, scheduling_db, "P1", "Sarah", days_ago=3
        )
        assert calendar.has_missed_appointment("P1", datetime.now(timezone.utc)) is True

    def test_no_appointment_at_all_is_not_a_missed_appointment(self, calendar):
        assert calendar.has_missed_appointment("GHOST", datetime.now(timezone.utc)) is False

    def test_a_future_confirmed_appointment_is_not_a_missed_appointment(
        self, calendar, calendar_service, scheduling_db
    ):
        target_date = date.today() + timedelta(days=5)
        slots = calendar_service.generate_slots_for_date(target_date.isoformat())
        slot = slots[0] if slots else calendar_service.generate_slots_for_date(
            (target_date + timedelta(days=1)).isoformat()
        )[0]
        result = calendar_service.check_capacity_and_book(
            patient_id="P1", patient_name="Sarah",
            slot_datetime_utc=slot["datetime_utc"], slot_date=slot["date"],
            slot_session=slot["session"], slot_time=slot["time"],
            requested_by="staff", source="staff",
        )
        scheduling_db.approve_appointment(result["appointment_id"], actor="staff")

        assert calendar.has_missed_appointment("P1", datetime.now(timezone.utc)) is False

    def test_orchestrator_selects_no_show_message_type_for_a_real_missed_appointment(
        self, agent_env, calendar_service, scheduling_db
    ):
        orchestrator, data_store = agent_env
        _book_and_confirm_past_appointment(
            calendar_service, scheduling_db, "P1", "Sarah", days_ago=3
        )
        case = seed_case(orchestrator, data_store, "P1", "Sarah")

        composed = {}
        original_compose = orchestrator.message_composer.compose

        def spy_compose(case_arg, message_type):
            composed["message_type"] = message_type
            return original_compose(case_arg, message_type)

        orchestrator.message_composer.compose = spy_compose
        with contextlib.redirect_stdout(io.StringIO()):
            orchestrator.run_daily_cycle(date.today())

        assert composed.get("message_type") == "no_show"


# ---------------------------------------------------------------------------
# 3 & 4: the resulting email contains the correct, patient-specific link.
# ---------------------------------------------------------------------------

class TestNoShowEmailContainsTheCorrectPatientSpecificLink:
    def test_the_queued_no_show_message_contains_a_portal_link(
        self, agent_env, calendar_service, scheduling_db
    ):
        orchestrator, data_store = agent_env
        _book_and_confirm_past_appointment(
            calendar_service, scheduling_db, "P1", "Sarah", days_ago=3
        )
        with contextlib.redirect_stdout(io.StringIO()):
            data_store.add_patient(make_patient("P1", "Sarah"))
            orchestrator.run_daily_cycle(date.today())

        pending = orchestrator.get_pending_sends()
        assert len(pending) == 1
        message = pending[0]["message"]
        assert _extract_portal_url(message) is not None
        assert "Reschedule My Appointment" in message

    def test_two_different_no_show_patients_get_two_different_correct_links(
        self, agent_env, calendar_service, scheduling_db, token_store
    ):
        orchestrator, data_store = agent_env
        _book_and_confirm_past_appointment(
            calendar_service, scheduling_db, "P1", "Sarah", days_ago=3
        )
        _book_and_confirm_past_appointment(
            calendar_service, scheduling_db, "P2", "Blair", days_ago=5
        )

        with contextlib.redirect_stdout(io.StringIO()):
            data_store.add_patient(make_patient("P1", "Sarah"))
            data_store.add_patient(make_patient("P2", "Blair"))
            orchestrator.run_daily_cycle(date.today())

        pending = {item["patient_id"]: item for item in orchestrator.get_pending_sends()}
        assert set(pending.keys()) == {"P1", "P2"}

        link_p1 = _extract_portal_url(pending["P1"]["message"])
        link_p2 = _extract_portal_url(pending["P2"]["message"])

        assert link_p1 is not None and link_p2 is not None
        assert link_p1 != link_p2
        assert token_store.resolve_token(link_p1.rsplit("/", 1)[-1]) == "P1"
        assert token_store.resolve_token(link_p2.rsplit("/", 1)[-1]) == "P2"


# ---------------------------------------------------------------------------
# 5: historical no_show_history alone must NOT trigger the portal link.
# ---------------------------------------------------------------------------

class TestHistoricalNoShowHistoryAloneDoesNotTriggerTheLink:
    def test_high_no_show_history_with_no_current_missed_appointment_is_a_normal_recall(
        self, agent_env
    ):
        """The critical rule from the task: no_show_history > 0 is
        historical information and must NEVER by itself cause a normal
        recall to be composed as message_type="no_show"."""
        orchestrator, data_store = agent_env
        case = seed_case(
            orchestrator, data_store, "P1", "Sarah", no_show_history=5
        )
        assert case is not None

        composed = {}
        original_compose = orchestrator.message_composer.compose

        def spy_compose(case_arg, message_type):
            composed["message_type"] = message_type
            return original_compose(case_arg, message_type)

        orchestrator.message_composer.compose = spy_compose
        with contextlib.redirect_stdout(io.StringIO()):
            orchestrator.run_daily_cycle(date.today())

        assert composed.get("message_type") in ("initial", "urgent")
        assert composed.get("message_type") != "no_show"

        pending = orchestrator.get_pending_sends()
        assert len(pending) == 1
        assert _extract_portal_url(pending[0]["message"]) is None


# ---------------------------------------------------------------------------
# 6: initial / reminder / urgent stay link-free (composer-level regression
# guard - see also tests/test_portal_link_in_reminders.py, which owns the
# full byte-for-byte comparison; this is a narrower sanity check scoped to
# this feature's own test file).
# ---------------------------------------------------------------------------

class TestNonNoShowMessageTypesRemainWithoutAPortalLink:
    @pytest.mark.parametrize("message_type", ["initial", "reminder", "urgent"])
    def test_message_type_has_no_portal_link(self, agent_env, message_type):
        orchestrator, data_store = agent_env
        case = seed_case(orchestrator, data_store, "P1", "Sarah")

        message = orchestrator.message_composer.compose(case, message_type)
        assert _extract_portal_url(message) is None
        assert "Reschedule My Appointment" not in message


# ---------------------------------------------------------------------------
# The fallback path: calendars without has_missed_appointment (e.g.
# MockCalendarIntegration) must keep working exactly as before this
# feature - the getattr guard in agent/orchestrator.py is the mechanism.
# ---------------------------------------------------------------------------

class TestCalendarsWithoutMissedAppointmentSupportAreUnaffected:
    def test_mock_calendar_integration_still_selects_urgency_based_message_type(self):
        data_store = MockPatientDataStore()
        calendar = MockCalendarIntegration()
        policy = ClinicPolicyConfig()
        orchestrator = FollowUpAgentOrchestrator.with_llm_decisions(
            data_store, calendar, policy
        )
        data_store.add_patient(make_patient("P1", "Sarah"))
        with contextlib.redirect_stdout(io.StringIO()):
            orchestrator.run_daily_cycle(date.today())

        pending = orchestrator.get_pending_sends()
        assert len(pending) == 1
        assert _extract_portal_url(pending[0]["message"]) is None

# ---------------------------------------------------------------------------
# The outreach queue's own "context" must classify a no-show follow-up as
# such, not generically as "reminder" - see agent/orchestrator.py's
# SEND_REMINDER branch, which now queues with context=message_type
# instead of the previous hardcoded context="reminder". This is the
# outreach classification the staff outreach page (and any other
# consumer of get_pending_sends()) relies on to tell a no-show follow-up
# apart from a routine recall/urgent reminder.
# ---------------------------------------------------------------------------

class TestOutreachQueueClassifiesNoShowByContext:
    def test_a_real_no_show_is_queued_with_no_show_context_body_and_portal_link(
        self, agent_env, calendar_service, scheduling_db
    ):
        orchestrator, data_store = agent_env
        _book_and_confirm_past_appointment(
            calendar_service, scheduling_db, "P1", "Sarah", days_ago=3
        )
        with contextlib.redirect_stdout(io.StringIO()):
            data_store.add_patient(make_patient("P1", "Sarah"))
            orchestrator.run_daily_cycle(date.today())

        pending = orchestrator.get_pending_sends()
        assert len(pending) == 1
        item = pending[0]

        # Outreach classification: context must be "no_show", not the
        # generic "reminder" label.
        assert item["context"] == "no_show"

        # NO_SHOW message body (agent/notifications.py's compose()).
        assert "missed your" in item["message"].lower()

        # Patient-specific portal link, issued through the real,
        # production portal-link machinery (web/patient_portal_auth.py's
        # build_portal_link_provider), not a stub.
        portal_url = _extract_portal_url(item["message"])
        assert portal_url is not None
        assert "/patient/" in portal_url
        assert "Reschedule My Appointment" in item["message"]

    def test_normal_overdue_outreach_is_not_classified_as_no_show(self, agent_env):
        """A patient with no missed appointment at all must be queued
        under their real message_type ("initial"/"urgent"), never
        "no_show" - the classification tracks the actual signal, it does
        not default to no_show."""
        orchestrator, data_store = agent_env
        case = seed_case(orchestrator, data_store, "P1", "Sarah")
        assert case is not None

        pending = orchestrator.get_pending_sends()
        assert len(pending) == 1
        item = pending[0]

        assert item["context"] != "no_show"
        assert item["context"] in ("initial", "urgent")
        assert _extract_portal_url(item["message"]) is None
