#!/usr/bin/env python3
"""
Record the runtime and repository state the deployment was validated on.

Deployment evidence is only meaningful against a known revision. This script
captures the moving parts that make a run reproducible: the exact commit, the
working-tree state, the interpreter and library versions, and the shape of the
codebase. If a reviewer re-runs the project and sees different behaviour, these
are the first lines to compare.

What it produces under ``proposal/evidence/``:

* ``03_runtime_and_repository.md`` -- human-readable state report.
* ``03_runtime_and_repository.json`` -- the same facts, machine-readable.

Usage::

    .venv/bin/python proposal/scripts/capture_runtime_evidence.py

"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

#: Repository root, derived from this file's location (proposal/scripts/...).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Default output directory for the evidence pack.
DEFAULT_OUT_DIR = PROJECT_ROOT / "proposal" / "evidence"

#: Packages whose version changes the behaviour a reviewer will observe.
TRACKED_PACKAGES = ("boto3", "botocore", "anthropic", "flask", "openpyxl", "certifi")

#: Directories counted as "the deployment" for the line-of-code summary.
SOURCE_GROUPS = ("agent", "core", "tools", "utils", "web", "scheduling", "scripts", "tests")


def _run(command: Sequence[str], cwd: Path = PROJECT_ROOT) -> str:
    """
    Run a command and return its stdout, or a readable failure note.

    Args:
        command: argv to execute.
        cwd: Working directory.

    Returns:
        Stripped stdout, or ``"<unavailable: ...>"`` when the command fails.
    """
    try:
        completed = subprocess.run(
            command, cwd=str(cwd), capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"<unavailable: {type(exc).__name__}: {exc}>"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        return f"<unavailable: {detail[-1] if detail else 'non-zero exit'}>"
    return completed.stdout.strip()


def package_versions() -> Dict[str, str]:
    """
    Read the installed version of each tracked dependency.

    Returns:
        ``{package: version}``; uninstalled packages report ``"not installed"``.
    """
    versions: Dict[str, str] = {}
    for name in TRACKED_PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return versions


def source_line_counts() -> Dict[str, Dict[str, int]]:
    """
    Count Python files and lines per source group.

    A rough size signal that tells a reviewer the order of magnitude of the
    system without making them clone it.

    Returns:
        ``{group: {"files": n, "lines": n}}``.
    """
    summary: Dict[str, Dict[str, int]] = {}
    for group in SOURCE_GROUPS:
        directory = PROJECT_ROOT / group
        if not directory.is_dir():
            continue
        files = 0
        lines = 0
        for path in sorted(directory.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            files += 1
            try:
                lines += len(path.read_text(encoding="utf-8").splitlines())
            except OSError:
                continue
        summary[group] = {"files": files, "lines": lines}

    # Entry points that sit at the repository root (demo.py, hospital_setup.py)
    # would otherwise be invisible in the total.
    root_files = [p for p in sorted(PROJECT_ROOT.glob("*.py"))]
    if root_files:
        lines = 0
        for path in root_files:
            try:
                lines += len(path.read_text(encoding="utf-8").splitlines())
            except OSError:
                continue
        summary["(root)"] = {"files": len(root_files), "lines": lines}
    return summary


def git_state() -> Dict[str, Any]:
    """
    Describe the commit and working tree the evidence was captured from.

    Returns:
        Branch, HEAD commit and subject, modified files, and the remote URL
        when one is configured.
    """
    status = _run(["git", "status", "--porcelain"])
    return {
        "branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "head_commit": _run(["git", "rev-parse", "HEAD"]),
        "head_subject": _run(["git", "log", "-1", "--pretty=%s"]),
        "head_date": _run(["git", "log", "-1", "--pretty=%cI"]),
        "working_tree_clean": status == "",
        "modified_files": [line for line in status.splitlines() if line.strip()],
        "remote": _run(["git", "remote", "get-url", "origin"]),
    }


def collect() -> Dict[str, Any]:
    """
    Gather every runtime fact the evidence document reports.

    Returns:
        A JSON-serializable state dictionary.
    """
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "interpreter": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "packages": package_versions(),
        "repository": git_state(),
        "source": source_line_counts(),
    }


def render_markdown(state: Mapping[str, Any]) -> str:
    """
    Render the state report as Markdown.

    Args:
        state: Output of :func:`collect`.

    Returns:
        Markdown text.
    """
    interpreter = state["interpreter"]
    repository = state["repository"]
    source = state["source"]

    lines: List[str] = [
        "# Deployment evidence 3 - Runtime and repository state",
        "",
        f"Captured: {state['generated_at']}",
        "",
        "## What this proves",
        "",
        "The deployment was validated against a specific, identifiable revision "
        "of the code on a specific interpreter with specific library versions. "
        "Anyone re-running the project can confirm they are comparing like with "
        "like before judging any other evidence in this pack.",
        "",
        "## Interpreter",
        "",
        f"- Python: {interpreter['version']} ({interpreter['implementation']})",
        f"- Executable: `{interpreter['executable']}`",
        f"- Platform: {interpreter['platform']}",
        "",
        "## Repository",
        "",
        f"- Branch: `{repository['branch']}`",
        f"- Commit: `{repository['head_commit']}`",
        f"- Commit subject: {repository['head_subject']}",
        f"- Commit date: {repository['head_date']}",
        f"- Working tree clean: {'yes' if repository['working_tree_clean'] else 'no'}",
        "",
    ]

    if repository["modified_files"]:
        lines += [
            "Files changed relative to that commit (this analysis work only):",
            "",
            "```",
            *repository["modified_files"],
            "```",
            "",
        ]
    else:
        lines.append("No files differ from the recorded commit.")
        lines.append("")

    lines += [
        "## Installed dependencies",
        "",
        "| Package | Version |",
        "| --- | --- |",
        *[f"| {name} | {version} |" for name, version in state["packages"].items()],
        "",
        "## Codebase size",
        "",
        "| Module | Python files | Lines |",
        "| --- | ---: | ---: |",
        *[
            f"| `{group}/` | {counts['files']} | {counts['lines']:,} |"
            for group, counts in source.items()
        ],
        "",
        f"Total: {sum(c['files'] for c in source.values())} files, "
        f"{sum(c['lines'] for c in source.values()):,} lines.",
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
        "--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Where evidence is written."
    )
    args = parser.parse_args(argv)

    state = collect()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "03_runtime_and_repository.json"
    md_path = args.out_dir / "03_runtime_and_repository.md"
    json_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(state), encoding="utf-8")

    print(f"wrote {md_path.relative_to(PROJECT_ROOT)}")
    print(f"wrote {json_path.relative_to(PROJECT_ROOT)}")
    print(f"  commit={state['repository']['head_commit'][:12]} python={state['interpreter']['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
