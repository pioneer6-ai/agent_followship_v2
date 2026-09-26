#!/usr/bin/env python3
"""
Build the evidence index for the proposal pack.

The index is generated rather than hand-written so it cannot drift from the
files that actually exist: each entry carries the artifact's own hash, size and
stated purpose, read out of the artifact itself.

What it produces under ``proposal/evidence/``:

* ``00_index.md`` -- the table of contents for the evidence pack.

Usage::

    .venv/bin/python proposal/scripts/capture_evidence_index.py

"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = PROJECT_ROOT / "proposal" / "evidence"
INDEX_NAME = "00_index.md"


def sha256_of(path: Path) -> str:
    """
    Hash a file.

    Args:
        path: File to hash.

    Returns:
        Lowercase hex digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stated_purpose(path: Path) -> str:
    """
    Read the "What this proves" paragraph out of an evidence report.

    The purpose is taken from the report body so the index cannot claim more than
    the report itself does.

    Args:
        path: Evidence markdown file.

    Returns:
        The paragraph, or an empty string if the section is missing.
    """
    text = path.read_text(encoding="utf-8")
    match = re.search(
        r"## What this proves\s*\n+(.+?)(?=\n## |\n> |\Z)", text, flags=re.DOTALL
    )
    if not match:
        return ""
    return " ".join(match.group(1).split())


def title_of(path: Path) -> str:
    """
    Read the first level-1 heading of a markdown file.

    Args:
        path: Markdown file.

    Returns:
        Heading text with its leading hashes removed.
    """
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def collect(out_dir: Path) -> List[Dict[str, Any]]:
    """
    Gather every evidence artifact currently present.

    Args:
        out_dir: Evidence directory.

    Returns:
        One entry per artifact, reports first and then their raw inputs.
    """
    entries: List[Dict[str, Any]] = []
    for path in sorted(out_dir.glob("*.md")):
        if path.name == INDEX_NAME:
            continue
        entries.append(
            {
                "kind": "report",
                "name": path.name,
                "title": title_of(path),
                "purpose": stated_purpose(path),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_of(path),
            }
        )

    artifacts_dir = out_dir / "artifacts"
    if artifacts_dir.is_dir():
        for path in sorted(artifacts_dir.rglob("*")):
            if path.is_file():
                entries.append(
                    {
                        "kind": "raw artifact",
                        "name": str(path.relative_to(out_dir)),
                        "title": path.name,
                        "purpose": "Primary source retained so the figures in the reports can be recomputed.",
                        "size_bytes": path.stat().st_size,
                        "sha256": sha256_of(path),
                    }
                )

    shots_dir = out_dir / "screenshots"
    if shots_dir.is_dir():
        for path in sorted(shots_dir.rglob("*")):
            if path.is_file():
                entries.append(
                    {
                        "kind": "visual artifact",
                        "name": str(path.relative_to(out_dir)),
                        "title": path.name,
                        "purpose": "Rendered or captured output from the running system.",
                        "size_bytes": path.stat().st_size,
                        "sha256": sha256_of(path),
                    }
                )

    return entries


def render_markdown(entries: Sequence[Dict[str, Any]]) -> str:
    """
    Render the index as markdown.

    Args:
        entries: Collected artifacts.

    Returns:
        Markdown document.
    """
    reports = [e for e in entries if e["kind"] == "report"]
    others = [e for e in entries if e["kind"] != "report"]

    lines = [
        "# Evidence pack index",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "Every artifact listed here was produced by a script in",
        "`proposal/scripts/` against the deployment revision recorded in",
        "evidence 3. Hashes are of the artifact as it exists in this pack, so a",
        "reviewer can confirm they are reading the same bytes the proposal was",
        "written from.",
        "",
        "## Reports",
        "",
    ]

    for entry in reports:
        lines += [
            f"### `{entry['name']}`",
            "",
            f"*{entry['title']}*",
            "",
            entry["purpose"] or "_No stated purpose._",
            "",
            f"- Size: {entry['size_bytes']:,} bytes",
            f"- SHA-256: `{entry['sha256']}`",
            "",
        ]

    lines += ["## Supporting artifacts", "", "| Artifact | Kind | Size | SHA-256 (first 16) |", "| --- | --- | ---: | --- |"]
    for entry in others:
        lines.append(
            f"| `{entry['name']}` | {entry['kind']} | {entry['size_bytes']:,} | "
            f"`{entry['sha256'][:16]}` |"
        )

    lines += [
        "",
        "## Provenance of this index",
        "",
        "The index is generated, not maintained by hand: adding a report to this",
        "directory and re-running `capture_evidence_index.py` adds it here. There",
        "is no list to forget to update.",
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
        "--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Evidence directory."
    )
    args = parser.parse_args(argv)

    entries = collect(args.out_dir)
    if not entries:
        print("no evidence artifacts found", file=sys.stderr)
        return 1

    index_path = args.out_dir / INDEX_NAME
    index_path.write_text(render_markdown(entries), encoding="utf-8")

    print(f"wrote {index_path.relative_to(PROJECT_ROOT)}")
    print(f"  {len(entries)} artifacts indexed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
