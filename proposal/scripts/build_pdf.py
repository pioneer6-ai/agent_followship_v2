#!/usr/bin/env python3
"""
Render the proposal to PDF.

The pipeline is markdown -> styled HTML -> PDF. Pandoc does the markdown
conversion and headless Chrome does the printing, which avoids depending on a
LaTeX toolchain being installed while still producing a typeset document with
page numbers-free, print-correct output.

The script verifies the result rather than assuming success: it checks the PDF
magic bytes, counts pages, and fails loudly if the render produced nothing.

What it produces:

* ``proposal/Patient_Followup_Agent_Proposal.pdf`` -- the document.
* ``proposal/build/proposal.html`` -- the intermediate HTML, kept for inspection.

Usage::

    .venv/bin/python proposal/scripts/build_pdf.py

"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = PROJECT_ROOT / "proposal" / "proposal.md"
DEFAULT_OUTPUT = PROJECT_ROOT / "proposal" / "Patient_Followup_Agent_Proposal.pdf"
BUILD_DIR = PROJECT_ROOT / "proposal" / "build"
CSS = PROJECT_ROOT / "proposal" / "assets" / "proposal.css"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def to_html(
    source: Path,
    destination: Path,
    title: str = "Patient Follow-up Agent - Proposal",
) -> None:
    """
    Convert a markdown document to a standalone HTML file.

    Args:
        source: Markdown file.
        destination: HTML file to write.
        title: Document title for pandoc's metadata block.

    Raises:
        RuntimeError: If pandoc fails.
    """
    completed = subprocess.run(
        [
            "pandoc",
            str(source),
            "--from",
            "markdown",
            "--to",
            "html5",
            "--standalone",
            "--embed-resources",
            "--css",
            str(CSS),
            "--resource-path",
            f"{source.parent}:{PROJECT_ROOT}",
            "--metadata",
            f"title={title}",
            "--output",
            str(destination),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"pandoc failed: {completed.stderr.strip()}")


def to_pdf(html: Path, destination: Path) -> subprocess.CompletedProcess:
    """
    Print the HTML to PDF with headless Chrome.

    Args:
        html: HTML file to print.
        destination: PDF path to write.

    Returns:
        The completed process, for its stderr (Chrome is noisy but harmless).
    """
    return subprocess.run(
        [
            CHROME,
            "--headless",
            "--disable-gpu",
            "--no-pdf-header-footer",
            "--virtual-time-budget=8000",
            f"--print-to-pdf={destination}",
            html.as_uri(),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )


def verify(pdf: Path) -> int:
    """
    Check that a real, non-trivial PDF was produced.

    Args:
        pdf: Candidate PDF.

    Returns:
        Page count as counted from the PDF page objects.

    Raises:
        RuntimeError: If the file is missing or is not a PDF.
    """
    if not pdf.exists():
        raise RuntimeError("no PDF was produced")

    data = pdf.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise RuntimeError(f"output is not a PDF (starts with {data[:8]!r})")
    if len(data) < 20_000:
        raise RuntimeError(f"PDF is suspiciously small ({len(data)} bytes)")

    return len(re.findall(rb"/Type\s*/Page[^s]", data)) or len(
        re.findall(rb"/Type\s*/Page\b", data)
    )


def main(argv: Sequence[str] | None = None) -> int:
    """
    Command-line entry point.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Markdown source.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="PDF destination.")
    parser.add_argument(
        "--html-output",
        type=Path,
        default=BUILD_DIR / "proposal.html",
        help="Intermediate HTML destination.",
    )
    parser.add_argument(
        "--title",
        default="Patient Follow-up Agent - Proposal",
        help="Document title passed to pandoc's metadata block.",
    )
    args = parser.parse_args(argv)

    # Resolve all paths: pandoc and Chrome both need absolute paths (``as_uri``
    # rejects relative ones), and the progress messages report them relative to
    # the project root.
    args.source = args.source.resolve()
    args.output = args.output.resolve()
    args.html_output = args.html_output.resolve()

    html_path = args.html_output
    html_path.parent.mkdir(parents=True, exist_ok=True)

    to_html(args.source, html_path, title=args.title)
    if not html_path.exists() or html_path.stat().st_size < 1000:
        print("pandoc produced no usable HTML", file=sys.stderr)
        return 1

    result = to_pdf(html_path, args.output)
    pages = verify(args.output)
    size = args.output.stat().st_size

    print(f"wrote {args.output.relative_to(PROJECT_ROOT)}")
    print(f"  {size:,} bytes, {pages} pages")
    print(f"  html: {html_path.relative_to(PROJECT_ROOT)}")
    if "error" in (result.stderr or "").lower() and pages == 0:
        print(result.stderr.strip()[-500:], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
