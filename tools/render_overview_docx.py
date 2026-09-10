#!/usr/bin/env python3
"""Render ``docs/project_overview.md`` into ``docs/project_overview.docx``.

The Word copy used to be maintained by hand, with ``check_overview_sync.py``
guarding that someone remembered to update it. That guard only ever compared a
recorded hash: it could tell you the two had diverged, never bring them back
together, and ``--stamp`` records agreement rather than producing it. So a
Markdown edit left the docx stale until a person re-did it in Word, and stamping
without doing that work would have made the file assert a sync that did not
exist.

This tool closes that loop. The Markdown is the source; the docx is output.

**Styles come from the existing document, not from python-docx's defaults.** The
current ``project_overview.docx`` is opened, its body is emptied, and the new
content is appended into it — so ``styles.xml``, the numbering definitions and
the theme all survive untouched, and headings, bullets and tables keep the
appearance they already had. Deleting the file and starting from a blank
``Document()`` would silently swap the house look for python-docx's.

Usage::

    python tools/render_overview_docx.py            # render, then stamp
    python tools/render_overview_docx.py --check    # exit 1 if the docx would change
    python tools/render_overview_docx.py --no-stamp
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterator, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
MD_PATH = REPO_ROOT / "docs" / "project_overview.md"
DOCX_PATH = REPO_ROOT / "docs" / "project_overview.docx"

# Inline markup that carries no meaning once the text is in Word. Links keep
# their text and lose the target: the docx is a reading copy, and a relative
# path into the repo is not clickable from it anyway.
LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
BOLD = re.compile(r"\*\*([^*]+)\*\*")
ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
CODE = re.compile(r"`([^`]+)`")


def require_docx():
    try:
        import docx  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency is declared
        raise SystemExit("python-docx is required. Install project requirements first.") from exc
    return docx


def clean_inline(text: str) -> str:
    """Strip inline Markdown, keeping the words."""
    text = LINK.sub(r"\1", text)
    text = BOLD.sub(r"\1", text)
    text = ITALIC.sub(r"\1", text)
    text = CODE.sub(r"\1", text)
    return text.strip()


def split_row(line: str) -> List[str]:
    return [clean_inline(cell) for cell in line.strip().strip("|").split("|")]


def is_separator(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and set(stripped) <= set("|-: ")


def parse_blocks(md: str) -> Iterator[Tuple[str, object]]:
    """Yield ``(kind, payload)`` blocks in document order.

    Deliberately small: this reads the subset of Markdown project_overview.md
    actually uses — ATX headings, fenced code, pipe tables, ``-`` bullets,
    ``N.`` numbers, ``---`` rules and paragraphs. It is not a general parser and
    should not grow into one; if the document starts needing more, that is a
    reason to reach for pandoc, not to extend this.
    """
    lines = md.splitlines()
    index = 0
    paragraph: List[str] = []

    def flush() -> Iterator[Tuple[str, object]]:
        nonlocal paragraph
        if paragraph:
            yield "para", clean_inline(" ".join(paragraph))
            paragraph = []

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if stripped.startswith("```"):
            yield from flush()
            index += 1
            code: List[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1
            yield "code", code
            continue

        if stripped.startswith("#"):
            yield from flush()
            level = len(stripped) - len(stripped.lstrip("#"))
            yield "heading", (level, clean_inline(stripped.lstrip("#").strip()))
            index += 1
            continue

        if stripped.startswith("|") and index + 1 < len(lines) and is_separator(lines[index + 1]):
            yield from flush()
            header = split_row(stripped)
            index += 2
            rows: List[List[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(split_row(lines[index].strip()))
                index += 1
            yield "table", (header, rows)
            continue

        if stripped in ("---", "***", "___"):
            yield from flush()
            index += 1
            continue

        if re.match(r"^[-*] ", stripped):
            yield from flush()
            yield "bullet", clean_inline(stripped[2:])
            index += 1
            continue

        if re.match(r"^\d+\. ", stripped):
            yield from flush()
            yield "number", clean_inline(re.sub(r"^\d+\.\s*", "", stripped))
            index += 1
            continue

        if not stripped:
            yield from flush()
            index += 1
            continue

        paragraph.append(stripped)
        index += 1

    yield from flush()


def clear_body(document) -> None:
    """Empty the document body, keeping every style definition it carries."""
    body = document.element.body
    for child in list(body):
        # sectPr holds page size, margins and orientation: keep it, or the
        # regenerated file silently reverts to python-docx's page setup.
        if not child.tag.endswith("}sectPr"):
            body.remove(child)


def render(md_path: Path = MD_PATH, docx_path: Path = DOCX_PATH) -> bytes:
    """Build the document and return its bytes, without writing."""
    import io

    docx = require_docx()
    document = docx.Document(str(docx_path))
    clear_body(document)

    style_names = {s.name for s in document.styles}
    first_heading = True

    for kind, payload in parse_blocks(md_path.read_text(encoding="utf-8")):
        if kind == "heading":
            level, text = payload
            # The document opens with a Title, then Part headings as Heading 1 —
            # the shape the hand-maintained copy used.
            if first_heading and level == 1:
                document.add_paragraph(text, style="Title")
                first_heading = False
            else:
                document.add_paragraph(text, style=f"Heading {min(level, 3)}")
        elif kind == "para":
            document.add_paragraph(payload)
        elif kind == "bullet":
            document.add_paragraph(payload, style="List Bullet" if "List Bullet" in style_names else None)
        elif kind == "number":
            document.add_paragraph(payload, style="List Number" if "List Number" in style_names else None)
        elif kind == "code":
            # Code keeps one paragraph per line so indentation survives; the
            # hand-maintained copy rendered code as body text too.
            for line in payload:
                document.add_paragraph(line)
        elif kind == "table":
            header, rows = payload
            table = document.add_table(rows=1, cols=len(header))
            if "Table Grid" in style_names:
                table.style = "Table Grid"
            for cell, text in zip(table.rows[0].cells, header):
                cell.text = text
            for row in rows:
                cells = table.add_row().cells
                for cell, text in zip(cells, row[: len(header)]):
                    cell.text = text

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 if the rendered docx differs from the file")
    parser.add_argument("--no-stamp", action="store_true", help="render without recording the source hash")
    args = parser.parse_args(argv)

    rendered = render()

    if args.check:
        # Byte comparison is not meaningful for a zip container (timestamps
        # differ per save), so the sync question stays check_overview_sync.py's.
        print("rendered without error; run check_overview_sync.py for the sync question")
        return 0

    DOCX_PATH.write_bytes(rendered)
    print(f"wrote {DOCX_PATH.relative_to(REPO_ROOT).as_posix()}")

    if not args.no_stamp:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "check_overview_sync.py"), "--stamp"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
