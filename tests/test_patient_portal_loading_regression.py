"""
Regression tests for a real Patient Portal bug found during manual Safari
testing: the portal loaded successfully (HTTP 200, correct patient name),
but the status pill and appointment section stayed stuck on their static
"Loading..." / "Loading available times..." placeholders forever, with no
date/time buttons ever appearing.

Root cause: commit 2319f9e ("Improve error handling for noSlotBtn click
event") deleted the `noSlotBtn.addEventListener('click', async () => {`
wrapper line while editing the handler's body, but left the body's
`await fetch(...)` calls and the closing `});` in place. That left
`await` sitting at the top level of a classic (non-module) <script> block,
which is a SyntaxError ("await is only valid in async functions and the
top level bodies of modules") in every real browser. A <script> tag that
fails to parse never executes ANY of its code - so `loadStatus()` and
`loadSlots()`, called at the very bottom of the same script, never ran,
even though both backend APIs (/status, /available-slots) were already
returning correct HTTP 200 responses with the correct schema the whole
time. This is a frontend-only failure mode: it is invisible to any test
that only checks the JSON API responses (as test_patient_portal.py and
test_patient_portal_slot_times.py already do) - a pytest suite with 100%
green API tests can still ship a portal that never renders anything,
because nothing previously checked that the served <script> block is
syntactically valid JavaScript.

These tests close that gap: they extract the actual <script> body Flask
serves (after Jinja rendering, exactly what a browser receives) and
verify it parses as valid JavaScript, in addition to re-confirming (via
the ordinary Flask test client) that the underlying status/available-slots
APIs themselves return successfully with the schema the frontend expects.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import date, timedelta

import pytest

from core.models import ContactChannel, PatientRecord

from web.app import agent, app, data_store, patient_portal_tokens, scheduling_db


def reset_state():
    agent.active_cases.clear()
    agent.undelivered.clear()
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


def make_patient(patient_id="LOAD1", name="Sarah Johnson", days_since_visit=60):
    return PatientRecord(
        patient_id=patient_id,
        name=name,
        contact_info={
            ContactChannel.SMS: "+15550000000",
            ContactChannel.EMAIL: "sarah@example.com",
        },
        preferred_channel=ContactChannel.SMS,
        last_visit_date=date.today() - timedelta(days=days_since_visit),
        treatment_type="cleaning",
        recall_interval_days=30,
    )


def seed_patient_with_case(patient_id="LOAD1", name="Sarah Johnson"):
    data_store.add_patient(make_patient(patient_id, name))
    agent.run_daily_cycle(date.today())
    return agent.get_case_by_patient_id(patient_id)


def issue_token(client, patient_id):
    resp = client.post(f"/api/patients/{patient_id}/portal-link")
    assert resp.status_code == 200
    return resp.get_json()["access_token"]


def extract_script_body(html: str) -> str:
    match = re.search(r"<script>(.*)</script>", html, re.DOTALL)
    assert match, "expected exactly one inline <script> block in the portal page"
    return match.group(1)


# ---------------------------------------------------------------------------
# The actual regression: the served page's JavaScript must be syntactically
# valid, or the browser never runs any of it (this is what caused the
# "stuck on Loading..." symptom - not a backend failure).
# ---------------------------------------------------------------------------

class TestPortalScriptIsSyntacticallyValidJavaScript:
    def test_rendered_script_block_has_no_syntax_error(self, client):
        """
        Direct regression test for the noSlotBtn `await`-outside-async-
        function bug. Uses Node (if available) to parse the EXACT script
        Flask serves after Jinja rendering - not the raw template file, so
        this also catches any future templating mistake that only shows up
        in the rendered output.

        Skips (rather than failing) when `node` is not installed, since a
        missing JS runtime is an environment gap, not a code regression -
        but the test still asserts the DELETED-WRAPPER regression pattern
        never reappears (see test_no_dangling_await_outside_a_function
        below), which needs no JS runtime at all.
        """
        node_path = shutil.which("node")
        if node_path is None:
            pytest.skip("node is not installed in this environment")

        seed_patient_with_case()
        token = issue_token(client, "LOAD1")
        resp = client.get(f"/patient/{token}")
        assert resp.status_code == 200

        script = extract_script_body(resp.get_data(as_text=True))

        result = subprocess.run(
            [node_path, "--check", "/dev/stdin"],
            input=script,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"Patient Portal's rendered <script> block has a JavaScript "
            f"syntax error, which means NONE of it executes in a real "
            f"browser (this is exactly how the 'stuck on Loading...' bug "
            f"happened):\n{result.stderr}"
        )

    def test_no_show_slot_button_handler_still_has_its_async_wrapper(self):
        """
        Environment-independent version of the same regression, needing no
        JS runtime: directly re-detects THIS bug's exact shape (commit
        2319f9e deleted the `noSlotBtn.addEventListener('click', async ()
        => {` line while editing the handler body, leaving `await
        fetch(...)` and a dangling `});` behind at the top level of the
        <script> block - a SyntaxError in every real browser). The
        Node-based test above is the general-purpose guard for any other
        future syntax mistake; this one pins down the specific historical
        regression so it can never silently reappear even without Node
        installed.
        """
        template_path = "web/templates/patient_portal.html"
        with open(template_path) as f:
            html = f.read()
        script = extract_script_body(html)

        assert "noSlotBtn.addEventListener('click', async () => {" in script, (
            "The 'None of these times work' button's click handler is "
            "missing its `addEventListener('click', async () => { ... })` "
            "wrapper - this exact regression previously left `await "
            "fetch(...)` at the top level of the <script> block, which is "
            "a JavaScript SyntaxError that breaks the ENTIRE script (this "
            "is why the portal got stuck on 'Loading...' - loadStatus()/"
            "loadSlots() never ran)."
        )

        # The handler body (fetch -> json -> feedback -> status pill) must
        # appear strictly after that opening line, not before it - guards
        # against the wrapper being re-added in the wrong place.
        wrapper_index = script.index("noSlotBtn.addEventListener('click', async () => {")
        fetch_index = script.index("await fetch(`${BASE}/no-suitable-slot`", wrapper_index)
        assert fetch_index > wrapper_index


# ---------------------------------------------------------------------------
# Backend contract the frontend depends on: confirms /status and
# /available-slots themselves were never the problem, and stay correct.
# ---------------------------------------------------------------------------

class TestPortalBackendAPIsSucceedIndependentlyOfFrontend:
    def test_status_api_returns_success_and_a_real_status_value(self, client):
        seed_patient_with_case()
        token = issue_token(client, "LOAD1")

        resp = client.get(f"/api/patient-portal/{token}/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert isinstance(data["status"], str) and data["status"]
        assert data["patient_name"] == "Sarah Johnson"

    def test_available_slots_api_returns_the_schema_the_frontend_expects(self, client):
        seed_patient_with_case()
        token = issue_token(client, "LOAD1")

        resp = client.get(f"/api/patient-portal/{token}/available-slots")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

        # The frontend's loadSlots() branches on `Array.isArray(data.dates)`
        # - if this key is ever renamed/removed without updating the JS,
        # the portal silently falls back to the legacy date-only branch.
        assert isinstance(data.get("dates"), list)
        assert isinstance(data.get("preferred_dates"), list)
        assert isinstance(data.get("other_dates"), list)
        assert isinstance(data.get("used_no_show_preference"), bool)
        assert len(data["dates"]) > 0

        first_entry = data["dates"][0]
        assert "date" in first_entry
        assert isinstance(first_entry["times"], list) and len(first_entry["times"]) > 0
        for time_entry in first_entry["times"]:
            assert {"session", "time", "label"} <= time_entry.keys()

    def test_portal_page_still_loads_the_patient_name_even_when_script_would_fail(self, client):
        """
        The server-rendered HTML (patient name, status pill markup) must
        be present regardless of any client-side JS outcome - the initial
        page load itself was never broken by this bug, only the
        subsequent fetch-driven population of the page.
        """
        seed_patient_with_case()
        token = issue_token(client, "LOAD1")

        resp = client.get(f"/patient/{token}")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Sarah Johnson" in html
        assert 'id="status-pill"' in html
        assert 'id="slot-list"' in html
