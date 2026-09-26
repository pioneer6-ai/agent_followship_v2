#!/usr/bin/env python3
"""
Capture the running dashboard as deployment evidence.

Evidence that a system is deployed should show the system, not just describe it.
This script starts the real Flask application against the same in-process
orchestrator the operator uses, waits until it answers, drives a browser to it
with headless Chrome, and stores the resulting screenshot plus the live JSON the
dashboard itself consumes.

The dashboard is rendered with the project's built-in sample data, so the
screenshot is clearly labelled as simulated demo data rather than patient data.

What it produces under ``proposal/evidence/``:

* ``screenshots/dashboard_overview.png`` -- full-page screenshot.
* ``screenshots/dashboard_status_api.json`` -- live ``/api/status`` payload.
* ``screenshots/dashboard_cases_api.json`` -- live ``/api/cases`` payload.
* ``05_dashboard_runtime.md`` -- summary tying the images to the claim.

Usage::

    .venv/bin/python proposal/scripts/capture_dashboard_screenshot.py

"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = PROJECT_ROOT / "proposal" / "evidence"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def free_port() -> int:
    """
    Ask the OS for an unused TCP port.

    Avoids clashing with a dashboard the operator may already have open on the
    default port.

    Returns:
        A port number that was free at the moment of the call.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def fetch_json(url: str, timeout: float = 10.0) -> Dict[str, Any]:
    """
    GET a JSON endpoint.

    Args:
        url: Absolute URL.
        timeout: Seconds before giving up.

    Returns:
        Decoded JSON body.

    Raises:
        urllib.error.URLError: If the endpoint is unreachable or not yet ready.
    """
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, payload: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    """
    POST a JSON body and decode the JSON response.

    Args:
        url: Absolute URL.
        payload: Body to serialise as JSON.
        timeout: Seconds before giving up. The daily cycle walks every sample
            patient, so this is more generous than the read timeout.

    Returns:
        Decoded JSON body, or ``{"error": ...}`` if the call failed.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def wait_until_ready(base_url: str, attempts: int = 60) -> bool:
    """
    Poll the status endpoint until the server answers.

    Args:
        base_url: Server root, e.g. ``http://127.0.0.1:5151``.
        attempts: Maximum number of polls, one second apart.

    Returns:
        True if the server answered within the budget.
    """
    for _ in range(attempts):
        try:
            fetch_json(f"{base_url}/api/status")
            return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(1.0)
    return False


def case_status(base_url: str, patient_id: str) -> str:
    """
    Read one case's status from the running dashboard.

    Args:
        base_url: Server root.
        patient_id: Patient to look up, e.g. ``"P001"``.

    Returns:
        The status string, or ``"absent"`` if the patient has no case.
    """
    cases = fetch_json(f"{base_url}/api/cases")
    for case in cases if isinstance(cases, list) else []:
        if case.get("patient_id") == patient_id:
            return str(case.get("status"))
    return "absent"


def issue_portal_token(base_url: str, patient_id: str) -> Dict[str, Any]:
    """
    Mint a demo patient-portal access token through the staff-side endpoint.

    The portal is only reachable by URL token, which is exactly what a headless
    browser needs, so the capture can show the patient-facing surface that this
    revision adds without scripting a login.

    Args:
        base_url: Server root.
        patient_id: Patient to mint a link for.

    Returns:
        The endpoint's JSON response, containing ``access_token`` and
        ``portal_url`` on success.
    """
    return post_json(f"{base_url}/api/patients/{patient_id}/portal-link", {})


def driver_cycle(base_url: str) -> Dict[str, Any]:
    """
    Drive the dashboard's own control surface over the sample patients.

    A screenshot of an idle dashboard shows the layout but not the operating
    surface. Calling the same endpoints the operator's buttons call exercises
    the agent loop through the running web process, so the captured page shows
    real cases in real states produced by this process. The three replies reuse
    the scenarios the project's own demo walks through, so the resulting board
    covers a booking, a decline and an escalation.

    Args:
        base_url: Server root, e.g. ``http://127.0.0.1:5151``.

    Returns:
        ``{"cycle": <response>, "replies": [<response>, ...]}`` where each reply
        records the case status before and after.
    """
    cycle = post_json(f"{base_url}/api/run-cycle", {})
    replies = []
    for patient_id, message in (
        ("P001", "Yes, I'd like to schedule an appointment"),
        ("P007", "No thanks, not needed right now"),
        ("P004", "How much will this cost with my insurance?"),
    ):
        before = case_status(base_url, patient_id)
        response = post_json(
            f"{base_url}/api/simulate-reply",
            {"patient_id": patient_id, "message": message},
        )
        replies.append(
            {
                "patient_id": patient_id,
                "message": message,
                "response": response,
                "status_before": before,
                "status_after": case_status(base_url, patient_id),
            }
        )
    return {"cycle": cycle, "replies": replies}


def capture_portal(base_url: str, shots: Path, patient_id: str) -> Dict[str, Any]:
    """
    Capture the patient-facing portal for one patient.

    Args:
        base_url: Server root.
        shots: Screenshot output directory.
        patient_id: Patient whose portal should be shown.

    Returns:
        ``{"patient_id", "token_issued", "portal_url", "screenshot",
        "status_api", "artifacts"}``; ``token_issued`` is False when the endpoint
        refused, in which case the rest is omitted rather than faked.
    """
    token_response = issue_portal_token(base_url, patient_id)
    if not token_response.get("success"):
        return {"patient_id": patient_id, "token_issued": False, "response": token_response}

    portal_path = str(token_response["portal_url"])
    png = shots / "patient_portal.png"
    screenshot = shoot(f"{base_url}{portal_path}", png)
    status_api = fetch_json(f"{base_url}/api/patient-portal/{token_response['access_token']}/status")
    return {
        "patient_id": patient_id,
        "token_issued": True,
        "portal_url": portal_path,
        "screenshot": screenshot,
        "status_api": status_api,
        "artifacts": {"screenshot": str(png.relative_to(PROJECT_ROOT))},
    }


def start_server(port: int, audit_dir: Path) -> Any:
    """
    Serve the dashboard on a background thread.

    Imports the application object directly and serves it with werkzeug rather
    than shelling out to ``python web/app.py``, so no debug reloader is spawned
    and the process can be shut down cleanly.

    The process changes into ``audit_dir`` before importing the application
    because the audit logger opens ``audit_log.json`` relative to the working
    directory. Isolating it this way means exercising the agent here cannot
    append to the audit log that deployment evidence 1 is derived from, and the
    captured dashboard reflects only the run being demonstrated.

    Args:
        port: Port to bind on localhost.
        audit_dir: Directory to run in, receiving this run's audit log.

    Returns:
        The ``HTTPServer`` instance, already serving.
    """
    os.chdir(audit_dir)
    sys.path.insert(0, str(PROJECT_ROOT))
    from werkzeug.serving import make_server

    from utils.sample_data import initialize_sample_data
    from web.app import app, calendar, data_store

    initialize_sample_data(data_store, calendar)

    server = make_server("127.0.0.1", port, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def shoot(url: str, destination: Path, height: int = 1400) -> Dict[str, Any]:
    """
    Screenshot a URL with headless Chrome.

    ``--virtual-time-budget`` gives the dashboard's JavaScript time to fetch and
    render its panels before the capture, which a plain screenshot would miss.

    Args:
        url: Page to capture.
        destination: Output PNG path.
        height: Viewport height in CSS pixels.

    Returns:
        ``{"returncode": int, "size_bytes": int}`` for the produced file.
    """
    completed = subprocess.run(
        [
            CHROME,
            "--headless",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-pdf-header-footer",
            "--virtual-time-budget=10000",
            f"--window-size=1600,{height}",
            f"--screenshot={destination}",
            url,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return {
        "returncode": completed.returncode,
        "size_bytes": destination.stat().st_size if destination.exists() else 0,
    }


def portal_lines(portal: Mapping[str, Any] | None) -> List[str]:
    """
    Render the patient-portal section of the dashboard evidence.

    Args:
        portal: The portal capture block, or None when it was not attempted.

    Returns:
        Markdown lines, empty when there is nothing to report.
    """
    if not portal:
        return []
    if not portal.get("token_issued"):
        return [
            "## Patient portal",
            "",
            "The portal link could not be issued, so no portal screenshot was "
            f"taken. Endpoint response: `{json.dumps(portal.get('response'))}`",
            "",
        ]

    status_code = portal["screenshot"]["size_bytes"]
    lines = [
        "## Patient portal (added in this revision)",
        "",
        "The same process also serves the patient-facing portal. A staff-side "
        "endpoint mints a link token, and that URL was then rendered, so the page "
        "shown is produced by the running application rather than mocked up.",
        "",
        f"- Portal URL path: `{portal['portal_url']}` (token is scoped to one patient)",
        f"- Screenshot: `{portal['artifacts']['screenshot']}` "
        f"({'captured' if status_code else 'FAILED'}, {status_code:,} bytes)",
        "",
    ]
    status_api = portal.get("status_api") or {}
    if status_api:
        lines += [
            "`GET /api/patient-portal/<token>/status`:",
            "",
            "```json",
            json.dumps(status_api, indent=2, ensure_ascii=False),
            "```",
            "",
        ]
    return lines


def render_markdown(result: Dict[str, Any]) -> str:
    """
    Render the dashboard evidence summary as markdown.

    Args:
        result: Evidence payload.

    Returns:
        Markdown document.
    """
    status = "captured" if result["screenshot"]["size_bytes"] else "FAILED"
    drive = result.get("drive", {})
    cycle = drive.get("cycle", {})
    lines = [
        "# Deployment evidence 5 - Running dashboard",
        "",
        f"Captured: {result['generated_at']}",
        "",
        "## What this proves",
        "",
        "The deployed application starts, serves its staff dashboard and its",
        "patient portal, answers its JSON API and accepts operator commands. The",
        "screenshots are the rendered UI as a browser receives it, and the JSON",
        "files are the unedited API responses the dashboard consumes while",
        "rendering.",
        "",
        f"- Server: `{result['server']['base_url']}` (werkzeug, bound to localhost)",
        f"- Screenshot: `{result['artifacts']['screenshot']}` "
        f"({status}, {result['screenshot']['size_bytes']:,} bytes)",
        f"- Viewport: {result['screenshot']['viewport']}",
        "",
        *portal_lines(result.get("patient_portal")),
        "## Operator commands exercised",
        "",
        "The page was not captured idle. Before the screenshot the same endpoints",
        "the dashboard's buttons call were invoked, so the rendered page shows",
        "cases this process produced rather than an empty board:",
        "",
        f"- `POST /api/run-cycle` -> `success={cycle.get('success')}`, "
        f"`cases_processed={cycle.get('cases_processed')}`",
    ]
    for reply in drive.get("replies", []):
        lines.append(
            f"- `POST /api/simulate-reply` for `{reply.get('patient_id')}` "
            f"(`\"{reply.get('message')}\"`) -> status `{reply.get('status_before')}`"
            f" to `{reply.get('status_after')}`"
        )
    lines += [
        "",
        f"> The dashboard is populated with the project's built-in sample data,",
        f"> not real patients. It demonstrates the operator experience and the",
        f"> operating surface; it is not a record of live patient activity.",
        "",
        f"> This run's audit log was written to a temporary directory "
        f"(`{result['audit_isolation']['directory_basename']}/audit_log.json`) and",
        f"> discarded, so exercising the agent here cannot alter the audit trail",
        f"> deployment evidence 1 is computed from.",
        "",
        "## Live API responses",
        "",
        "`GET /api/status`:",
        "",
        "```json",
        json.dumps(result["status_api"], indent=2, ensure_ascii=False),
        "```",
        "",
        "`GET /api/cases` returned "
        f"{result['cases_summary']['count']} cases (first entry shown):",
        "",
        "```json",
        json.dumps(result["cases_summary"]["sample"], indent=2, ensure_ascii=False),
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """
    Command-line entry point.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Where evidence is written."
    )
    args = parser.parse_args(argv)

    shots = args.out_dir / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)

    port = free_port()
    base_url = f"http://127.0.0.1:{port}"

    # Exercises the agent, so it runs in a scratch directory: the audit logger
    # resolves audit_log.json relative to the working directory and must not
    # append to the log deployment evidence 1 is derived from.
    with tempfile.TemporaryDirectory(prefix="followup-dashboard-") as scratch:
        audit_dir = Path(scratch)
        server = start_server(port, audit_dir)
        try:
            if not wait_until_ready(base_url):
                print("dashboard did not become ready in time", file=sys.stderr)
                return 1

            drive = driver_cycle(base_url)

            png = shots / "dashboard_overview.png"
            screenshot = shoot(base_url, png)

            status_api = fetch_json(f"{base_url}/api/status")
            cases = fetch_json(f"{base_url}/api/cases")
            case_list = cases.get("cases", cases) if isinstance(cases, dict) else cases

            status_path = shots / "dashboard_status_api.json"
            cases_path = shots / "dashboard_cases_api.json"
            status_path.write_text(
                json.dumps(status_api, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            cases_path.write_text(
                json.dumps(cases, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            portal = capture_portal(base_url, shots, "P001")
            audit_log_written = (audit_dir / "audit_log.json").exists()
        finally:
            server.shutdown()

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "server": {"base_url": base_url, "port": port},
        "drive": drive,
        "audit_isolation": {
            "directory_basename": "followup-dashboard-*",
            "audit_log_written": audit_log_written,
            "note": (
                "The capture ran in a temporary directory, so audit_log.json was "
                "written there and discarded. The audit trail in evidence 1 is "
                "unaffected by exercising the agent here."
            ),
        },
        "screenshot": {
            **screenshot,
            "viewport": "1600x1400",
            "note": "Headless Chrome --screenshot with 10s virtual time budget",
        },
        "status_api": status_api,
        "cases_summary": {
            "count": len(case_list) if isinstance(case_list, list) else None,
            "sample": case_list[0] if isinstance(case_list, list) and case_list else None,
        },
        "patient_portal": portal,
        "artifacts": {
            "screenshot": str(png.relative_to(PROJECT_ROOT)),
            "status_api": str(status_path.relative_to(PROJECT_ROOT)),
            "cases_api": str(cases_path.relative_to(PROJECT_ROOT)),
        },
    }

    md_path = args.out_dir / "05_dashboard_runtime.md"
    json_path = args.out_dir / "05_dashboard_runtime.json"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"wrote {md_path.relative_to(PROJECT_ROOT)}")
    print(f"wrote {png.relative_to(PROJECT_ROOT)} ({screenshot['size_bytes']:,} bytes)")
    portal_shot = portal.get("screenshot", {}) if portal.get("token_issued") else {}
    if portal_shot:
        print(f"wrote {portal['artifacts']['screenshot']} ({portal_shot['size_bytes']:,} bytes)")
    return 0 if screenshot["size_bytes"] else 1


if __name__ == "__main__":
    sys.exit(main())
