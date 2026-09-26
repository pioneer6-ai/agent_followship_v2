#!/usr/bin/env python3
"""
Render the consolidated deployment evidence pack to PDF.

The pack under ``proposal/evidence/`` is a set of eight reports plus their raw
artifacts: seven captured by script, and one -- report 8's mailbox screenshot --
contributed by the operator, which no script can reproduce. Each report is readable
on its own, but a reviewer deciding on a deployment wants them in one place, with
the figures restated and the artifact hashes re-checked. This script builds that
document.

It reuses the proposal's pipeline rather than copying it: ``build_pdf.py`` does
markdown -> styled HTML (pandoc) -> PDF (headless Chrome) with the shared
print stylesheet, and verifies the result. Only the source markdown, the
destination and the document title differ.

What it produces:

* ``proposal/Patient_Followup_Agent_Deployment_Evidence.pdf`` -- the document.
* ``proposal/build/deployment_evidence.html`` -- the intermediate HTML, kept for
  inspection.

Usage::

    .venv/bin/python proposal/scripts/build_deployment_evidence_pdf.py

"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_pdf import BUILD_DIR, PROJECT_ROOT, to_html, to_pdf, verify  # noqa: E402

DEFAULT_SOURCE = PROJECT_ROOT / "proposal" / "deployment_evidence.md"
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "proposal" / "Patient_Followup_Agent_Deployment_Evidence.pdf"
)
TITLE = "Patient Follow-up Agent - Deployment Evidence"


def main(argv: Sequence[str] | None = None) -> int:
    """
    Command-line entry point.

    Args:
        argv: Unused; accepted so the signature matches ``build_pdf.main``.

    Returns:
        Process exit code.
    """
    source = DEFAULT_SOURCE
    output = DEFAULT_OUTPUT

    if not source.exists():
        print(f"missing source: {source}", file=sys.stderr)
        return 1

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    html_path = BUILD_DIR / "deployment_evidence.html"

    to_html(source, html_path, title=TITLE)
    if not html_path.exists() or html_path.stat().st_size < 1000:
        print("pandoc produced no usable HTML", file=sys.stderr)
        return 1

    result = to_pdf(html_path, output)
    pages = verify(output)
    size = output.stat().st_size

    print(f"wrote {output.relative_to(PROJECT_ROOT)}")
    print(f"  {size:,} bytes, {pages} pages")
    print(f"  html: {html_path.relative_to(PROJECT_ROOT)}")
    if "error" in (result.stderr or "").lower() and pages == 0:
        print(result.stderr.strip()[-500:], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
