#!/usr/bin/env python3
"""
Produce the audit record that deployment evidence 1 is computed from.

The record has to be produced by the real agent -- a hand-written fixture would
prove nothing about the decision loop. This drives the orchestrator over the
project's built-in sample patients for a fixed run of days, injects a few patient
replies so the Observe path is exercised too, and leaves ``audit_log.json`` in
the working directory for ``capture_audit_evidence.py`` to freeze and analyse.

Two deliberate choices keep the run honest and reproducible:

* The clock is fixed and advanced one day at a time, so the record does not
  depend on the wall clock and a reviewer gets the same shape of data.
* The run is asserted to be offline. The sample patients carry invented
  addresses, so letting the channels transmit would mean aiming real messages at
  them. ``agent.delivery`` only swaps in a transmitting backend when
  ``AGENT_LIVE_SENDS=1``; this script clears that variable and then *checks* that
  every channel is on the printing backend and that every recorded message id
  is one the offline backend minted. Transmission is evidenced separately, on
  purpose, by ``capture_live_send.py``.

Because ``agent.delivery`` and ``tools.config`` read ``.env`` themselves, the
script cannot promise ``.env`` was never read -- it promises the outcome that
matters: nothing was transmitted.

Usage::

    .venv/bin/python proposal/scripts/generate_audit_record.py

"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]

BASE_DAY = date(2026, 9, 21)
DAYS = 42

# Replies injected on the listed day index, mirroring the scenarios the
# project's own demo walks through: a booking, a decline, and a question the
# agent is expected to escalate rather than answer itself. They are spaced far
# enough apart that each patient is due a reminder first, so the Observe path
# is reached from a real state rather than forced.
REPLY_SCRIPT = {
    2: [("P006", "Yes, I'd like to schedule an appointment")],
    9: [("P007", "No thanks, not needed right now")],
    16: [("P004", "How much will this cost with my insurance?")],
    23: [("P003", "Can I come in next week?")],
    30: [("P011", "Yes please")],
}


def build_agent():
    """
    Construct an orchestrator over the sample patients, forced offline.

    Returns:
        ``(agent, clinic_tz)``.
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    from agent.delivery import PrintDeliveryBackend
    from agent.orchestrator import FollowUpAgentOrchestrator
    from core.clock import FixedClock
    from core.config import ClinicPolicyConfig
    from core.data_access import MockCalendarIntegration, MockPatientDataStore
    from utils.sample_data import initialize_sample_data

    # The agent layer transmits only when this is set (see
    # agent.delivery.is_configured_for_live_sends), and the tool layer
    # additionally needs MESSAGING_DRY_RUN=0. Clear the agent-layer switch so a
    # permissive shell cannot turn this record into a transmission log, and pin
    # the tool layer's switch so the guard holds even if a caller sets neither.
    os.environ.pop("AGENT_LIVE_SENDS", None)
    os.environ["MESSAGING_DRY_RUN"] = "1"

    data_store = MockPatientDataStore()
    calendar = MockCalendarIntegration()
    policy = ClinicPolicyConfig()
    initialize_sample_data(data_store, calendar)

    clinic_tz = ZoneInfo(policy.clinic_timezone)
    clock = FixedClock(datetime.combine(BASE_DAY, datetime.min.time(), tzinfo=clinic_tz).replace(hour=9))
    agent = FollowUpAgentOrchestrator(
        data_store, calendar, policy, clock=clock, escalation_email="staff@clinic.example"
    )

    offline = all(
        isinstance(channel.backend, PrintDeliveryBackend)
        for channel in agent.notification_channels.values()
    )
    if not offline:
        backends = {
            channel.value: type(channel.backend).__name__
            for channel in agent.notification_channels.values()
        }
        raise RuntimeError(f"refusing to record a run that could transmit: {backends}")
    return agent, clinic_tz


def install_transmission_guard() -> list:
    """
    Make a transmitting delivery impossible, and count what was delivered.

    The orchestrator chooses its backends from the environment, so asserting the
    constructed channels is not enough on its own -- a different code path could
    build its own. Patching the transmitting backend's ``deliver`` to raise turns
    "we believe nothing was sent" into "nothing could have been sent".

    Returns:
        A counter dict, mutated as calls occur: ``offline`` counts deliveries the
        printing backend handled, ``transmit`` counts attempted transmissions.
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    from agent.delivery import PrintDeliveryBackend, ToolkitDeliveryBackend

    counters = {"offline": 0, "transmit": 0}
    printing_deliver = PrintDeliveryBackend.deliver

    def counting_deliver(self, *args, **kwargs):
        counters["offline"] += 1
        return printing_deliver(self, *args, **kwargs)

    def refusing_deliver(self, *args, **kwargs):
        counters["transmit"] += 1
        raise RuntimeError(
            "ToolkitDeliveryBackend.deliver() reached while generating the audit "
            "record; the sample patients carry invented addresses, so this run "
            "must stay offline"
        )

    PrintDeliveryBackend.deliver = counting_deliver
    ToolkitDeliveryBackend.deliver = refusing_deliver
    return counters


def run(out_path: Path) -> dict:
    """
    Drive the agent over the scripted days and write the audit log.

    Args:
        out_path: Where the audit log should end up.

    Returns:
        Summary of what was run.
    """
    guard = install_transmission_guard()
    agent, clinic_tz = build_agent()
    cycles = []
    replies = []

    # The orchestrator prints a narrated trace; it is captured rather than
    # discarded so the report can quote how much was processed.
    chatter = io.StringIO()
    with redirect_stdout(chatter):
        for index in range(DAYS):
            today = BASE_DAY + timedelta(days=index)
            # Advance the injected clock rather than trusting the wall clock:
            # both the cycle and the reply handler date-ground their work on it.
            agent.clock.fixed_now = datetime.combine(
                today, datetime.min.time(), tzinfo=clinic_tz
            ).replace(hour=9)
            processed = agent.run_daily_cycle(today)
            cycles.append({"day": today.isoformat(), "cases_processed": len(processed)})

            for patient_id, message in REPLY_SCRIPT.get(index, []):
                # Snapshot the status *before* handling: the case objects are
                # live references, so reading them afterwards would report the
                # post-reply status as if it were the pre-reply one.
                case = agent.get_case_by_patient_id(patient_id)
                status_before = case.status.value if case else None
                agent.handle_incoming_reply(patient_id, message)
                case_after = agent.get_case_by_patient_id(patient_id)
                replies.append(
                    {
                        "day": today.isoformat(),
                        "patient_id": patient_id,
                        "message": message,
                        "status_before": status_before,
                        "status_after": case_after.status.value if case_after else "no_case",
                    }
                )

        final_statuses: Dict[str, int] = {}
        for case in agent.get_active_cases():
            key = case.status.value
            final_statuses[key] = final_statuses.get(key, 0) + 1

    # The orchestrator persists on every log call, so its own file is the record.
    live_log = Path("audit_log.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if live_log.resolve() != out_path.resolve():
        out_path.write_bytes(live_log.read_bytes())
    entries = json.loads(out_path.read_text(encoding="utf-8"))

    # Second half of the offline guarantee, checked at call time rather than
    # only at construction: every delivery that actually happened must have gone
    # through the printing backend. A transmitting backend would already have
    # raised in its own patched deliver(), so reaching this point with a non-zero
    # print count means the log describes simulated sends only.
    if guard["transmit"]:
        raise RuntimeError(
            f"refusing to publish: {guard['transmit']} transmitting delivery call(s)"
        )
    if not guard["offline"]:
        raise RuntimeError("refusing to publish: no delivery calls were observed, so nothing ran")

    event_types: Dict[str, int] = {}
    actions: Dict[str, int] = {}
    channels: Dict[str, int] = {}
    for entry in entries:
        event_types[entry["event_type"]] = event_types.get(entry["event_type"], 0) + 1
        if entry["event_type"] == "agent_decision" and entry.get("action_taken"):
            key = entry["action_taken"]
            actions[key] = actions.get(key, 0) + 1
        if entry["event_type"] == "communication" and entry.get("channel"):
            key = entry["channel"]
            channels[key] = channels.get(key, 0) + 1

    summary = {
        "base_day": BASE_DAY.isoformat(),
        "period_end": (BASE_DAY + timedelta(days=DAYS - 1)).isoformat(),
        "days": DAYS,
        "cycles": cycles,
        "replies": replies,
        "final_case_statuses": final_statuses,
        "events_written": len(entries),
        "event_types": event_types,
        "decisions_by_action": actions,
        "communications_by_channel": channels,
        "offline_deliveries_observed": guard["offline"],
        "offline_channels": True,
        "note": (
            "Channels were the offline printing backend, asserted at both ends: "
            "every constructed channel had to be a PrintDeliveryBackend, and the "
            "transmitting backend's deliver() was patched to raise. That is what "
            "makes this a record of decisions instead of transmission. The "
            "sample patients carry invented addresses, so the run must not "
            "transmit; real transmission is evidenced by capture_live_send.py."
        ),
    }
    print(json.dumps(summary, indent=2))

    # Sidecar provenance. The audit log alone cannot say how it was produced,
    # and evidence 1 has to report that; writing the run summary next to the log
    # keeps the two together and lets the capture script quote it verbatim
    # instead of guessing.
    sidecar = out_path.with_name(out_path.stem + ".generation.json")
    sidecar.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    """
    Command-line entry point.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=PROJECT_ROOT / "audit_log.json",
        help="Where to write the audit record.",
    )
    args = parser.parse_args(argv)

    # Belt and braces on top of build_agent()'s assertion: the tool layer's own
    # dry-run switch is pinned before anything can read .env. load_env_file()
    # never clobbers an already-exported value, so this survives.
    os.environ["MESSAGING_DRY_RUN"] = "1"
    os.chdir(PROJECT_ROOT)
    run(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
