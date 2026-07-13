#!/usr/bin/env python3
"""Provenance guard for the hand-maintained Word copy of the project overview.

``docs/project_overview.docx`` is a curated Word rendering of the canonical
Markdown ``docs/project_overview.md`` (same content, embedded diagram images,
shareable outside the repo). Because it is edited by hand rather than generated,
it can silently drift from the Markdown — which it has, repeatedly.

This tool makes that drift loud instead of silent. It records the Markdown's
content hash inside the docx (in the core-properties ``comments`` field) and
checks, on demand and in the test suite, that the stored hash still matches the
current Markdown. The guarantee is provenance, not byte-identity: the docx
declares which revision of the Markdown it was last synced against, and the gate
fails the moment the Markdown moves on without a re-sync.

Workflow: edit ``project_overview.md`` -> update the docx prose/figures to match
-> ``python tools/check_overview_sync.py --stamp`` -> commit both. The default
(no ``--stamp``) is the check the test runs.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MD_PATH = REPO_ROOT / "docs" / "project_overview.md"
DOCX_PATH = REPO_ROOT / "docs" / "project_overview.docx"
STAMP_PREFIX = "source: project_overview.md sha256="


def require_docx():
    try:
        from docx import Document  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("python-docx is required to sync-check the overview docx.") from exc
    return Document


def md_fingerprint(md_path: Path = MD_PATH) -> str:
    # Normalize line endings so a CRLF/LF checkout difference is not drift.
    text = md_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stamped_fingerprint(docx_path: Path = DOCX_PATH) -> str | None:
    Document = require_docx()
    comments = Document(str(docx_path)).core_properties.comments or ""
    for line in comments.splitlines():
        if line.startswith(STAMP_PREFIX):
            return line[len(STAMP_PREFIX) :].strip()
    return None


def stamp(docx_path: Path = DOCX_PATH, md_path: Path = MD_PATH) -> str:
    Document = require_docx()
    doc = Document(str(docx_path))
    fingerprint = md_fingerprint(md_path)
    doc.core_properties.comments = f"{STAMP_PREFIX}{fingerprint}"
    doc.save(str(docx_path))
    return fingerprint


def check(docx_path: Path = DOCX_PATH, md_path: Path = MD_PATH) -> list[str]:
    if not docx_path.is_file():
        return [f"missing {docx_path.relative_to(REPO_ROOT)}"]
    current = md_fingerprint(md_path)
    stored = stamped_fingerprint(docx_path)
    if stored is None:
        return ["docx carries no source stamp; run `python tools/check_overview_sync.py --stamp`"]
    if stored != current:
        return [
            "project_overview.docx is stale: it was synced against a different "
            "project_overview.md. Update the docx to match, then run "
            "`python tools/check_overview_sync.py --stamp`.",
        ]
    return []


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp", action="store_true", help="record the current Markdown hash into the docx")
    args = parser.parse_args(argv)

    if args.stamp:
        fingerprint = stamp()
        print(f"stamped project_overview.docx with md sha256={fingerprint[:12]}...")
        return 0

    errors = check()
    if errors:
        for error in errors:
            print(f"overview sync: {error}")
        return 1
    print("overview sync: OK (docx matches project_overview.md)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
