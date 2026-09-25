"""
Regression tests for the upload-patient-list feature.

The question this answers: when a clinic uploads a patient list, does the agent
actually read it and does the main dashboard then show those patients? The
feature exists, but it was silently broken once -- ``web/app.py`` raised
``TypeError`` on import because ``with_llm_decisions`` injected a ``policy``
keyword that had already been passed positionally. Nothing caught it, because
nothing imported the web app.

These tests therefore cover three things:

1. ``web.app`` imports and constructs an orchestrator at all (the bug above).
2. The upload -> import -> dashboard round trip moves real patients into the
   data store.
3. An empty or absent file is refused rather than corrupting the store.
"""

import io
import json

import pytest

from web.app import agent, app, data_store, scheduling_db

PATIENT_LIST = [
    {
        "patient_id": "UP-1",
        "name": "Uploaded One",
        "contact_info": {"sms": "+6583536885"},
        "preferred_channel": "sms",
        "last_visit_date": "2023-01-15",
        "treatment_type": "cleaning",
    },
    {
        "patient_id": "UP-2",
        "name": "Uploaded Two",
        "contact_info": {"email": "two@example.com"},
        "preferred_channel": "email",
        "last_visit_date": "2023-02-20",
        "treatment_type": "filling",
    },
]


def empty_store():
    """
    Reset the patient store between tests.

    ``web.app``'s ``data_store`` is SqlitePatientDataStore, backed by the
    real (persistent) ``patients`` table in scheduling_db - the web app
    holds a single module-level instance, so tests must not inherit each
    other's rows. Deleting directly from the table is the SQLite-store
    equivalent of the old ``MockPatientDataStore._patients.clear()``.
    """
    conn = scheduling_db.get_connection()
    try:
        conn.execute("DELETE FROM patients")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client():
    """Flask test client, with the store emptied first so IDs do not collide."""
    empty_store()
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client
    empty_store()


def upload(client, body, filename="patients.csv", content_type="text/csv"):
    """POST a file to the upload endpoint (accepts a str or raw bytes body)."""
    payload = body.encode() if isinstance(body, str) else body
    return client.post(
        "/api/upload-patient-list",
        data={"file": (io.BytesIO(payload), filename, content_type)},
        content_type="multipart/form-data",
    )


def import_patients(client, patients):
    """POST parsed patients to the import endpoint."""
    return client.post(
        "/api/import-patients",
        data=json.dumps({"patients": patients}),
        content_type="application/json",
    )


class TestWebAppConstructs:
    def test_the_app_imports_without_raising(self):
        # The actual regression: a TypeError here meant the dashboard was dead.
        assert app is not None

    def test_the_orchestrator_is_wired_to_its_dependencies(self):
        assert agent.data_store is data_store

    def test_the_decision_engine_is_present(self):
        # The bug lived in the constructor that builds this, so assert it
        # exists rather than merely that the import stopped raising.
        assert agent.decision_engine is not None

    def test_a_positional_policy_is_accepted(self):
        # Locked in deliberately: web/app.py passes policy positionally.
        from agent.orchestrator import FollowUpAgentOrchestrator
        from core.config import ClinicPolicyConfig
        from core.data_access import MockCalendarIntegration, MockPatientDataStore

        orchestrator = FollowUpAgentOrchestrator.with_llm_decisions(
            MockPatientDataStore(), MockCalendarIntegration(), ClinicPolicyConfig()
        )
        assert orchestrator.policy is not None

    def test_a_keyword_policy_is_also_accepted(self):
        from agent.orchestrator import FollowUpAgentOrchestrator
        from core.config import ClinicPolicyConfig
        from core.data_access import MockCalendarIntegration, MockPatientDataStore

        orchestrator = FollowUpAgentOrchestrator.with_llm_decisions(
            MockPatientDataStore(),
            MockCalendarIntegration(),
            policy=ClinicPolicyConfig(),
        )
        assert orchestrator.policy is not None

    def test_the_dashboard_page_renders(self, client):
        response = client.get("/")
        assert response.status_code == 200


class TestUpload:
    def test_a_csv_upload_is_parsed(self, client):
        response = upload(
            client,
            "patient_id,name,phone\nUP-1,Uploaded One,+6583536885\n",
        )
        body = response.get_json()
        assert response.status_code == 200
        assert body["success"] is True
        assert body["count"] >= 1

    def test_an_upload_without_a_file_is_refused(self, client):
        response = client.post(
            "/api/upload-patient-list",
            data={},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
        assert response.get_json()["success"] is False

    def test_an_empty_filename_is_refused(self, client):
        response = upload(client, "", filename="")
        assert response.status_code == 400


class TestImportReachesTheDashboard:
    def test_imported_patients_land_in_the_data_store(self, client):
        before = len(data_store.get_all_active_patients())
        response = import_patients(client, PATIENT_LIST)
        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert body["imported_count"] == 2
        after = data_store.get_all_active_patients()
        assert len(after) == before + 2

    def test_the_status_endpoint_reports_the_new_total(self, client):
        import_patients(client, PATIENT_LIST)
        body = client.get("/api/status").get_json()
        assert body["total_patients"] == 2

    def test_the_dashboard_case_list_includes_the_imported_patients(self, client):
        import_patients(client, PATIENT_LIST)
        cases = client.get("/api/cases").get_json()
        assert isinstance(cases, list)
        names = {case["patient_name"] for case in cases}
        assert "Uploaded One" in names

    def test_the_full_upload_then_import_then_dashboard_round_trip(self, client):
        # The end-to-end path a clinic actually performs.
        uploaded = upload(
            client,
            "patient_id,name,phone\nUP-1,Uploaded One,+6583536885\n",
        ).get_json()
        assert uploaded["count"] == 1

        patients = uploaded["patients"]
        # Uploaded rows carry whatever the parser understood; give the record
        # what the importer needs if the parser omitted it.
        for patient in patients:
            patient.setdefault("name", "Uploaded One")
            patient.setdefault("contact_info", {"sms": "+6583536885"})
        imported = import_patients(client, patients).get_json()
        assert imported["success"] is True
        assert imported["imported_count"] == 1

        status = client.get("/api/status").get_json()
        assert status["total_patients"] == 1

        assert client.get("/api/cases").get_json()

    def test_the_import_triggers_a_daily_cycle(self, client):
        # Importing kicks off the agent, so the dashboard reflects the agent's
        # decisions about the new patients, not merely a longer list.
        import_patients(client, PATIENT_LIST)
        status = client.get("/api/status").get_json()
        assert sum(status["cases_by_urgency"].values()) == 2

    def test_the_agent_acted_on_the_uploaded_patients(self, client):
        # The point of the feature: the agent reads the uploaded list and works
        # it, so the audit trail must contain decisions for those patients.
        import_patients(client, PATIENT_LIST)
        audit = client.get("/api/status").get_json()["audit_summary"]
        assert audit["total_decisions"] > 0

    def test_a_duplicate_upload_is_merged_not_re_inserted(self, client):
        # Re-importing the exact same list (same patient_id) must MERGE
        # into the existing rows, not insert duplicates and not error out.
        import_patients(client, PATIENT_LIST)
        second = import_patients(client, PATIENT_LIST).get_json()
        assert second["inserted_count"] == 0
        assert second["updated_count"] == 2
        assert second["imported_count"] == 2
        assert len(data_store.get_all_active_patients()) == 2

    def test_an_import_without_patients_is_refused(self, client):
        response = client.post(
            "/api/import-patients", data=json.dumps({}), content_type="application/json"
        )
        assert response.status_code == 400

    def test_a_patient_with_no_contact_info_still_imports(self, client):
        # The importer substitutes a placeholder rather than rejecting the row,
        # which is what keeps a rough spreadsheet usable.
        response = import_patients(
            client,
            [{"patient_id": "UP-9", "name": "No Contact", "contact_info": {}}],
        )
        assert response.get_json()["imported_count"] == 1

    def test_a_second_upload_with_the_same_email_but_a_different_id_merges(self, client):
        # The Patient Master Database: same email (case/whitespace
        # insensitive) is the SAME patient even under a different
        # patient_id - it must be merged, not inserted as a duplicate.
        import_patients(
            client,
            [{
                "patient_id": "DUP-A",
                "name": "Original Name",
                "contact_info": {"email": "dup@example.com"},
                "preferred_channel": "email",
                "last_visit_date": "2023-01-15",
                "treatment_type": "cleaning",
            }],
        )
        second = import_patients(
            client,
            [{
                "patient_id": "DUP-B",
                "name": "Updated Name",
                "contact_info": {"email": "  DUP@EXAMPLE.COM  "},
                "preferred_channel": "email",
                "last_visit_date": "2023-06-01",
                "treatment_type": "checkup",
            }],
        ).get_json()

        assert second["inserted_count"] == 0
        assert second["updated_count"] == 1
        assert second["updated_patients"][0]["matched_by"] == "email"

        patients = {p.patient_id: p for p in data_store.get_all_active_patients()}
        assert len(patients) == 1
        assert patients["DUP-A"].name == "Updated Name"

    def test_imported_patient_survives_reopening_the_apps_own_scheduling_db_file(self, client):
        """Not an isolated tmp_path fixture - this reopens the EXACT
        scheduling.db file path web.app itself uses (see
        web.app.scheduling_db.db_path), proving the fix actually applies
        to the real running application's database, not just a test
        double of it."""
        import_patients(
            client,
            [{
                "patient_id": "RESTART-1",
                "name": "Restart Survivor",
                "contact_info": {"email": "restart@example.com"},
                "preferred_channel": "email",
                "last_visit_date": "2023-01-15",
                "treatment_type": "cleaning",
            }],
        )

        from scheduling.database import SchedulingDatabase
        from core.data_access import SqlitePatientDataStore

        reopened = SqlitePatientDataStore(SchedulingDatabase(scheduling_db.db_path))
        patient = reopened.get_patient_by_id("RESTART-1")
        assert patient is not None
        assert patient.name == "Restart Survivor"

    def test_a_second_upload_with_a_blank_email_does_not_erase_the_existing_one(self, client):
        import_patients(
            client,
            [{
                "patient_id": "KEEP-1",
                "name": "Has Email",
                "contact_info": {"email": "keep@example.com"},
                "preferred_channel": "email",
                "last_visit_date": "2023-01-15",
                "treatment_type": "cleaning",
            }],
        )
        import_patients(
            client,
            [{
                "patient_id": "KEEP-1",
                "name": "Has Email",
                "contact_info": {},
                "last_visit_date": "2023-06-01",
                "treatment_type": "checkup",
            }],
        )

        patient = data_store.get_patient_by_id("KEEP-1")
        from core.models import ContactChannel

        assert patient.contact_info[ContactChannel.EMAIL] == "keep@example.com"
        # The genuinely new field (treatment_type) still took effect.
        assert patient.treatment_type == "checkup"


def _xlsx_bytes(rows, header=None):
    """Build a real ``.xlsx`` workbook in memory."""
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    if header is not None:
        sheet.append(header)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class TestExcelUpload:
    """
    A clinic is far more likely to keep its recall list in Excel than in CSV, so
    an ``.xlsx`` upload must genuinely work.

    It previously did not: ``_parse_excel`` was a stub that forwarded the bytes
    to the CSV reader, so a real workbook (a ZIP container) failed with a
    ``UnicodeDecodeError`` while the README advertised Excel support.
    """

    HEADER = ["Patient ID", "Name", "Phone", "Email", "Last Visit", "Recall Interval"]
    ROW = [2001, "Excel Patient", 91234567, "excel@example.com", "2024-01-15", 180]

    def test_an_xlsx_upload_is_parsed(self, client):
        response = upload(
            client,
            _xlsx_bytes([self.ROW], self.HEADER),
            filename="patients.xlsx",
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["success"] is True
        assert payload["count"] == 1
        patient = payload["patients"][0]
        assert patient["patient_id"] == "2001"
        assert patient["name"] == "Excel Patient"
        assert patient["contact_info"]["email"] == "excel@example.com"

    def test_an_xlsx_and_a_csv_of_the_same_data_agree(self, client):
        from utils.llm_parser import LLMPatientParser

        parser = LLMPatientParser(use_llm=False)
        csv_bytes = (
            b"Patient ID,Name,Phone,Email,Last Visit,Recall Interval\n"
            b"2001,Excel Patient,91234567,excel@example.com,2024-01-15,180\n"
        )
        assert parser.parse_file(csv_bytes, "p.csv") == parser.parse_file(
            _xlsx_bytes([self.ROW], self.HEADER), "p.xlsx"
        )

    def test_a_date_cell_keeps_the_date_only(self, client):
        """Excel hands back a datetime, which the date parser could not read."""
        import datetime

        from utils.llm_parser import LLMPatientParser

        parser = LLMPatientParser(use_llm=False)
        rows = parser.parse_file(
            _xlsx_bytes(
                [[2002, "Dated", 91234567, "d@example.com",
                  datetime.date(2024, 3, 2), 180]],
                self.HEADER,
            ),
            "p.xlsx",
        )
        assert rows[0]["last_visit_date"] == "2024-03-02"

    def test_blank_rows_are_skipped(self, client):
        from utils.llm_parser import LLMPatientParser

        parser = LLMPatientParser(use_llm=False)
        rows = parser.parse_file(
            _xlsx_bytes([self.ROW, [None] * 6], self.HEADER), "p.xlsx"
        )
        assert len(rows) == 1

    def test_a_legacy_xls_upload_is_refused_with_advice(self, client):
        response = upload(
            client, b"\xd0\xcf\x11\xe0legacy", filename="patients.xls",
            content_type="application/vnd.ms-excel",
        )
        assert response.status_code == 500
        error = response.get_json()["error"]
        assert ".xls" in error and ".xlsx" in error

    def test_a_corrupt_xlsx_reports_could_not_read(self, client):
        response = upload(
            client, b"not really a workbook", filename="patients.xlsx",
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        assert response.status_code == 500
        assert "Could not read the workbook" in response.get_json()["error"]

    def test_a_missing_openpyxl_install_explains_itself(self, monkeypatch):
        """Absent openpyxl must degrade to advice, not a decode error."""
        import builtins

        from utils.llm_parser import LLMPatientParser

        real_import = builtins.__import__

        def without_openpyxl(name, *args, **kwargs):
            if name == "openpyxl":
                raise ImportError("simulated: openpyxl is not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", without_openpyxl)
        with pytest.raises(ValueError) as excinfo:
            LLMPatientParser(use_llm=False).parse_file(b"PK\x03\x04", "p.xlsx")
        assert "pip install openpyxl" in str(excinfo.value)

    def test_csv_json_and_text_uploads_still_work(self, client):
        """The Excel work must not disturb the formats that already ran."""
        assert upload(client, b"Patient ID,Name\n3001,Csv Patient\n").status_code == 200
        json_rows = json.dumps(
            [{"patient_id": "3002", "name": "Json Patient"}]
        ).encode()
        assert upload(
            client, json_rows, filename="p.json", content_type="application/json"
        ).status_code == 200
        assert upload(
            client, b"Name: Text Patient\nPhone: 91234567\n",
            filename="p.txt", content_type="text/plain",
        ).status_code == 200
