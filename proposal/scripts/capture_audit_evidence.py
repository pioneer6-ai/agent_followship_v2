#!/usr/bin/env python3
"""
Turn the agent's audit log into deployment evidence.

The audit log is the agent's compliance record: one JSON object per perceived
event, written by the OBSERVE phase as the daily cycle runs. On its own it is
just a file; as evidence it is only meaningful alongside two facts, which this
script records explicitly so a reviewer never has to guess:

* **Provenance.** The live log is copied to a frozen snapshot under
  ``evidence/artifacts/`` and that snapshot's SHA-256 and size are recorded, so
  the numbers below can be re-derived later. The copy matters because the live
  file is append-only and is in fact appended to by the test suite, which would
  otherwise make the report impossible to reproduce.
* **Scope.** The agent only transmits for real when *both* ``MESSAGING_DRY_RUN=0``
  and ``AGENT_LIVE_SENDS=1`` are set. The default delivery backend prints
  instead of sending, so the communication events here are evidence of the
  agent's *decision-making and audit trail*, not of message transmission. That
  distinction is stated in the generated document rather than left for a
  reader to misread.

What it produces, under ``proposal/evidence/``:

* ``01_agent_decision_audit.md`` -- human-readable summary tables.
* ``01_agent_decision_audit.json`` -- the same numbers, machine-readable, for
  anyone who wants to re-derive them.

Usage::

    .venv/bin/python proposal/scripts/capture_audit_evidence.py

"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

#: Repository root, derived from this file's location (proposal/scripts/...).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Default location of the audit log the agent writes.
DEFAULT_AUDIT_LOG = PROJECT_ROOT / "audit_log.json"

#: Default output directory for the evidence pack.
DEFAULT_OUT_DIR = PROJECT_ROOT / "proposal" / "evidence"

#: Event type -> the key the agent uses to distinguish record kinds.
DECISION_EVENT = "agent_decision"
COMMUNICATION_EVENT = "communication"


def sha256_of(path: Path) -> str:
    """
    Hash a file so the evidence names the exact artifact it describes.

    Args:
        path: File to hash.

    Returns:
        Lower-case hex SHA-256 digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_generation(audit_log: Path) -> Dict[str, Any] | None:
    """
    Read the run summary the generator left next to the audit log, if any.

    Args:
        audit_log: Path to the audit log under analysis.

    Returns:
        The parsed summary, or None when the sidecar is absent (which is the case
        for a log produced by anything other than the generator).
    """
    sidecar = audit_log.with_name(audit_log.stem + ".generation.json")
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def freeze_generation(audit_log: Path, artifacts_dir: Path) -> Path | None:
    """
    Freeze the generator's run summary alongside the log it describes.

    Args:
        audit_log: Path to the audit log.
        artifacts_dir: Directory that holds frozen evidence artifacts.

    Returns:
        Path to the frozen summary, or None when there is no summary to freeze.
    """
    sidecar = audit_log.with_name(audit_log.stem + ".generation.json")
    if not sidecar.exists():
        return None
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    target = artifacts_dir / "audit_log.generation.json"
    target.write_bytes(sidecar.read_bytes())
    return target


def freeze(path: Path, artifacts_dir: Path) -> Path:
    """
    Copy the audit log to a stable snapshot so the report is reproducible.

    The live log is append-only and the test suite writes to it, so analysing it
    in place would produce a different report on every run. The snapshot is the
    artifact the numbers are computed from; its hash is recorded in the report.

    Args:
        path: Live audit log.
        artifacts_dir: Directory that holds frozen evidence artifacts.

    Returns:
        Path to the snapshot.
    """
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    snapshot = artifacts_dir / f"audit_log_snapshot{path.suffix or '.json'}"
    snapshot.write_bytes(path.read_bytes())
    return snapshot


def load_events(path: Path) -> List[Mapping[str, Any]]:
    """
    Parse the audit log.

    The file is a JSON array, but a JSONL variant is accepted too, because an
    append-only writer is a natural way to grow a log and a reviewer should not
    have to care which one they were handed.

    Args:
        path: Audit log to read.

    Returns:
        The list of events, with non-object entries dropped.

    Raises:
        SystemExit: If the file is missing or unparseable.
    """
    if not path.exists():
        raise SystemExit(f"Audit log not found: {path}")
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise SystemExit(f"Audit log is empty: {path}")

    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed.append(json.loads(line))
            except ValueError:
                continue

    if not isinstance(parsed, list):
        raise SystemExit(f"Audit log is neither a JSON array nor JSONL: {path}")
    return [event for event in parsed if isinstance(event, dict)]


def _counter(events: Sequence[Mapping[str, Any]], key: str) -> Dict[str, int]:
    """
    Count events by a field, most frequent first.

    Args:
        events: Events to count.
        key: Field name; missing values are labelled ``"(unset)"``.

    Returns:
        Ordered ``{value: count}`` mapping.
    """
    counts = collections.Counter(
        str(event.get(key) if event.get(key) is not None else "(unset)")
        for event in events
    )
    return dict(counts.most_common())


def _percent(part: int, whole: int) -> float:
    """Percentage of ``whole`` that ``part`` represents; 0.0 when empty."""
    return round(100.0 * part / whole, 1) if whole else 0.0


def compute_metrics(
    events: Sequence[Mapping[str, Any]],
    source: Mapping[str, Any],
    generation: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    Derive every headline number the evidence document reports.

    Args:
        events: Parsed audit-log events.
        source: Provenance block (path, hash, size).
        generation: Optional run summary written by the generator, describing how
            the log was produced (period, injected replies, offline guard).

    Returns:
        A JSON-serializable metrics dictionary.
    """
    decisions = [e for e in events if e.get("event_type") == DECISION_EVENT]
    comms = [e for e in events if e.get("event_type") == COMMUNICATION_EVENT]
    timestamps = sorted(str(e.get("timestamp")) for e in events if e.get("timestamp"))

    failures = [e for e in comms if e.get("success") is False]
    successes = [e for e in comms if e.get("success") is True]
    outbound = [e for e in comms if e.get("direction") == "outbound"]
    inbound = [e for e in comms if e.get("direction") == "inbound"]

    by_channel: Dict[str, Dict[str, Any]] = {}
    for channel, count in _counter(comms, "channel").items():
        channel_events = [e for e in comms if str(e.get("channel")) == channel]
        channel_failures = [e for e in channel_events if e.get("success") is False]
        by_channel[channel] = {
            "attempts": count,
            "succeeded": count - len(channel_failures),
            "failed": len(channel_failures),
            "success_rate_pct": _percent(count - len(channel_failures), count),
        }

    escalated_ids = {
        str(e.get("patient_id"))
        for e in decisions
        if str(e.get("case_status")) == "escalated"
    }
    escalated_actions = [e for e in decisions if e.get("action_taken") == "escalate_to_staff"]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": dict(source),
        "capture_environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "totals": {
            "events": len(events),
            "decisions": len(decisions),
            "communications": len(comms),
            "distinct_patients": len({str(e.get("patient_id")) for e in decisions}),
            "first_event": timestamps[0] if timestamps else None,
            "last_event": timestamps[-1] if timestamps else None,
        },
        "decisions": {
            "by_action": _counter(decisions, "action_taken"),
            "by_urgency": _counter(decisions, "urgency"),
            "by_case_status": _counter(decisions, "case_status"),
            "escalation_actions": len(escalated_actions),
            "patients_ever_escalated": len(escalated_ids),
            "escalation_rate_pct": _percent(len(escalated_actions), len(decisions)),
        },
        "communications": {
            "attempts": len(comms),
            "succeeded": len(successes),
            "failed": len(failures),
            "success_rate_pct": _percent(len(successes), len(comms)),
            "outbound": len(outbound),
            "inbound_replies": len(inbound),
            "by_channel": by_channel,
        },
        "scope_note": (
            "The agent's default delivery backend prints instead of transmitting; "
            "real sends require MESSAGING_DRY_RUN=0 AND AGENT_LIVE_SENDS=1. These "
            "events therefore evidence the agent's decisions and its audit trail, "
            "not message transmission."
        ),
        "generation": dict(generation) if generation else None,
    }


def generation_lines(generation: Mapping[str, Any] | None) -> List[str]:
    """
    Render the run summary that produced the log under analysis.

    Args:
        generation: The generator's summary, or None when it is unavailable.

    Returns:
        Markdown lines, empty when there is nothing to say.
    """
    if not generation:
        return []

    lines = [
        "## How this record was produced",
        "",
        "The log was not hand-written: it is the output of the real orchestrator "
        "driven by `proposal/scripts/generate_audit_record.py` over the project's "
        "sample patients, with the clock fixed so the run is independent of the "
        "wall clock.",
        "",
        f"- Simulated period: {generation.get('base_day')} to {generation.get('period_end')} "
        f"({generation.get('days')} daily cycles)",
        f"- Delivery calls handled offline: {generation.get('offline_deliveries_observed')}",
    ]

    actions = generation.get("decisions_by_action") or {}
    if actions:
        rendered = ", ".join(f"{key} {value}" for key, value in actions.items())
        lines.append(f"- Decisions by action: {rendered}")

    replies = generation.get("replies") or []
    if replies:
        lines += [
            "",
            "### Injected patient replies (the Observe path)",
            "",
            "Each reply was fed to the agent's reply handler so the record also "
            "covers what happens when a patient answers, rather than only "
            "outbound decision-making.",
            "",
            "| Day | Patient | Reply | Case status before | Case status after |",
            "| --- | --- | --- | --- | --- |",
        ]
        for reply in replies:
            lines.append(
                f"| {reply['day']} | {reply['patient_id']} | {reply['message']} | "
                f"{reply['status_before']} | {reply['status_after']} |"
            )

    note = generation.get("note")
    if note:
        lines += ["", f"> Generator note: {note}"]

    if generation.get("sha256"):
        lines.append(
            f"\nRun summary frozen as `{generation['snapshot_path']}` "
            f"(SHA-256 `{generation['sha256']}`)."
        )

    lines.append("")
    return lines


def failure_reading(comms: Mapping[str, Any]) -> str:
    """
    Explain the failure count honestly, whatever it happens to be.

    Args:
        comms: The communications block of the metrics.

    Returns:
        A markdown section. A run with no failures must not be presented with the
        fallback-path narrative, because that would imply failures were exercised
        when none were.
    """
    if comms["failed"]:
        return (
            "### Reading the failure count\n\n"
            "A failed attempt here is the agent's fallback path working as designed: "
            "a channel with no contact information for that patient reports failure "
            "and the agent moves to another channel rather than crashing or claiming "
            "the message went out."
        )
    return (
        "### Reading the success rate\n\n"
        "Every attempt is recorded as successful because the delivery backend is "
        "the offline printer: it accepts any recipient it is handed and so cannot "
        "fail. The rate is therefore a property of the offline backend, **not** "
        "carrier deliverability, and it is not comparable to the fallback-path "
        "behaviour described in the proposal's robustness section. Carrier-level "
        "delivery is evidenced separately, by the live-send and delivery-telemetry "
        "captures."
    )


def render_markdown(metrics: Mapping[str, Any]) -> str:
    """
    Render the metrics as the human-readable evidence document.

    Args:
        metrics: Output of :func:`compute_metrics`.

    Returns:
        Markdown text.
    """
    source = metrics["source"]
    totals = metrics["totals"]
    decisions = metrics["decisions"]
    comms = metrics["communications"]

    def table(mapping: Mapping[str, Any], left: str, right: str) -> List[str]:
        lines = [f"| {left} | {right} |", "| --- | ---: |"]
        lines += [f"| {key} | {value} |" for key, value in mapping.items()]
        return lines

    channel_rows = [
        "| Channel | Attempts | Succeeded | Failed | Success rate |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for channel, stats in comms["by_channel"].items():
        channel_rows.append(
            f"| {channel} | {stats['attempts']} | {stats['succeeded']} | "
            f"{stats['failed']} | {stats['success_rate_pct']}% |"
        )

    lines: List[str] = [
        "# Deployment evidence 1 - Agent decisions and audit trail",
        "",
        f"Captured: {metrics['generated_at']}",
        "",
        "## What this proves",
        "",
        "The agent runs its full Perceive -> Decide -> Act -> Observe loop and "
        "records a durable, queryable audit trail for every decision and every "
        "delivery attempt. The counts below are computed directly from the "
        "artifact whose hash is recorded here, so they can be re-derived by a "
        "reviewer instead of taken on trust.",
        "",
        "## Scope (what this does *not* claim)",
        "",
        metrics["scope_note"],
        "",
        "## Artifact provenance",
        "",
        f"- Live file: `{source['live_path']}` (append-only; read via the snapshot below)",
        f"- Snapshot analysed: `{source['snapshot_path']}`",
        f"- SHA-256: `{source['sha256']}`",
        f"- Size: {source['size_bytes']:,} bytes",
        f"- Events: {totals['events']:,}",
        f"- Entry timestamps (audit logger clock): {totals['first_event']} to {totals['last_event']}",
        f"- Patients: {totals['distinct_patients']} distinct patient identifiers",
        "",
        "> Timestamp note: the audit logger stamps entries from its own clock, not "
        "from the agent's injected clock. Under the fixed-clock harness used to "
        "produce this record that means the timestamps show a sub-second wall-clock "
        "window while the decisions they describe span the simulated period above. "
        "In production the two coincide, because both are the system clock; the "
        "simulated period is the meaningful span for reading this record.",
        "",
        "> Data-hygiene note: `audit_log.json` is append-only and the automated test",
        "> suite writes to it (tests exercise the real orchestrator and its audit",
        "> logger), so the live file grows every time the tests run. The snapshot",
        "> above was frozen at capture time and is the artifact these figures come",
        "> from; re-running the tests will not reproduce identical totals from the",
        "> live file. See the robustness section of the proposal.",
        "",
        *generation_lines(metrics.get("generation")),
        "## Agent decisions",
        "",
        f"{totals['decisions']:,} decisions were recorded.",
        "",
        "### Action taken",
        "",
        *table(decisions["by_action"], "Action", "Count"),
        "",
        "### Case urgency at decision time",
        "",
        *table(decisions["by_urgency"], "Urgency", "Count"),
        "",
        "### Case status at decision time",
        "",
        *table(decisions["by_case_status"], "Case status", "Count"),
        "",
        "### Escalation to human staff",
        "",
        f"- Escalation actions: {decisions['escalation_actions']:,}",
        f"- Distinct patients ever escalated: {decisions['patients_ever_escalated']}",
        f"- Escalation rate: {decisions['escalation_rate_pct']}% of decisions",
        "",
        "## Communication attempts",
        "",
        f"{comms['attempts']:,} attempts "
        f"({comms['outbound']:,} outbound, {comms['inbound_replies']} inbound "
        "patient replies).",
        "",
        *channel_rows,
        "",
        f"Overall success rate: **{comms['success_rate_pct']}%** "
        f"({comms['succeeded']:,} of {comms['attempts']:,}).",
        "",
        failure_reading(comms),
        "",
    ]
    return "\n".join(lines) + "\n"


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
        "--audit-log", type=Path, default=DEFAULT_AUDIT_LOG, help="Audit log to read."
    )
    parser.add_argument(
        "--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Where evidence is written."
    )
    args = parser.parse_args(argv)

    artifacts_dir = args.out_dir / "artifacts"
    snapshot = freeze(args.audit_log, artifacts_dir)
    events = load_events(snapshot)
    source = {
        "live_path": str(args.audit_log.relative_to(PROJECT_ROOT))
        if args.audit_log.is_relative_to(PROJECT_ROOT)
        else str(args.audit_log),
        "snapshot_path": str(snapshot.relative_to(PROJECT_ROOT)),
        "sha256": sha256_of(snapshot),
        "size_bytes": snapshot.stat().st_size,
    }
    metrics = compute_metrics(events, source, load_generation(args.audit_log))
    frozen_sidecar = freeze_generation(args.audit_log, artifacts_dir)
    if frozen_sidecar is not None and metrics["generation"] is not None:
        metrics["generation"]["snapshot_path"] = str(
            frozen_sidecar.relative_to(PROJECT_ROOT)
        )
        metrics["generation"]["sha256"] = sha256_of(frozen_sidecar)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "01_agent_decision_audit.json"
    md_path = args.out_dir / "01_agent_decision_audit.md"
    json_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(metrics), encoding="utf-8")

    print(f"wrote {md_path.relative_to(PROJECT_ROOT)}")
    print(f"wrote {json_path.relative_to(PROJECT_ROOT)}")
    print(
        f"  decisions={metrics['totals']['decisions']:,} "
        f"communications={metrics['totals']['communications']:,} "
        f"success_rate={metrics['communications']['success_rate_pct']}%"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
