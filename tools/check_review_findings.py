#!/usr/bin/env python3
"""Deterministic gate for normalization review findings.

See docs/proposals/review_normalization_stage.md. A reviewer — an LLM, or a
person reading the two documents — records what it believes changed meaning in
``reviews/<ip>.findings.yaml``. This tool decides, mechanically, whether every
recorded finding has been dealt with.

The split matters more than either half. A reviewer's verdict is sampled: ask
twice, get two answers, and a gate built on that stops meaning anything. So the
reviewer never votes. It writes an artifact, and this tool asks a question with a
mechanical answer::

    is every recorded finding accounted for?

A finding is accounted for when it is

  * **fixed** — the normalization changed, which makes the review stale and
    forces a fresh one (the same hash-pair device the normalization stamp uses),
    or
  * **dismissed** — a human wrote a line in ``decisions/<ip>.md`` naming the
    finding and giving a reason.

What this gate does *not* do is judge whether a finding is correct. That is the
human's call, and dismissing one costs a sentence of writing — deliberately, so
that dismissal is a decision on the record rather than a silence.

**An empty findings list is not a pass.** It records that a reviewer looked and
reported nothing, which is a weaker claim than "faithful" and must not be read as
the stronger one. The normalization stamp remains the human judgement.

Usage::

    python tools/check_review_findings.py                # every IP with findings
    python tools/check_review_findings.py sram_ctrl_ip   # one IP
    python tools/check_review_findings.py --list         # what is outstanding
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DLDS_DIR = REPO_ROOT / "dlds"
REVIEWS_DIR = REPO_ROOT / "reviews"
DECISIONS_DIR = REPO_ROOT / "decisions"

# Closed vocabulary. A reviewer that cannot classify an observation should not
# file it: an "other" bucket is where vague commentary accumulates, and every
# vague finding costs the same human attention as a real one.
FINDING_CLASSES = frozenset(
    {
        "timing_attachment",
        "wait_model_attachment",
        "state_semantics",
        "command_effect",
        "misplaced_unplaced",
        "open_item_drift",
    }
)
SEVERITIES = frozenset({"high", "medium", "low"})
REQUIRED_FIELDS = ("id", "class", "severity", "claim", "source", "normalized", "why")

# `- **F1 dismissed:** reason` / `- **F1 fixed:** reason` in decisions/<ip>.md.
DISMISSAL_RE = re.compile(r"\*\*\s*([A-Za-z][\w.-]*)\s+(dismissed|fixed)\s*:?\s*\*\*\s*(.+)", re.IGNORECASE)


def require_yaml():
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - environment guard
        raise SystemExit("PyYAML is required. Install project requirements before running this tool.") from exc
    return yaml


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def findings_path(ip: str) -> Path:
    return REVIEWS_DIR / f"{ip}.findings.yaml"


def reviewed_ips() -> list[str]:
    if not REVIEWS_DIR.is_dir():
        return []
    return sorted(path.name[: -len(".findings.yaml")] for path in REVIEWS_DIR.glob("*.findings.yaml"))


def load_findings(path: Path) -> tuple[dict, list[str]]:
    """Parse a findings file, rejecting anything malformed rather than skipping it.

    A findings file the gate cannot read is not an absence of findings — it is a
    review whose result is unknown, which is the one thing this must never treat
    as clean.
    """
    yaml = require_yaml()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same verdict
        return {}, [f"{path.name}: not valid YAML ({exc.__class__.__name__})"]
    if not isinstance(data, dict):
        return {}, [f"{path.name}: expected a mapping at the top level"]

    errors: list[str] = []
    for key in ("ip", "source_sha256", "normalized_sha256"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            errors.append(f"{path.name}: missing `{key}`")

    findings = data.get("findings")
    if findings is None:
        findings = []
    if not isinstance(findings, list):
        return data, errors + [f"{path.name}: `findings` must be a list"]

    seen: set[str] = set()
    for index, finding in enumerate(findings):
        where = f"{path.name}: findings[{index}]"
        if not isinstance(finding, dict):
            errors.append(f"{where}: expected a mapping")
            continue
        for field in REQUIRED_FIELDS:
            value = finding.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{where}: missing `{field}`")
        identifier = finding.get("id")
        if isinstance(identifier, str):
            if identifier in seen:
                errors.append(f"{where}: duplicate finding id `{identifier}`")
            seen.add(identifier)
        if isinstance(finding.get("class"), str) and finding["class"] not in FINDING_CLASSES:
            errors.append(f"{where}: unknown class `{finding['class']}` (one of: {', '.join(sorted(FINDING_CLASSES))})")
        if isinstance(finding.get("severity"), str) and finding["severity"] not in SEVERITIES:
            errors.append(f"{where}: unknown severity `{finding['severity']}`")

    data["findings"] = findings
    return data, errors


def dismissals(ip: str) -> dict[str, str]:
    """Finding ids a human has explicitly written off, with their stated reason."""
    path = DECISIONS_DIR / f"{ip}.md"
    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    for match in DISMISSAL_RE.finditer(path.read_text(encoding="utf-8")):
        found[match.group(1)] = match.group(3).strip()
    return found


def review_is_stale(data: dict, ip: str) -> str | None:
    """Whether the reviewed documents have changed since the review was written."""
    src = DLDS_DIR / f"{ip}_dld.src.md"
    normalized = DLDS_DIR / f"{ip}_dld.md"
    for path in (src, normalized):
        if not path.is_file():
            return f"{path.name} does not exist, so the review cannot be checked against it"
    if data.get("source_sha256") != sha256_text(src.read_text(encoding="utf-8")):
        return "the author source changed since this review"
    if data.get("normalized_sha256") != sha256_text(normalized.read_text(encoding="utf-8")):
        return "the normalized DLD changed since this review"
    return None


def check_ip(ip: str) -> list[str]:
    path = findings_path(ip)
    if not path.is_file():
        return []

    data, errors = load_findings(path)
    if errors:
        return errors

    stale = review_is_stale(data, ip)
    if stale:
        return [
            f"{ip}: review is stale - {stale}. Re-run the reviewer against the current "
            f"documents; an old clean review must not vouch for new text."
        ]

    if data.get("ip") != ip:
        errors.append(f"{path.name}: declares ip `{data.get('ip')}` but is named for `{ip}`")

    resolved = dismissals(ip)
    for finding in data["findings"]:
        identifier = finding.get("id")
        if identifier in resolved:
            continue
        errors.append(
            f"{ip}: finding {identifier} ({finding.get('class')}, {finding.get('severity')}) is unresolved - "
            f"{finding.get('claim')} Fix the normalization, or dismiss it by name in "
            f"decisions/{ip}.md (`- **{identifier} dismissed:** <reason>`)."
        )
    return errors


def outstanding_summary() -> list[str]:
    lines: list[str] = []
    for ip in reviewed_ips():
        data, errors = load_findings(findings_path(ip))
        if errors:
            lines.append(f"  {ip}: findings file is malformed")
            continue
        resolved = dismissals(ip)
        total = len(data["findings"])
        open_count = sum(1 for f in data["findings"] if f.get("id") not in resolved)
        state = "stale" if review_is_stale(data, ip) else "current"
        lines.append(f"  {ip}: {total} finding(s), {open_count} unresolved, review {state}")
    return lines or ["  (no reviews recorded)"]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ips", nargs="*", help="IP names to check (default: every IP with a findings file)")
    parser.add_argument("--list", action="store_true", help="summarise recorded reviews without failing")
    args = parser.parse_args(argv)

    if args.list:
        print("normalization reviews:")
        for line in outstanding_summary():
            print(line)
        return 0

    ips = args.ips or reviewed_ips()
    if not ips:
        # No reviewer has run. That is the current state of the world, not a
        # clean bill of health: the stage is unbuilt (see the proposal's build
        # order), and this becomes a hard requirement when it is wired in.
        print("review findings: none recorded (no reviews/*.findings.yaml)")
        return 0

    errors: list[str] = []
    for ip in ips:
        errors += check_ip(ip)

    if errors:
        print("review findings: FAIL", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"review findings: OK ({len(ips)} review(s), every finding accounted for)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
