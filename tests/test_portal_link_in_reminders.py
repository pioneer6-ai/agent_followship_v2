"""
Tests for embedding the existing Patient Portal link in NO_SHOW follow-up
messages ONLY.

This feature reuses the SAME token-issuing logic the staff-facing
``POST /api/patients/<id>/portal-link`` route already used
(web/patient_portal_auth.py's `PatientAccessTokenStore.issue_token`) via
one new shared function, `get_patient_portal_path`, and one small adapter,
`build_portal_link_provider`, that turns it into a plain
``patient_id -> URL`` callable. `agent/notifications.py`'s
`MessageComposerAgent` calls that callable directly - it does NOT import
Flask, `web.app`, or make any HTTP request back into the running app.

Scope of the link itself: `MessageComposerAgent.compose(case, message_type)`
only embeds the portal link when ``message_type == "no_show"`` AND a
``portal_link_provider`` is configured - having a provider configured is
necessary but NOT sufficient on its own. Every other message_type
("initial", "reminder", "urgent") must produce byte-for-byte the same
message as before this feature existed, even with a provider configured.

Covers:
  - The shared portal-link function itself: same patient -> same link
    (idempotent), different patients -> different links.
  - MessageComposerAgent embedding the link, the 7-day-window sentence,
    and the "remind me next week" mention ONLY for message_type="no_show".
  - "initial"/"reminder"/"urgent" never embed a link, even with a provider
    configured - this is the actual scope fix this file's second half
    tests: an earlier version of this feature added the link based only
    on `portal_link_provider is not None`, leaking it into every message
    type. That regression must not reappear.
  - Composing with no `portal_link_provider` at all still works exactly
    as before this feature (backward compatibility for every existing
    caller/test that doesn't pass one).
  - The staff-facing `/api/patients/<id>/portal-link` route and the email
    composer, called for the SAME patient, produce the IDENTICAL link -
    proving there is exactly one token system, not two.

Does not touch SMTP/SES configuration and does not send any real email -
these tests only exercise message *composition*, via the offline
PrintDeliveryBackend/ScriptedBackend paths already used elsewhere in the
test suite.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.models import CaseStatus, ContactChannel, FollowUpCase, PatientRecord, UrgencyLevel
from agent.notifications import MessageComposerAgent
from web.patient_portal_auth import (
    PatientAccessTokenStore,
    build_portal_link_provider,
    get_patient_portal_path,
)


def make_case(patient_id="P1", name="Test Patient", urgency=UrgencyLevel.MEDIUM) -> FollowUpCase:
    patient = PatientRecord(
        patient_id=patient_id,
        name=name,
        contact_info={ContactChannel.EMAIL: f"{patient_id.lower()}@example.com"},
        preferred_channel=ContactChannel.EMAIL,
        last_visit_date=date(2026, 1, 1),
        treatment_type="cleaning",
        recall_interval_days=180,
    )
    return FollowUpCase(
        patient=patient,
        days_overdue=30,
        urgency=urgency,
        reason="routine recall",
        status=CaseStatus.PENDING,
    )


# ---------------------------------------------------------------------------
# The shared portal-link function itself.
# ---------------------------------------------------------------------------

class TestSharedPortalLinkFunction:
    def test_same_patient_id_returns_the_same_path_every_time(self):
        store = PatientAccessTokenStore()
        first = get_patient_portal_path(store, "P100")
        second = get_patient_portal_path(store, "P100")
        assert first == second

    def test_different_patients_get_different_paths(self):
        store = PatientAccessTokenStore()
        path_a = get_patient_portal_path(store, "P100")
        path_b = get_patient_portal_path(store, "P200")
        assert path_a != path_b

    def test_path_format_matches_the_existing_portal_route(self):
        store = PatientAccessTokenStore()
        path = get_patient_portal_path(store, "P100")
        assert path.startswith("/patient/")
        token = path.rsplit("/", 1)[-1]
        assert store.resolve_token(token) == "P100"

    def test_provider_prepends_the_configured_base_url(self):
        store = PatientAccessTokenStore()
        provider = build_portal_link_provider(store, base_url="https://clinic.example.com")
        url = provider("P100")
        assert url.startswith("https://clinic.example.com/patient/")

    def test_provider_with_no_base_url_stays_relative(self):
        store = PatientAccessTokenStore()
        provider = build_portal_link_provider(store)
        url = provider("P100")
        assert url.startswith("/patient/")
        assert "://" not in url

    def test_provider_and_direct_path_helper_agree_for_the_same_patient(self):
        """The provider is just an adapter over get_patient_portal_path -
        confirm they never diverge for the same store/patient."""
        store = PatientAccessTokenStore()
        direct_path = get_patient_portal_path(store, "P100")
        provider = build_portal_link_provider(store)
        assert provider("P100") == direct_path


# ---------------------------------------------------------------------------
# MessageComposerAgent embedding the link into a reminder.
# ---------------------------------------------------------------------------

class TestNoShowMessagesEmbedThePortalLink:
    """Requirement 1 & 4: NO_SHOW messages contain the link, and different
    NO_SHOW patients receive different, correctly-attributed links."""

    def test_a_no_show_message_contains_the_portal_link(self):
        store = PatientAccessTokenStore()
        provider = build_portal_link_provider(store, base_url="https://clinic.example.com")
        composer = MessageComposerAgent(portal_link_provider=provider)

        message = composer.compose(make_case("P100", "Alex Chen"), "no_show")

        assert _extract_portal_url(message) is not None
        assert "Reschedule My Appointment" in message

    def test_patient_a_and_patient_b_receive_different_links_in_their_no_show_messages(self):
        store = PatientAccessTokenStore()
        provider = build_portal_link_provider(store, base_url="https://clinic.example.com")
        composer = MessageComposerAgent(portal_link_provider=provider)

        message_a = composer.compose(make_case("P100", "Alex Chen"), "no_show")
        message_b = composer.compose(make_case("P200", "Blair Doe"), "no_show")

        link_a = _extract_portal_url(message_a)
        link_b = _extract_portal_url(message_b)

        assert link_a is not None and link_b is not None
        assert link_a != link_b
        # Each patient's OWN link resolves back to THAT patient, not the other.
        assert store.resolve_token(link_a.rsplit("/", 1)[-1]) == "P100"
        assert store.resolve_token(link_b.rsplit("/", 1)[-1]) == "P200"

    def test_the_same_no_show_patient_gets_the_same_link_across_multiple_messages(self):
        store = PatientAccessTokenStore()
        provider = build_portal_link_provider(store)
        composer = MessageComposerAgent(portal_link_provider=provider)

        first = composer.compose(make_case("P100"), "no_show")
        second = composer.compose(make_case("P100"), "no_show")

        assert _extract_portal_url(first) == _extract_portal_url(second)

    def test_no_show_message_mentions_the_seven_day_availability_window(self):
        store = PatientAccessTokenStore()
        provider = build_portal_link_provider(store)
        composer = MessageComposerAgent(portal_link_provider=provider, booking_window_days=7)

        message = composer.compose(make_case("P100"), "no_show")
        assert "7 days" in message

    def test_no_show_message_mentions_a_different_configured_window(self):
        store = PatientAccessTokenStore()
        provider = build_portal_link_provider(store)
        composer = MessageComposerAgent(portal_link_provider=provider, booking_window_days=14)

        message = composer.compose(make_case("P100"), "no_show")
        assert "14 days" in message

    def test_no_show_message_mentions_the_remind_me_next_week_option(self):
        store = PatientAccessTokenStore()
        provider = build_portal_link_provider(store)
        composer = MessageComposerAgent(portal_link_provider=provider)

        message = composer.compose(make_case("P100"), "no_show")
        assert "remind me next week" in message.lower()

    def test_no_show_message_still_has_a_personalized_greeting_and_signoff(self):
        store = PatientAccessTokenStore()
        provider = build_portal_link_provider(store)
        composer = MessageComposerAgent(portal_link_provider=provider)

        message = composer.compose(make_case("P100", "Jordan Lee"), "no_show")
        assert "Jordan Lee" in message
        assert "Your Dental Care Team" in message

    def test_no_show_message_has_no_link_when_no_provider_is_configured(self):
        """A no_show message with NO provider configured must not invent
        a link out of nowhere - the provider is still required."""
        composer = MessageComposerAgent()
        message = composer.compose(make_case("P100"), "no_show")
        assert _extract_portal_url(message) is None
        assert "Reschedule My Appointment" not in message


class TestNonNoShowMessageTypesNeverEmbedALinkEvenWithAProviderConfigured:
    """Requirements 2 & 3: RECALL ("initial"/"reminder") and other
    follow-up types ("urgent") must never contain the portal link, and
    must be byte-for-byte identical to the pre-portal-link-feature
    template, even when a portal_link_provider IS configured - having a
    provider available is necessary but not sufficient; message_type must
    also be "no_show"."""

    @pytest.fixture
    def composer_with_provider(self):
        store = PatientAccessTokenStore()
        provider = build_portal_link_provider(store, base_url="https://clinic.example.com")
        return MessageComposerAgent(portal_link_provider=provider)

    def test_recall_initial_message_has_no_link(self, composer_with_provider):
        message = composer_with_provider.compose(make_case("P100"), "initial")
        assert _extract_portal_url(message) is None
        assert "Reschedule My Appointment" not in message
        assert "remind me next week" not in message.lower()

    def test_recall_reminder_message_has_no_link(self, composer_with_provider):
        message = composer_with_provider.compose(make_case("P100"), "reminder")
        assert _extract_portal_url(message) is None
        assert "Reschedule My Appointment" not in message

    def test_urgent_message_has_no_link(self, composer_with_provider):
        message = composer_with_provider.compose(make_case("P100"), "urgent")
        assert _extract_portal_url(message) is None
        assert "Reschedule My Appointment" not in message

    @pytest.mark.parametrize("message_type", ["initial", "reminder", "urgent"])
    @pytest.mark.parametrize("urgency", [UrgencyLevel.LOW, UrgencyLevel.HIGH, UrgencyLevel.CRITICAL])
    def test_byte_for_byte_identical_to_the_pre_feature_template(
        self, composer_with_provider, message_type, urgency
    ):
        """The authoritative regression check: compare against a
        composer with NO provider at all (the original, unmodified
        behavior) for every message_type/urgency combination that must
        stay untouched by this feature."""
        no_provider_composer = MessageComposerAgent()

        case_for_with_provider = make_case("P100", "Alex Chen", urgency)
        case_for_without_provider = make_case("P100", "Alex Chen", urgency)

        with_provider_message = composer_with_provider.compose(case_for_with_provider, message_type)
        without_provider_message = no_provider_composer.compose(case_for_without_provider, message_type)

        assert with_provider_message == without_provider_message


class TestComposerWithoutAPortalLinkProviderIsUnchanged:
    """Backward compatibility: every existing caller/test that builds a
    MessageComposerAgent() with no portal_link_provider must keep getting
    exactly the same message as before this feature existed, for every
    message_type including "no_show"."""

    @pytest.mark.parametrize("message_type", ["initial", "reminder", "urgent", "no_show"])
    def test_no_link_appears_when_no_provider_is_configured(self, message_type):
        composer = MessageComposerAgent()
        message = composer.compose(make_case("P100"), message_type)
        assert "http" not in message
        assert "/patient/" not in message
        assert "remind me next week" not in message.lower()

    def test_default_construction_matches_the_pre_feature_message_shape(self):
        composer = MessageComposerAgent()
        message = composer.compose(make_case("P100", "Jordan Lee"), "initial")
        assert message.startswith("Hello Jordan Lee,")
        assert message.endswith("Best regards,\nYour Dental Care Team")


def _extract_portal_url(message: str) -> str | None:
    for word in message.replace("\n", " ").split(" "):
        if "/patient/" in word:
            return word.rstrip(".,)")
    return None


# ---------------------------------------------------------------------------
# End-to-end: the staff route and the email composer share one token system.
# ---------------------------------------------------------------------------

from web.app import agent as app_agent, app, data_store, patient_portal_tokens, scheduling_db  # noqa: E402


def reset_state():
    app_agent.active_cases.clear()
    app_agent.undelivered.clear()
    patient_portal_tokens._tokens.clear()

    conn = scheduling_db.get_connection()
    try:
        conn.execute("DELETE FROM patients")
        conn.execute("DELETE FROM appointment_requests")
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM blocked_periods")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client():
    reset_state()
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client
    reset_state()


def make_patient_record(patient_id="P100", name="Alex Patient"):
    return PatientRecord(
        patient_id=patient_id,
        name=name,
        contact_info={ContactChannel.EMAIL: f"{patient_id.lower()}@example.com"},
        preferred_channel=ContactChannel.EMAIL,
        last_visit_date=date.today() - timedelta(days=60),
        treatment_type="cleaning",
        recall_interval_days=30,
    )


class TestStaffRouteAndEmailComposerShareOneTokenSystem:
    def test_the_link_the_route_issues_is_what_the_composer_would_embed(self, client):
        """The exact same production wiring (web.app.agent.message_composer)
        must produce the SAME link the staff dashboard's portal-link route
        returns for that patient - proving one shared implementation, not
        two independent token stores."""
        data_store.add_patient(make_patient_record("P100", "Alex Patient"))

        resp = client.post("/api/patients/P100/portal-link")
        assert resp.status_code == 200
        route_path = resp.get_json()["portal_url"]

        composer = app_agent.message_composer
        assert composer.portal_link_provider is not None
        composed_link = composer.portal_link_provider("P100")

        # The route returns a relative path; the composer's provider may
        # prepend a base_url, so compare on the path only.
        assert composed_link.endswith(route_path)

    def test_patient_a_and_patient_b_get_different_links_through_the_real_route(self, client):
        data_store.add_patient(make_patient_record("P100", "Alex Patient"))
        data_store.add_patient(make_patient_record("P200", "Blair Patient"))

        link_a = client.post("/api/patients/P100/portal-link").get_json()["portal_url"]
        link_b = client.post("/api/patients/P200/portal-link").get_json()["portal_url"]

        assert link_a != link_b

    def test_the_route_still_returns_a_working_token_for_the_portal_page(self, client):
        """Confirms the refactor to a shared function did not change the
        route's observable contract: the returned token must still
        resolve to a real portal page for THAT patient."""
        data_store.add_patient(make_patient_record("P100", "Alex Patient"))
        app_agent.run_daily_cycle(date.today())

        resp = client.post("/api/patients/P100/portal-link")
        token = resp.get_json()["access_token"]

        portal_resp = client.get(f"/patient/{token}")
        assert portal_resp.status_code == 200
        assert b"Alex Patient" in portal_resp.data
