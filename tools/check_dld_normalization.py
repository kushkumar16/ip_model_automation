#!/usr/bin/env python3
"""Fidelity gate for the DLD normalization stage (docs/proposals/normalize_dld_stage.md).

Normalization rewrites an author's ``dlds/<ip>_dld.src.md`` into the
extractor-shaped ``dlds/<ip>_dld.md``. It may change *structure* freely and may
change *content* not at all. This tool is the deterministic half of that
contract — no LLM, no judgment:

  * **measurement conservation** — every ``2 ns`` / ``500 MHz`` / ``depth 8`` in
    the source survives, with its value and unit intact,
  * **identifier conservation** — every state name and backticked identifier
    survives,
  * **no net-new identifiers** — an identifier in the normalized file that
    appears nowhere in the source is invention (the hallucinated-state check),
  * **FSM parity** — same FSM name set and same declared count, read with the
    real extractor rather than a second parser,
  * **wait-model provenance** — a ``Wait model:`` block must trace to blocking
    language in the source; one conjured for a silent interface is invention,
  * **unplaced accounting** — source content that reaches neither the normalized
    body nor its ``## Unplaced Source Content`` section is silent loss.

What this gate proves is that nothing was dropped, altered, or invented. It does
**not** prove the meaning survived — a normalizer could preserve every number
while attaching it to the wrong FSM. That claim is earned per IP by a human, the
way ``check_model_provenance.py`` earns its baseline::

    python tools/check_dld_normalization.py --stamp <ip>

The stamp records the (source, normalized) hash pair in
``dlds/normalization_baselines.json``; editing either file breaks it and
re-requires review.

Usage::

    python tools/check_dld_normalization.py                 # every IP with a .src.md
    python tools/check_dld_normalization.py mailbox_ip      # one IP
    python tools/check_dld_normalization.py --calibrate     # every DLD against itself
    python tools/check_dld_normalization.py --stamp <ip>    # record human review
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DLDS_DIR = REPO_ROOT / "dlds"
BASELINES_PATH = DLDS_DIR / "normalization_baselines.json"
UNPLACED_HEADING = "## Unplaced Source Content"

TOOLS_DIR = Path(__file__).resolve().parent


def _load_sibling(module_name: str):
    """Load a sibling tool module, so this gate reuses the real extractor."""
    path = TOOLS_DIR / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load tool: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dld = _load_sibling("dld_to_template")


# --------------------------------------------------------------------------- #
# Tokenizers
# --------------------------------------------------------------------------- #

# A measurement is a number bound to a unit. Both orders occur in real DLDs:
# "2 ns", "500 MHz", "1 cycle" (value first) and "depth 8", "capacity 4"
# (keyword first).
UNITS = (
    "ns|us|ms|ps|s|mhz|ghz|khz|hz|cycle|cycles|cyc|bit|bits|byte|bytes|"
    "word|words|entry|entries|channel|channels|deep|slot|slots|level|levels"
)
MEASURE_RE = re.compile(rf"(\d+(?:\.\d+)?)\s*({UNITS})\b", re.IGNORECASE)

KEYWORDS = (
    "depth|capacity|limit|width|count|total|outstanding|budget|credits?|"
    "tokens?|threshold|watermark|priority|frequency|size"
)
KEYED_RE = re.compile(rf"\b({KEYWORDS})\b\W{{0,4}}(\d+(?:\.\d+)?)", re.IGNORECASE)

NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
}
NUMBER_WORD_RE = re.compile(rf"\b({'|'.join(NUMBER_WORDS)})\b", re.IGNORECASE)

# An identifier is something the author marked as one: a backticked token, or an
# ALL_CAPS name with an underscore (WAIT_SW_CLEAR, CHAN_CTRL). Bare ALL_CAPS
# words (IDLE, RESET) are picked up structurally from `States:` blocks instead,
# so prose acronyms like FIFO or AXI never enter the identifier set.
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
UNDERSCORE_CAPS_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
IDENTIFIER_SHAPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")

BLOCKING_LANGUAGE = (
    "block",
    "wait",
    "stall",
    "until",
    "backpressure",
    "back-pressure",
    "acknowledge",
    "acknowledged",
    "ack",
    "outstanding",
    "clear",
    "response",
    "handshake",
    "in flight",
    "in-flight",
)

STOPWORDS = frozenset(
    """a an the and or of to in on for with is are be as by at from that this it its
    each per not no any all when while then than which must shall may can into out
    if only also such same other more most one two both same very same""".split()
)


def digitize(text: str) -> str:
    """Spell out numbers as digits so "one cycle" and "1 cycle" compare equal.

    Normalization legitimately rewrites prose into the extractor's table form, so
    a spelled-out count must not read as a dropped measurement on one side and an
    invented one on the other.
    """
    return NUMBER_WORD_RE.sub(lambda match: NUMBER_WORDS[match.group(0).lower()], text)


def measurement_counts(text: str) -> Counter[str]:
    """Every value+unit pair in the text, with occurrence counts."""
    prepared = digitize(text)
    counts: Counter[str] = Counter()
    for value, unit in MEASURE_RE.findall(prepared):
        counts[f"{float(value):g} {unit.lower()}"] += 1
    for keyword, value in KEYED_RE.findall(prepared):
        counts[f"{keyword.lower()}={float(value):g}"] += 1
    return counts


def measurements(text: str) -> set[str]:
    """The distinct value+unit pairs in the text."""
    return set(measurement_counts(text))


def state_names(text: str) -> set[str]:
    """ALL_CAPS names listed as bullets under a ``States:`` label."""
    names: set[str] = set()
    in_states = False
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^states\s*:", stripped, re.IGNORECASE):
            in_states = True
            continue
        if in_states:
            bullet = re.match(r"^[-*]\s+(.+)$", stripped)
            if bullet:
                candidate = bullet.group(1).strip().strip("`").strip()
                if re.fullmatch(r"[A-Z][A-Z0-9_]*", candidate):
                    names.add(candidate)
                continue
            if stripped and not stripped.startswith(("-", "*")):
                in_states = False
    return names


def identifiers(text: str) -> set[str]:
    """Author-marked identifiers: backticked tokens, underscored ALL_CAPS, states."""
    found: set[str] = set(UNDERSCORE_CAPS_RE.findall(text))
    found |= state_names(text)
    for raw in BACKTICK_RE.findall(text):
        candidate = raw.strip()
        if IDENTIFIER_SHAPE_RE.match(candidate) and not candidate.isdigit():
            found.add(candidate)
    return found


def content_blocks(text: str) -> list[str]:
    """Split into content blocks, keeping fenced code as single units."""
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            current.append(line)
            if not in_fence:
                blocks.append("\n".join(current))
                current = []
            continue
        if in_fence:
            current.append(line)
            continue
        if not line.strip():
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return [block for block in blocks if block.strip()]


def content_tokens(text: str) -> set[str]:
    """Distinctive lowercase word/number tokens, for block matching."""
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?", text.lower())
    return {token for token in raw if len(token) > 1 and token not in STOPWORDS}


def split_unplaced(text: str) -> tuple[str, str]:
    """Return (body, unplaced_section) around the Unplaced Source Content heading."""
    index = text.find(UNPLACED_HEADING)
    if index < 0:
        return text, ""
    return text[:index], text[index:]


def interface_sections(text: str) -> dict[str, str]:
    """Map interface-section heading text to that section's body."""
    sections: dict[str, str] = {}
    current_key: str | None = None
    current: list[str] = []
    for line in text.splitlines():
        heading = re.match(r"^#{2,4}\s+(.*)$", line.strip())
        if heading:
            if current_key is not None:
                sections[current_key] = "\n".join(current)
            title = heading.group(1)
            current_key = title if re.search(r"interface", title, re.IGNORECASE) else None
            current = []
            continue
        if current_key is not None:
            current.append(line)
    if current_key is not None:
        sections[current_key] = "\n".join(current)
    return sections


def heading_key(title: str) -> frozenset[str]:
    return frozenset(content_tokens(re.sub(r"^[\d.\s]+", "", title)))


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def check_measurements(source: str, normalized: str) -> list[str]:
    """Measurements must not disappear, and none may appear from nowhere.

    Both directions are load-bearing. Checking only for *lost* values misses the
    most dangerous edit of all — retyping one ``2 ns`` as ``3 ns`` in a document
    that states ``2 ns`` elsewhere leaves the value present and looks clean. The
    invented-value check is what catches it.

    Occurrence *counts* are deliberately reported rather than failed: a
    normalizer may legitimately consolidate a timing the source states twice, or
    restate a source-backed number in the table the extractor reads. A changed
    count is a fact for the human reviewer holding the stamp, not a machine
    verdict.
    """
    source_counts, normalized_counts = measurement_counts(source), measurement_counts(normalized)

    errors: list[str] = []
    lost = sorted(set(source_counts) - set(normalized_counts))
    if lost:
        errors.append(f"measurement(s) dropped: {', '.join(lost)}")
    invented = sorted(set(normalized_counts) - set(source_counts))
    if invented:
        errors.append(f"measurement(s) invented (absent from source): {', '.join(invented)}")
    return errors


def measurement_count_notes(source: str, normalized: str) -> list[str]:
    """Informational count deltas for values present on both sides."""
    source_counts, normalized_counts = measurement_counts(source), measurement_counts(normalized)
    notes: list[str] = []
    for value in sorted(set(source_counts) & set(normalized_counts)):
        before, after = source_counts[value], normalized_counts[value]
        if before != after:
            notes.append(f"`{value}` stated {before}x in source, {after}x after normalization")
    return notes


def check_identifiers(source: str, normalized: str) -> list[str]:
    source_ids, normalized_ids = identifiers(source), identifiers(normalized)
    errors: list[str] = []
    lost = sorted(source_ids - normalized_ids)
    if lost:
        errors.append(f"identifier(s) dropped: {', '.join(lost)}")
    invented = sorted(normalized_ids - source_ids)
    if invented:
        errors.append(f"identifier(s) invented (absent from source): {', '.join(invented)}")
    return errors


def check_fsm_parity(source: str, normalized: str) -> list[str]:
    """Compare FSM name sets and declared counts using the real extractor."""
    source_fsms = {f["name"] for f in dld.extract_fsms(source.splitlines())}
    normalized_fsms = {f["name"] for f in dld.extract_fsms(normalized.splitlines())}

    errors: list[str] = []
    dropped = sorted(source_fsms - normalized_fsms)
    added = sorted(normalized_fsms - source_fsms)

    # A source too unstructured for the extractor to see FSMs is exactly what
    # normalization is for; only a *disagreement* about a recognized FSM fails.
    if dropped and normalized_fsms:
        errors.append(f"FSM(s) present in source but missing after normalization: {', '.join(dropped)}")
    if added and source_fsms:
        errors.append(f"FSM(s) added by normalization: {', '.join(added)}")

    source_count = dld.extract_fsm_count(source, len(source_fsms))
    normalized_count = dld.extract_fsm_count(normalized, len(normalized_fsms))
    if source_fsms and source_count != normalized_count:
        errors.append(f"declared FSM count changed: source={source_count} normalized={normalized_count}")
    if normalized_fsms and normalized_count != len(normalized_fsms):
        errors.append(
            f"normalized declares {normalized_count} FSM(s) but defines {len(normalized_fsms)} FSM section(s)"
        )
    return errors


def check_wait_model_provenance(source: str, normalized: str) -> list[str]:
    """A wait model must trace to blocking language in the matching source section."""
    source_sections = interface_sections(source)
    errors: list[str] = []

    for title, body in interface_sections(normalized).items():
        if not re.search(r"wait\s*model\s*:", body, re.IGNORECASE):
            continue

        wanted = heading_key(title)
        best_body, best_overlap = "", 0
        for source_title, source_body in source_sections.items():
            overlap = len(wanted & heading_key(source_title))
            if overlap > best_overlap:
                best_body, best_overlap = source_body, overlap

        haystack = (best_body if best_overlap else source).lower()
        if not any(word in haystack for word in BLOCKING_LANGUAGE):
            scope = f"source section matching `{title}`" if best_overlap else "the source document"
            errors.append(
                f"interface `{title}` declares a wait model, but {scope} states no blocking behavior "
                "(a wait model may not be invented for a silent interface)"
            )
    return errors


def check_unplaced_accounting(source: str, normalized: str, threshold: float = 0.6) -> list[str]:
    """Every source block must reach the normalized body or the Unplaced section."""
    normalized_blocks = [content_tokens(block) for block in content_blocks(normalized)]
    lost: list[str] = []

    for block in content_blocks(source):
        tokens = content_tokens(block)
        if not tokens:
            continue
        best = max((len(tokens & other) / len(tokens) for other in normalized_blocks), default=0.0)
        if best < threshold:
            summary = " ".join(block.split())[:70]
            lost.append(summary)

    if not lost:
        return []
    listed = "; ".join(f'"{item}"' for item in lost[:5])
    suffix = f" (+{len(lost) - 5} more)" if len(lost) > 5 else ""
    return [
        f"{len(lost)} source block(s) reached neither the normalized body nor `{UNPLACED_HEADING}`: {listed}{suffix}"
    ]


# --------------------------------------------------------------------------- #
# Stamping
# --------------------------------------------------------------------------- #


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_baselines() -> dict[str, dict[str, str]]:
    if not BASELINES_PATH.is_file():
        return {}
    return json.loads(BASELINES_PATH.read_text(encoding="utf-8"))


def save_baselines(baselines: dict[str, dict[str, str]]) -> None:
    BASELINES_PATH.write_text(json.dumps(baselines, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check_stamp(ip: str, source: str, normalized: str) -> list[str]:
    baseline = load_baselines().get(ip)
    if baseline is None:
        return [
            f"{ip}: no normalization stamp — a human must confirm the meaning survived, then run "
            f"`python tools/check_dld_normalization.py --stamp {ip}`"
        ]
    if baseline.get("src") != sha256_text(source) or baseline.get("normalized") != sha256_text(normalized):
        return [
            f"{ip}: normalization stamp is stale (a DLD changed since review) — re-review the diff, "
            f"then re-stamp with `--stamp {ip}`"
        ]
    return []


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def source_path(ip: str) -> Path:
    return DLDS_DIR / f"{ip}_dld.src.md"


def normalized_path(ip: str) -> Path:
    return DLDS_DIR / f"{ip}_dld.md"


def normalizable_ips() -> list[str]:
    return sorted(path.name[: -len("_dld.src.md")] for path in DLDS_DIR.glob("*_dld.src.md"))


def all_ips() -> list[str]:
    return sorted(path.name[: -len("_dld.md")] for path in DLDS_DIR.glob("*_dld.md"))


def check_ip(ip: str, source: str, normalized: str, *, require_stamp: bool) -> list[str]:
    errors: list[str] = []
    errors += check_measurements(source, normalized)
    errors += check_identifiers(source, normalized)
    errors += check_fsm_parity(source, normalized)
    errors += check_wait_model_provenance(source, normalized)
    errors += check_unplaced_accounting(source, normalized)
    if require_stamp:
        errors += check_stamp(ip, source, normalized)
    return [f"{ip}: {error}" for error in errors]


def run_calibration() -> int:
    """Every DLD against itself: an identity normalization must pass every check."""
    ips = all_ips()
    if not ips:
        print("no DLDs found", file=sys.stderr)
        return 1

    failures = 0
    for ip in ips:
        text = normalized_path(ip).read_text(encoding="utf-8")
        errors = check_ip(ip, text, text, require_stamp=False)
        counts = f"{len(measurements(text))} measurement(s), {len(identifiers(text))} identifier(s)"
        if errors:
            failures += 1
            print(f"  FAIL {ip}: {counts}")
            for error in errors:
                print(f"    - {error}")
        else:
            print(f"  OK   {ip}: {counts}")

    if failures:
        print(f"calibration: FAIL ({failures}/{len(ips)} DLDs)", file=sys.stderr)
        return 1
    print(f"calibration: OK ({len(ips)} DLDs pass identity normalization)")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ips", nargs="*", help="IP names to check (default: every IP with a .src.md)")
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="check every DLD against itself; an identity normalization must pass",
    )
    parser.add_argument("--stamp", metavar="IP", help="record human review of this IP's normalization")
    parser.add_argument(
        "--no-stamp-check",
        action="store_true",
        help="run the mechanical checks only, skipping the human-review stamp",
    )
    args = parser.parse_args(argv)

    if args.calibrate:
        return run_calibration()

    if args.stamp:
        ip = args.stamp
        src, normalized = source_path(ip), normalized_path(ip)
        for path in (src, normalized):
            if not path.is_file():
                raise SystemExit(f"not found: {path}")
        source_text = src.read_text(encoding="utf-8")
        normalized_text = normalized.read_text(encoding="utf-8")
        errors = check_ip(ip, source_text, normalized_text, require_stamp=False)
        if errors:
            print("refusing to stamp: mechanical checks fail", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            return 1
        baselines = load_baselines()
        baselines[ip] = {"src": sha256_text(source_text), "normalized": sha256_text(normalized_text)}
        save_baselines(baselines)
        print(f"stamped {ip}: normalization reviewed and recorded in {BASELINES_PATH.name}")
        return 0

    ips = args.ips or normalizable_ips()
    if not ips:
        print("no normalized DLDs to check (no dlds/*_dld.src.md present)")
        return 0

    all_errors: list[str] = []
    for ip in ips:
        src, normalized = source_path(ip), normalized_path(ip)
        if not src.is_file():
            all_errors.append(f"{ip}: no source DLD {src.name} (nothing to check normalization against)")
            continue
        if not normalized.is_file():
            all_errors.append(f"{ip}: no normalized DLD {normalized.name}")
            continue
        source_text = src.read_text(encoding="utf-8")
        normalized_text = normalized.read_text(encoding="utf-8")
        all_errors += check_ip(ip, source_text, normalized_text, require_stamp=not args.no_stamp_check)
        for note in measurement_count_notes(source_text, normalized_text):
            print(f"  note: {ip}: {note}")

    if all_errors:
        print("dld normalization: FAIL", file=sys.stderr)
        for error in all_errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"dld normalization: OK ({len(ips)} normalized DLD(s) faithful to their source)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
