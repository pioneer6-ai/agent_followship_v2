#!/usr/bin/env python3
"""
Confirm that a sent message was actually delivered, not just accepted.

A provider message id proves acceptance. This script closes the loop that the
rest of the pack deliberately stops short of, using two independent checks:

1. **Provider telemetry** -- SES emits per-message ``Delivery``, ``Bounce`` and
   ``Complaint`` events for the configured configuration set, published to
   CloudWatch under the ``AWS/SES`` namespace.
2. **Mailbox** -- an IMAP search of the recipient mailbox for the subject that
   was sent, reporting which folder holds it.

The second check is deliberately blunt: it reports the folder the message was
found in, because "delivered to the mailbox but filed as spam" and "delivered to
the inbox" are different operational facts and the proposal needs the real one.

It must read the *recipient's* mailbox, so its credentials are separate from the
sender's. Set ``EVIDENCE_MAILBOX_USER`` and ``EVIDENCE_MAILBOX_PASSWORD`` to the
mailbox that received the test message. They fall back to ``SMTP_USERNAME`` and
``SMTP_PASSWORD``, but the script refuses to run that fallback when it resolves
to the sending account, because a search of the sender's own mailbox cannot
fail and therefore proves nothing.

What it produces under ``proposal/evidence/``:

* ``07_delivery_confirmation.md`` -- the checks and their results.
* ``07_delivery_confirmation.json`` -- the same facts, machine-readable.

Usage::

    EVIDENCE_MAILBOX_USER=<recipient> EVIDENCE_MAILBOX_PASSWORD=<app password> \
        .venv/bin/python proposal/scripts/capture_delivery_confirmation.py

"""
from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = PROJECT_ROOT / "proposal" / "evidence"
DEFAULT_SEND_REPORT = DEFAULT_OUT_DIR / "06_live_delivery.json"

IMAP_HOST = "imap.gmail.com"

# Gmail's IMAP folder names are UTF-7 encoded for non-ASCII labels; these are the
# wire names for the folders worth checking, resolved from a folder listing.
MAILBOXES = (
    ("INBOX", "INBOX"),
    ("spam", '"[Gmail]/&V4NXPpCuTvY-"'),
    ("all_mail", '"[Gmail]/&YkBnCZCuTvY-"'),
)


def aws(arguments: Sequence[str]) -> Dict[str, Any]:
    """
    Run an AWS CLI command and normalise the outcome.

    Args:
        arguments: Arguments after ``aws``.

    Returns:
        ``{"ok": bool, "data": ..., "error": str | None}``.
    """
    completed = subprocess.run(
        ["aws", *arguments], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=90
    )
    if completed.returncode != 0:
        first = (completed.stderr or completed.stdout).strip().splitlines()
        return {"ok": False, "data": None, "error": first[0] if first else "unknown error"}
    try:
        return {"ok": True, "data": json.loads(completed.stdout or "{}"), "error": None}
    except json.JSONDecodeError:
        return {"ok": True, "data": completed.stdout.strip(), "error": None}


def cloudwatch_metrics(
    region: str, configuration_set: str, minutes: int, wait_s: int
) -> Dict[str, Any]:
    """
    Read SES delivery telemetry for the configuration set.

    CloudWatch metrics lag behind the send, so this polls until it sees a
    datapoint or the wait budget is spent, and reports which of the two happened.

    Args:
        region: AWS region.
        configuration_set: SES configuration set name.
        minutes: Look-back window.
        wait_s: Seconds to keep polling for the first datapoint.

    Returns:
        ``{"waited_s": int, "metrics": {name: datapoints}}``.
    """
    end = datetime.now(timezone.utc)
    results: Dict[str, Any] = {}
    waited = 0

    for name in ("Delivery", "Bounce", "Complaint"):
        results[name] = []

    started = time.monotonic()
    while True:
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=minutes)
        empty = True
        for name in results:
            response = aws(
                [
                    "cloudwatch",
                    "get-metric-statistics",
                    "--region",
                    region,
                    "--namespace",
                    "AWS/SES",
                    "--metric-name",
                    name,
                    "--dimensions",
                    f"Name=ses:configuration-set,Value={configuration_set}",
                    "--start-time",
                    start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "--end-time",
                    end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "--period",
                    "300",
                    "--statistics",
                    "Sum",
                ]
            )
            datapoints = (response.get("data") or {}).get("Datapoints", [])
            results[name] = sorted(datapoints, key=lambda d: d.get("Timestamp", ""))
            if datapoints:
                empty = False
        if not empty or time.monotonic() - started >= wait_s:
            break
        time.sleep(20)

    waited = int(time.monotonic() - started)
    return {"waited_s": waited, "metrics": results, "window_end": end.isoformat()}


def mailbox_check(subject_fragment: str) -> Dict[str, Any]:
    """
    Search the recipient mailbox for the sent subject.

    This is the independent half of the evidence: it must read the *recipient's*
    mailbox, so its credentials are deliberately separate from the sending
    account's. ``EVIDENCE_MAILBOX_USER`` and ``EVIDENCE_MAILBOX_PASSWORD`` name
    that mailbox; they fall back to ``SMTP_USERNAME``/``SMTP_PASSWORD`` only for
    single-mailbox deployments.

    Args:
        subject_fragment: Substring of the subject to search for.

    Returns:
        ``{"attempted": bool, "mailbox_account": str | None, "folders": {...},
        "error": str | None}``.
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    from tools.config import load_env_file

    load_env_file(str(PROJECT_ROOT / ".env"))
    user = os.environ.get("EVIDENCE_MAILBOX_USER") or os.environ.get("SMTP_USERNAME")
    password = os.environ.get("EVIDENCE_MAILBOX_PASSWORD") or os.environ.get("SMTP_PASSWORD")
    if not user or not password:
        return {
            "attempted": False,
            "mailbox_account": None,
            "folders": {},
            "error": "no mailbox credentials configured",
        }

    # Reading the sending account proves nothing about arrival: the message is
    # sitting in its own Sent folder whether or not it was ever delivered. When
    # the sending identity has moved to a different mailbox than the recipient,
    # a fallback to SMTP_* silently lands here, so refuse rather than report a
    # check that cannot fail.
    senders = {
        (os.environ.get(key) or "").strip().lower()
        for key in ("EMAIL_FROM", "SMTP_USERNAME", "AWS_SES_SOURCE")
    }
    if user.strip().lower() in senders:
        return {
            "attempted": False,
            "mailbox_account": user,
            "folders": {},
            "error": (
                f"mailbox credentials resolve to the sending account ({user}); "
                "set EVIDENCE_MAILBOX_USER/EVIDENCE_MAILBOX_PASSWORD to the "
                "recipient's mailbox so the search stays independent of the send"
            ),
        }

    folders: Dict[str, Any] = {}
    try:
        client = imaplib.IMAP4_SSL(IMAP_HOST)
        client.login(user, password)
    except Exception as exc:  # noqa: BLE001 - reported, not raised, on purpose
        return {
            "attempted": True,
            "mailbox_account": user,
            "folders": {},
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        for label, mailbox in MAILBOXES:
            entry: Dict[str, Any] = {"matches": 0, "headers": []}
            try:
                client.select(mailbox, readonly=True)
                _, data = client.search(None, f'(HEADER Subject "{subject_fragment}")')
                ids = data[0].split()
                entry["matches"] = len(ids)
                for identifier in ids[-3:]:
                    _, fetched = client.fetch(
                        identifier, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])"
                    )
                    raw = fetched[0][1].decode("utf-8", errors="replace")
                    parsed = email.message_from_string(raw)
                    entry["headers"].append(
                        {
                            "subject": parsed.get("Subject"),
                            "from": parsed.get("From"),
                            "date": parsed.get("Date"),
                            "message_id": parsed.get("Message-ID"),
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - reported per folder
                entry["error"] = f"{type(exc).__name__}: {exc}"
            folders[label] = entry
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001 - best effort
            pass

    return {"attempted": True, "mailbox_account": user, "folders": folders, "error": None}


def render_markdown(result: Mapping[str, Any]) -> str:
    """
    Render the delivery confirmation as markdown.

    Args:
        result: Evidence payload.

    Returns:
        Markdown document.
    """
    sent = result["sent_message"]
    telemetry = result["provider_telemetry"]
    mailbox = result["mailbox"]

    delivered_events = sum(
        point.get("Sum", 0) for point in telemetry["metrics"].get("Delivery", [])
    )
    bounced_events = sum(
        point.get("Sum", 0) for point in telemetry["metrics"].get("Bounce", [])
    )
    complaint_events = sum(
        point.get("Sum", 0) for point in telemetry["metrics"].get("Complaint", [])
    )

    found_in: List[str] = [
        label for label, entry in mailbox.get("folders", {}).items() if entry.get("matches")
    ]

    lines = [
        "# Deployment evidence 7 - Delivery confirmation",
        "",
        f"Captured: {result['generated_at']}",
        "",
        "## What this proves",
        "",
        "Evidence 6 shows the provider accepted a message. This artifact shows",
        "what happened to it afterwards, from two independent vantage points: the",
        "provider's own delivery telemetry and the recipient mailbox. These are",
        "different claims and are kept separate on purpose.",
        "",
        "## The message under test",
        "",
        f"- Message id (as returned by SES): `{sent.get('message_id')}`",
        f"- Recipient: `{sent.get('recipient')}`",
        f"- Sent at: {sent.get('generated_at')}",
        "",
        "## Check 1 - provider delivery telemetry",
        "",
        f"CloudWatch `AWS/SES` for configuration set `{result['configuration_set']}`, "
        f"namespace queried in `{result['region']}`:",
        "",
        "| Metric | Sum |",
        "| --- | ---: |",
        f"| Delivery | {int(delivered_events)} |",
        f"| Bounce | {int(bounced_events)} |",
        f"| Complaint | {int(complaint_events)} |",
        "",
    ]

    if not any((delivered_events, bounced_events, complaint_events)):
        lines += [
            f"> No datapoint appeared within the {telemetry['waited_s']}s polling",
            "> window. SES publishes these metrics on a delay, so an empty result",
            "> here means *not yet visible*, not *not delivered*. Check 2 below is",
            "> the stronger of the two and does not depend on this.",
            "",
        ]

    lines += [
        "## Check 2 - recipient mailbox",
        "",
        f"- Method: IMAP `{IMAP_HOST}`, read-only search of the recipient account",
        f"- Mailbox searched: `{mailbox.get('mailbox_account') or 'unknown'}`",
        f"- Subject searched: \"{result['subject_fragment']}\"",
        "",
    ]

    if not mailbox.get("attempted"):
        lines += [f"- Not attempted: {mailbox.get('error')}", ""]
    elif mailbox.get("error"):
        lines += [f"- Failed: {mailbox['error']}", ""]
    else:
        lines += ["| Folder | Messages found |", "| --- | ---: |"]
        for label, entry in mailbox["folders"].items():
            lines.append(f"| {label} | {entry.get('matches', 0)} |")
        lines.append("")

        if found_in:
            lines += [
                f"The message was found in: **{', '.join(found_in)}**.",
                "",
            ]
            if "INBOX" in found_in:
                lines += [
                    "It reached the inbox, which is the outcome a recall programme",
                    "needs.",
                    "",
                ]
            else:
                lines += [
                    "**It was delivered, but not to the inbox.** The mailbox",
                    "accepted the message and filed it as spam. This is the single",
                    "most consequential operational finding in this pack: a",
                    "reminder that lands in a spam folder has not reminded",
                    "anyone, and no amount of agent correctness compensates for",
                    "it. It is a sending-identity problem, not a software",
                    "problem -- see the deliverability item in the proposal's",
                    "risk table.",
                    "",
                ]
            for label in found_in:
                for header in mailbox["folders"][label]["headers"]:
                    lines += [
                        "Message headers observed:",
                        "",
                        "```",
                        f"Subject: {header.get('subject')}",
                        f"From: {header.get('from')}",
                        f"Date: {header.get('date')}",
                        f"Message-ID: {header.get('message_id')}",
                        "```",
                        "",
                    ]
        else:
            lines += [
                "The message was **not found** in any folder searched. Combined with",
                "an accepted message id, that points at a delay in mailbox",
                "propagation or at a folder outside the search scope, and the check",
                "should be repeated before drawing a conclusion.",
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
    parser.add_argument("--send-report", type=Path, default=DEFAULT_SEND_REPORT)
    parser.add_argument(
        "--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Where evidence is written."
    )
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--configuration-set", default="patient-followup")
    parser.add_argument("--wait", type=int, default=180, help="Seconds to poll telemetry.")
    parser.add_argument(
        "--subject", default="delivery confirmation", help="Subject fragment to search for."
    )
    args = parser.parse_args(argv)

    if not args.send_report.exists():
        print(f"no send report at {args.send_report}; run capture_live_send.py first", file=sys.stderr)
        return 1

    previous = json.loads(args.send_report.read_text(encoding="utf-8"))
    send_result = previous.get("send", {}).get("result", {})

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "region": args.region,
        "configuration_set": args.configuration_set,
        "subject_fragment": args.subject,
        "sent_message": {
            "message_id": send_result.get("message_id"),
            "recipient": previous.get("send", {}).get("recipient"),
            "status": send_result.get("status"),
            "simulated": send_result.get("simulated"),
            "generated_at": previous.get("generated_at"),
        },
        "provider_telemetry": cloudwatch_metrics(
            args.region, args.configuration_set, minutes=180, wait_s=args.wait
        ),
        "mailbox": mailbox_check(args.subject),
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    md_path = args.out_dir / "07_delivery_confirmation.md"
    json_path = args.out_dir / "07_delivery_confirmation.json"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"wrote {md_path.relative_to(PROJECT_ROOT)}")
    print(f"wrote {json_path.relative_to(PROJECT_ROOT)}")
    found = [k for k, v in result["mailbox"].get("folders", {}).items() if v.get("matches")]
    print(f"  mailbox matches in: {found or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
