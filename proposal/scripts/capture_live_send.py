#!/usr/bin/env python3
"""
Capture one real end-to-end delivery as deployment evidence.

Every other artifact in this pack shows the system working offline. This script
closes the gap by driving one genuine send through the agent's own tool layer and
recording what the provider actually answered.

It refuses to transmit unless ``--approve`` is passed, because the send reaches a
real mailbox or handset. Without that flag it reports exactly what it would have
done and stops.

What it produces under ``proposal/evidence/``:

* ``06_live_delivery.md`` -- the send, the provider's answer, and the tool trail.
* ``06_live_delivery.json`` -- the same facts, machine-readable.

Usage::

    # Dry report (no transmission)
    .venv/bin/python proposal/scripts/capture_live_send.py --channel email

    # Real send, explicitly approved
    .venv/bin/python proposal/scripts/capture_live_send.py --channel email --approve

"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = PROJECT_ROOT / "proposal" / "evidence"

TEST_RECIPIENTS = {
    "email": "martinchenonly1@gmail.com",
    "sms": "+6583536885",
}

REASON = "pilot acceptance check: verifying the delivery path end to end"


def revision() -> str:
    """
    Read the current commit for the record.

    Returns:
        Commit hash, or ``"unknown"`` outside a repository.
    """
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def send(channel: str, recipient: str) -> Dict[str, Any]:
    """
    Perform the send through the agent's own tool registry.

    Uses the registry rather than the provider clients directly, so what is
    evidence is exactly the path the agent takes, including allow-list checks,
    dry-run gating and audit recording.

    Args:
        channel: ``"email"`` or ``"sms"``.
        recipient: Destination address or E.164 number.

    Returns:
        ``{"tool": ..., "result": {...}, "audit_trail": [...]}``.
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    from tools.config import load_env_file
    from tools.messaging import MessagingToolkit

    # The tool layer reads configuration from the environment, so a process that
    # wants live sends must export .env itself. Without this the toolkit
    # correctly degrades to a simulated send.
    load_env_file(str(PROJECT_ROOT / ".env"))

    toolkit = MessagingToolkit()
    registry = toolkit.build_registry()

    if channel == "email":
        tool = "send_email"
        arguments = {
            "to_email": recipient,
            "subject": "Patient Follow-up Agent - delivery confirmation",
            "body": (
                "This message confirms that the Patient Follow-up Agent can "
                "deliver email through its tool layer.\n\n"
                "It was sent during a pilot acceptance check. No action is "
                "needed."
            ),
            "reason": REASON,
        }
    else:
        tool = "send_sms"
        arguments = {
            "phone_number": recipient,
            "message": "Patient Follow-up Agent: delivery confirmation test. No action needed.",
            "reason": REASON,
        }

    # Record which clinic identity actually transmitted, so the artifact names
    # the sender as well as the recipient.
    if channel == "email":
        sender = toolkit.config.aws_ses_source
    else:
        sender = toolkit.config.aws_sms_origination_identity

    result = registry.call(tool, arguments)
    return {
        "tool": tool,
        "channel": channel,
        "sender": sender,
        "recipient": recipient,
        "arguments": {k: v for k, v in arguments.items() if k != "body"},
        "result": json.loads(result.to_json()),
        "audit_trail": toolkit.audit_trail(),
    }


def render_markdown(payload: Mapping[str, Any]) -> str:
    """
    Render the live-delivery evidence as markdown.

    Args:
        payload: Evidence payload.

    Returns:
        Markdown document.
    """
    result = payload["send"]["result"]
    transmitted = result.get("status") in ("sent", "delivered") and not result.get("simulated")

    lines = [
        "# Deployment evidence 6 - Live end-to-end delivery",
        "",
        f"Captured: {payload['generated_at']}",
        f"Revision: `{payload['revision']}`",
        "",
        "## What this proves",
        "",
        "A message was handed to the real provider through the agent's own tool",
        "layer and the provider answered. This is the only artifact in the pack",
        "that evidences transmission rather than behaviour, which is why it is",
        "run only with explicit approval.",
        "",
        "## The send",
        "",
        f"- Tool: `{payload['send']['tool']}`",
        f"- Channel: {payload['send']['channel']}",
        f"- Recipient: `{payload['send']['recipient']}`",
        f"- Sender identity: `{payload['send'].get('sender') or 'provider default'}`",
        f"- Reason recorded: \"{payload['send']['arguments'].get('reason')}\"",
        "",
        "## Provider response",
        "",
        "```json",
        json.dumps(result, indent=2, ensure_ascii=False),
        "```",
        "",
    ]

    if transmitted:
        lines += [
            f"The provider accepted the message and returned message id",
            f"`{result.get('message_id')}`. Acceptance is not delivery: for email,",
            "the SES configuration set emits a separate `Delivery` event that is",
            "the authoritative record of arrival.",
            "",
        ]
    else:
        lines += [
            "The provider did **not** transmit this message. The structured",
            "outcome above is the evidence: the agent reported the failure",
            "instead of raising or claiming success, and the reason is",
            "recoverable from the record.",
            "",
        ]

    lines += [
        "## Tool audit trail",
        "",
        "Every attempt the toolkit made during this run, including the reason:",
        "",
        "```json",
        json.dumps(payload["send"]["audit_trail"], indent=2, ensure_ascii=False),
        "```",
        "",
        "## How to reproduce",
        "",
        "```bash",
        f".venv/bin/python proposal/scripts/capture_live_send.py "
        f"--channel {payload['send']['channel']} --approve",
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
    parser.add_argument("--channel", choices=["email", "sms"], default="email")
    parser.add_argument("--recipient", default=None, help="Override the test recipient.")
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Actually transmit. Without this the script reports and stops.",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Where evidence is written."
    )
    args = parser.parse_args(argv)

    recipient = args.recipient or TEST_RECIPIENTS[args.channel]

    if not args.approve:
        print(f"would send one {args.channel} to {recipient} through the tool layer")
        print("re-run with --approve to transmit and record the evidence")
        return 0

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "revision": revision(),
        "send": send(args.channel, recipient),
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    md_path = args.out_dir / "06_live_delivery.md"
    json_path = args.out_dir / "06_live_delivery.json"
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    result = payload["send"]["result"]
    print(f"wrote {md_path.relative_to(PROJECT_ROOT)}")
    print(f"wrote {json_path.relative_to(PROJECT_ROOT)}")
    print(f"  status={result.get('status')} message_id={result.get('message_id')}")
    print(f"  sender={payload['send'].get('sender') or 'provider default'}")

    if result.get("simulated"):
        print(
            "  NOT live evidence: the tool reported a simulated send. Check "
            "MESSAGING_DRY_RUN and credentials before presenting this artifact.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
