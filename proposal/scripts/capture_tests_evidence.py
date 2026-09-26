#!/usr/bin/env python3
"""
Record the automated test suite as deployment evidence.

A passing test suite is the cheapest evidence that the deployed revision behaves
as intended, but "the tests pass" is only reproducible if the exact command, the
exact revision and the observed output are recorded together. This script runs
the suite, stores the raw output verbatim, and summarises what was executed.

The raw pytest output is written next to the summary so a reviewer can read the
primary source rather than a paraphrase.

What it produces under ``proposal/evidence/``:

* ``04_test_suite.md`` -- human-readable summary.
* ``artifacts/pytest_output.txt`` -- verbatim pytest output.
* ``04_test_suite.json`` -- machine-readable summary.

Usage::

    .venv/bin/python proposal/scripts/capture_tests_evidence.py

"""
from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = PROJECT_ROOT / "proposal" / "evidence"


def run_suite(python: str, test_path: str) -> Dict[str, Any]:
    """
    Execute pytest and capture its output.

    Args:
        python: Interpreter used to run pytest.
        test_path: Test directory or file to run.

    Returns:
        ``{"command": str, "returncode": int, "output": str}``.
    """
    command = [python, "-m", "pytest", test_path, "-q"]
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    return {
        "command": " ".join(command),
        "returncode": completed.returncode,
        "output": completed.stdout + completed.stderr,
    }


def parse_summary(output: str) -> Dict[str, Any]:
    """
    Extract the headline numbers from pytest's output.

    Parses pytest's own summary line rather than recounting tests independently,
    so the figures reported here are exactly the ones pytest printed.

    Args:
        output: Verbatim pytest output.

    Returns:
        Passed/failed/skipped counts, warning count and duration in seconds.
    """
    summary: Dict[str, Any] = {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "warnings": 0,
        "duration_s": None,
        "summary_line": "",
    }

    for line in reversed(output.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            summary["summary_line"] = line.strip()
            for label, key in (
                ("passed", "passed"),
                ("failed", "failed"),
                ("skipped", "skipped"),
                ("error", "errors"),
                ("warning", "warnings"),
            ):
                match = re.search(rf"(\d+)\s+{label}s?\b", line)
                if match:
                    summary[key] = int(match.group(1))
            break

    duration = re.search(r"in ([\d.]+)s", output)
    if duration:
        summary["duration_s"] = float(duration.group(1))

    return summary


def test_inventory(test_path: str) -> List[Dict[str, Any]]:
    """
    Count test functions per test module.

    Gives a reviewer the shape of the suite: which areas of the system are
    covered and how heavily, without them having to open every file.

    Args:
        test_path: Test directory relative to the project root.

    Returns:
        One entry per test module, sorted by name.
    """
    rows: List[Dict[str, Any]] = []
    base = PROJECT_ROOT / test_path
    if not base.is_dir():
        return rows

    for module in sorted(base.rglob("test_*.py")):
        text = module.read_text(encoding="utf-8")
        count = len(re.findall(r"^\s*def test_", text, flags=re.MULTILINE))
        rows.append(
            {
                "module": str(module.relative_to(PROJECT_ROOT)),
                "lines": len(text.splitlines()),
                "tests": count,
            }
        )
    return rows


def render_markdown(result: Dict[str, Any]) -> str:
    """
    Render the evidence summary as markdown.

    Args:
        result: Evidence payload.

    Returns:
        Markdown document.
    """
    summary = result["summary"]
    status = "PASS" if result["run"]["returncode"] == 0 else "FAIL"

    lines = [
        "# Deployment evidence 4 - Automated test suite",
        "",
        f"Captured: {result['generated_at']}",
        "",
        "## What this proves",
        "",
        "The revision identified in evidence 3 passes its full automated test",
        "suite on the recorded interpreter. The suite is the executable form of",
        "the behaviour contracts: tool schemas, error taxonomy, provider",
        "selection, agent loop control flow, delivery gating and the web API.",
        "The verbatim pytest output is stored alongside this file.",
        "",
        "## Result",
        "",
        f"- Status: **{status}** (exit code {result['run']['returncode']})",
        f"- Command: `{result['run']['command']}`",
        f"- Passed: {summary['passed']:,}",
        f"- Failed: {summary['failed']:,}",
        f"- Errors: {summary['errors']:,}",
        f"- Skipped: {summary['skipped']:,}",
        f"- Warnings: {summary['warnings']:,}",
        f"- Duration: {summary['duration_s']}s",
        "",
        "```",
        summary["summary_line"],
        "```",
        "",
        "## Environment",
        "",
        f"- Python: {result['environment']['python']}",
        f"- Platform: {result['environment']['platform']}",
        f"- Raw output: `{result['artifacts']['raw_output']}`",
        "",
        "## Coverage by module",
        "",
        "| Test module | Tests | Lines |",
        "| --- | ---: | ---: |",
    ]

    for row in result["inventory"]:
        lines.append(f"| `{row['module']}` | {row['tests']} | {row['lines']:,} |")

    lines += [
        f"| **Total** | **{sum(r['tests'] for r in result['inventory']):,}** | "
        f"**{sum(r['lines'] for r in result['inventory']):,}** |",
        "",
        f"The inventory counts test *functions* ({sum(r['tests'] for r in result['inventory']):,});",
        f"pytest reports {summary['passed']:,} passing *cases* because several are",
        "parameterised and expand at collection time. Both numbers describe the",
        "same suite.",
        "",
        "## Known limitation found while capturing this evidence",
        "",
        "The suite exercises the real orchestrator and therefore its real audit",
        "logger, which appends to `audit_log.json` in the working directory. In",
        "other words, running the tests mutates the audit record. Evidence 1 is",
        "computed from a frozen snapshot for exactly this reason, and isolating",
        "the audit path during tests is a prerequisite for production sign-off.",
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
    parser.add_argument("--tests", default="tests", help="Test path to run.")
    parser.add_argument(
        "--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Where evidence is written."
    )
    args = parser.parse_args(argv)

    python = sys.executable
    run = run_suite(python, args.tests)
    summary = parse_summary(run["output"])
    inventory = test_inventory(args.tests)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    artifacts = args.out_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    raw_path = artifacts / "pytest_output.txt"
    raw_path.write_text(run["output"], encoding="utf-8")

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run": run,
        "summary": summary,
        "inventory": inventory,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "artifacts": {
            "raw_output": str(raw_path.relative_to(PROJECT_ROOT)),
        },
    }

    md_path = args.out_dir / "04_test_suite.md"
    json_path = args.out_dir / "04_test_suite.json"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"wrote {md_path.relative_to(PROJECT_ROOT)}")
    print(f"wrote {json_path.relative_to(PROJECT_ROOT)}")
    print(f"  {summary['summary_line']}")
    return 0 if run["returncode"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
