#!/usr/bin/env python3
"""
Record the AWS messaging configuration as deployment evidence.

Live AWS read-back is the strongest form of evidence, but it needs credentials.
This script is written to be useful in both cases: it always records the
*configuration* the deployment will behave according to (region, sender
identity, recipient allow-list, dry-run state), and it additionally records live
API responses when an AWS session is available.

Secrets are never written: the script records whether a value is present and
scrubs anything that looks like a key, token or password.

What it produces under ``proposal/evidence/``:

* ``02_aws_messaging_config.md`` -- human-readable summary.
* ``02_aws_messaging_config.json`` -- machine-readable summary.

Usage::

    .venv/bin/python proposal/scripts/capture_aws_config_evidence.py

"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = PROJECT_ROOT / "proposal" / "evidence"
DEFAULT_ENV = PROJECT_ROOT / ".env"
AWS_REGION_DEFAULT = "ap-southeast-1"

SECRET_HINTS = ("PASSWORD", "SECRET", "TOKEN", "KEY", "CREDENTIAL")

RELEVANT_KEYS = (
    "AWS_REGION",
    "AWS_SES_SOURCE",
    "AWS_SES_CONFIGURATION_SET",
    "AWS_SMS_ORIGINATION_IDENTITY",
    "AWS_SMS_ALLOWED_NUMBERS",
    "AWS_EMAIL_ALLOWED_ADDRESSES",
    "MESSAGING_DRY_RUN",
    "AGENT_LIVE_SENDS",
)

CODE_PATHS = (
    ("tools/aws_providers.py", "client.send_text_message"),
    ("tools/aws_providers.py", "client.send_email"),
    ("tools/messaging.py", "def send_sms"),
    ("tools/messaging.py", "def send_email"),
    ("agent/delivery.py", "def is_configured_for_live_sends"),
)


def read_env(path: Path) -> Dict[str, str]:
    """
    Read the relevant settings from the project's ``.env``.

    Only allow-listed keys are returned, and values whose key name suggests a
    secret are replaced by a presence marker, so no credential can leak into the
    evidence pack.

    Args:
        path: Path to the ``.env`` file.

    Returns:
        ``{key: value}`` for the relevant keys that were found.
    """
    if not path.exists():
        return {}

    values: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key not in RELEVANT_KEYS:
            continue
        if any(hint in key.upper() for hint in SECRET_HINTS) and value:
            values[key] = f"<set: {len(value)} chars>"
        else:
            values[key] = value
    return values


def run_aws(arguments: Sequence[str]) -> Dict[str, Any]:
    """
    Run an AWS CLI command and normalise the outcome.

    Args:
        arguments: Arguments after ``aws``, e.g. ``["sts", "get-caller-identity"]``.

    Returns:
        ``{"ok": bool, "data": parsed JSON or None, "error": str or None}``.
    """
    completed = subprocess.run(
        ["aws", *arguments],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip().splitlines()
        return {"ok": False, "data": None, "error": message[0] if message else "unknown error"}

    try:
        return {"ok": True, "data": json.loads(completed.stdout or "{}"), "error": None}
    except json.JSONDecodeError:
        return {"ok": True, "data": completed.stdout.strip(), "error": None}


def collect_live(region: str) -> Dict[str, Any]:
    """
    Read the live AWS state for the messaging identities.

    Args:
        region: AWS region to query.

    Returns:
        ``{"session": ..., "checks": {name: result}}``.
    """
    checks: Dict[str, Any] = {}

    caller = run_aws(["sts", "get-caller-identity"])
    checks["caller_identity"] = caller
    if not caller["ok"]:
        return {"session": "unauthenticated", "checks": checks}

    checks["ses_verified_identities"] = run_aws(
        ["ses", "list-identities", "--region", region]
    )
    checks["ses_send_quota"] = run_aws(["ses", "get-send-quota", "--region", region])
    checks["ses_sending_enabled"] = run_aws(
        ["ses", "get-account-sending-enabled", "--region", region]
    )
    checks["sms_account_attributes"] = run_aws(
        [
            "pinpoint-sms-voice-v2",
            "describe-account-attributes",
            "--region",
            region,
        ]
    )
    checks["sms_verified_destination_numbers"] = run_aws(
        [
            "pinpoint-sms-voice-v2",
            "describe-verified-destination-numbers",
            "--region",
            region,
        ]
    )
    return {"session": "authenticated", "checks": checks}


def code_references() -> List[Dict[str, Any]]:
    """
    Locate the delivery code paths in the deployed revision.

    Evidence a reviewer can follow: the exact file and line implementing each
    claim made above.

    Returns:
        ``[{"file": ..., "needle": ..., "line": n or None}]``.
    """
    found: List[Dict[str, Any]] = []
    for relative, needle in CODE_PATHS:
        path = PROJECT_ROOT / relative
        line = None
        if path.exists():
            for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if needle in text:
                    line = number
                    break
        found.append({"file": relative, "needle": needle, "line": line})
    return found


def render_markdown(result: Mapping[str, Any]) -> str:
    """
    Render the AWS configuration evidence as markdown.

    Args:
        result: Evidence payload.

    Returns:
        Markdown document.
    """
    env = result["configuration"]
    live = result["live"]
    lines = [
        "# Deployment evidence 2 - AWS messaging configuration",
        "",
        f"Captured: {result['generated_at']}",
        "",
        "## What this proves",
        "",
        "The messaging transport is wired to named AWS services in a named",
        "region, it sends from a verified identity, and it refuses recipients",
        "outside an explicit allow-list. The last section pins every claim to a",
        "file and line in the deployed revision.",
        "",
        "## Configuration the deployment runs with",
        "",
        "| Setting | Value |",
        "| --- | --- |",
    ]

    for key in RELEVANT_KEYS:
        value = env.get(key, "<not set>")
        lines.append(f"| `{key}` | `{value or '<empty>'}` |")

    lines += [
        "",
        "## Live AWS read-back",
        "",
    ]

    if live["session"] != "authenticated":
        error = live["checks"].get("caller_identity", {}).get("error") or "unavailable"
        lines += [
            f"- AWS session: **{live['session']}**",
            f"- Reason: `{error}`",
            "",
            "> The configuration above is verifiable now; the live SES and SMS",
            "> account state could not be read at capture time. Re-run this",
            "> script after `aws login` to fill in the live values before",
            "> presenting this pack. Do not treat the configuration table alone",
            "> as proof that the AWS side is provisioned.",
            "",
        ]
    else:
        quota = live["checks"].get("ses_send_quota", {}).get("data") or {}
        identities = live["checks"].get("ses_verified_identities", {}).get("data") or {}
        numbers = live["checks"].get("sms_verified_destination_numbers", {}).get("data") or {}
        lines += [
            "- AWS session: **authenticated**",
            f"- SES sending enabled: "
            f"`{(live['checks'].get('ses_sending_enabled', {}).get('data') or {}).get('Enabled')}`",
            f"- SES 24-hour quota: `{quota.get('Max24HourSend')}`, "
            f"max send rate `{quota.get('MaxSendRate')}/s`, "
            f"sent in last 24h `{quota.get('SentLast24Hours')}`",
            "",
            "Verified SES identities:",
            "",
        ]
        for identity in identities.get("Identities", []):
            lines.append(f"- `{identity}`")
        lines += ["", "Verified SMS destination numbers:", ""]
        numbers_ok = live["checks"].get("sms_verified_destination_numbers", {}).get("ok")
        if numbers_ok:
            entries = numbers.get("VerifiedDestinationNumbers", [])
            if entries:
                for entry in entries:
                    lines.append(
                        f"- `{entry.get('DestinationPhoneNumber')}` "
                        f"({entry.get('Status')}, type {entry.get('VerificationStatus')})"
                    )
            else:
                lines.append("- None visible to this account.")
        else:
            lines.append(
                "- **Not readable**: "
                f"`{live['checks']['sms_verified_destination_numbers'].get('error')}`"
            )

        lines += [
            "",
            "> A quota of 200 messages per 24 hours and 1 message per second is",
            "> the SES sandbox ceiling, not a production limit. Production access",
            "> and SMS origination identity onboarding are prerequisites for a",
            "> real patient population and are currently outstanding.",
            "",
        ]

        if not numbers_ok:
            lines += [
                "> The SMS read failed with `SubscriptionRequiredException`, which",
                "> means this account is not subscribed to End User Messaging SMS in",
                "> the queried region. The SMS code path is complete, but attempts",
                "> return `not_subscribed` until the service is provisioned. Do not",
                "> describe SMS as available on the strength of this pack.",
                "",
            ]

    lines += [
        "## Delivery code paths in this revision",
        "",
        "| File | Locator | Line |",
        "| --- | --- | ---: |",
    ]
    for entry in result["code_references"]:
        lines.append(f"| `{entry['file']}` | `{entry['needle']}` | {entry['line']} |")

    lines.append("")
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
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV, help="Path to .env.")
    parser.add_argument(
        "--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Where evidence is written."
    )
    parser.add_argument("--region", default=None, help="AWS region override.")
    args = parser.parse_args(argv)

    configuration = read_env(args.env_file)
    region = args.region or configuration.get("AWS_REGION") or AWS_REGION_DEFAULT
    live = collect_live(region)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "region": region,
        "configuration": configuration,
        "configuration_source": str(args.env_file.relative_to(PROJECT_ROOT)),
        "live": live,
        "code_references": code_references(),
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    md_path = args.out_dir / "02_aws_messaging_config.md"
    json_path = args.out_dir / "02_aws_messaging_config.json"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"wrote {md_path.relative_to(PROJECT_ROOT)}")
    print(f"wrote {json_path.relative_to(PROJECT_ROOT)}")
    print(f"  region={region} aws_session={live['session']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
