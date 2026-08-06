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

# Two review kinds, because there are two places a faithful-looking artifact can
# still be wrong, and each compares a different pair of things:
#
#   normalization — the reshaped DLD against the author's original
#   model         — the generated model and its tests against their template
#
# Each declares the files it is a statement about (staleness is checked against
# them), the fields a finding must carry, and its own closed class vocabulary.
# A reviewer that cannot classify an observation should not file it: an "other"
# bucket is where vague commentary accumulates, and every vague finding costs the
# same human attention as a real one.
REVIEW_KINDS: dict[str, dict] = {
    "normalization": {
        "subjects": {
            "source_sha256": "dlds/{ip}_dld.src.md",
            "normalized_sha256": "dlds/{ip}_dld.md",
        },
        "fields": ("id", "class", "severity", "claim", "source", "normalized", "why"),
        "classes": frozenset(
            {
                "timing_attachment",
                "wait_model_attachment",
                "state_semantics",
                "command_effect",
                "misplaced_unplaced",
                "open_item_drift",
            }
        ),
    },
    "model": {
        "subjects": {
            "template_sha256": "templates/{ip}.template.yaml",
            "model_sha256": "src/ip_model_automation/{ip}.py",
            "tests_sha256": "tests/test_{ip}.py",
        },
        "fields": ("id", "class", "severity", "claim", "template", "model", "why"),
        "classes": frozenset(
            {
                "timing_mismatch",
                "capacity_mismatch",
                "fsm_structure",
                "wait_model_impl",
                "test_asserts_wrong_thing",
                "scenario_gap",
            }
        ),
    },
}
SEVERITIES = frozenset({"high", "medium", "low"})

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


def findings_path(ip: str, kind: str) -> Path:
    return REVIEWS_DIR / f"{ip}.{kind}.findings.yaml"


def reviews() -> list[tuple[str, str]]:
    """Every recorded review, as (ip, kind), from `<ip>.<kind>.findings.yaml`."""
    if not REVIEWS_DIR.is_dir():
        return []
    found: list[tuple[str, str]] = []
    for path in sorted(REVIEWS_DIR.glob("*.findings.yaml")):
        stem = path.name[: -len(".findings.yaml")]
        ip, _, kind = stem.rpartition(".")
        if ip and kind in REVIEW_KINDS:
            found.append((ip, kind))
    return found


def reviewed_ips() -> list[str]:
    return sorted({ip for ip, _kind in reviews()})


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
    kind = data.get("kind")
    if kind not in REVIEW_KINDS:
        return data, [f"{path.name}: unknown review kind `{kind}` (one of: {', '.join(sorted(REVIEW_KINDS))})"]
    spec = REVIEW_KINDS[kind]

    for key in ("ip", *spec["subjects"]):
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
        for field in spec["fields"]:
            value = finding.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{where}: missing `{field}`")
        identifier = finding.get("id")
        if isinstance(identifier, str):
            if identifier in seen:
                errors.append(f"{where}: duplicate finding id `{identifier}`")
            seen.add(identifier)
        if isinstance(finding.get("class"), str) and finding["class"] not in spec["classes"]:
            errors.append(
                f"{where}: unknown class `{finding['class']}` for a {kind} review "
                f"(one of: {', '.join(sorted(spec['classes']))})"
            )
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


def review_is_stale(data: dict, ip: str, kind: str) -> str | None:
    """Whether anything the review is a statement about has changed since.

    A review is a claim about specific files. Edit one and the claim no longer
    covers what is there — including the case that matters most, where a finding
    was fixed and a fresh review is owed.
    """
    for key, pattern in REVIEW_KINDS[kind]["subjects"].items():
        path = REPO_ROOT / pattern.format(ip=ip)
        if not path.is_file():
            return f"{path.name} does not exist, so the review cannot be checked against it"
        if data.get(key) != sha256_text(path.read_text(encoding="utf-8")):
            return f"{path.name} changed since this review"
    return None


def check_ip(ip: str, kind: str) -> list[str]:
    path = findings_path(ip, kind)
    if not path.is_file():
        return []

    data, errors = load_findings(path)
    if errors:
        return errors

    stale = review_is_stale(data, ip, kind)
    if stale:
        return [
            f"{ip} ({kind}): review is stale - {stale}. Re-run the reviewer against the current "
            f"files; an old clean review must not vouch for something that has changed since."
        ]

    if data.get("ip") != ip:
        errors.append(f"{path.name}: declares ip `{data.get('ip')}` but is named for `{ip}`")
    if data.get("kind") != kind:
        errors.append(f"{path.name}: declares kind `{data.get('kind')}` but is named for `{kind}`")

    resolved = dismissals(ip)
    for finding in data["findings"]:
        identifier = finding.get("id")
        if identifier in resolved:
            continue
        errors.append(
            f"{ip} ({kind}): finding {identifier} ({finding.get('class')}, {finding.get('severity')}) is "
            f"unresolved - {finding.get('claim')} Fix it, or dismiss it by name in "
            f"decisions/{ip}.md (`- **{identifier} dismissed:** <reason>`)."
        )
    return errors


def duplicate_ids(ip: str) -> list[str]:
    """Finding ids must be unique across an IP's reviews.

    Dismissals are written in one file per IP and matched by id, so `F1` meaning
    one thing in a normalization review and another in a model review would let a
    single dismissal silently clear both.
    """
    seen: dict[str, str] = {}
    clashes: list[str] = []
    for review_ip, kind in reviews():
        if review_ip != ip:
            continue
        data, errors = load_findings(findings_path(ip, kind))
        if errors:
            continue
        for finding in data.get("findings", []):
            identifier = finding.get("id")
            if not isinstance(identifier, str):
                continue
            if identifier in seen:
                clashes.append(
                    f"{ip}: finding id `{identifier}` is used by both the {seen[identifier]} and {kind} "
                    f"reviews; one dismissal would clear both. Give them distinct ids."
                )
            seen[identifier] = kind
    return clashes


def outstanding_summary() -> list[str]:
    lines: list[str] = []
    for ip, kind in reviews():
        data, errors = load_findings(findings_path(ip, kind))
        if errors:
            lines.append(f"  {ip} ({kind}): findings file is malformed")
            continue
        resolved = dismissals(ip)
        total = len(data["findings"])
        open_count = sum(1 for f in data["findings"] if f.get("id") not in resolved)
        state = "stale" if review_is_stale(data, ip, kind) else "current"
        lines.append(f"  {ip} ({kind}): {total} finding(s), {open_count} unresolved, review {state}")
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

    wanted = set(args.ips) if args.ips else None
    pairs = [(ip, kind) for ip, kind in reviews() if wanted is None or ip in wanted]
    if not pairs:
        # No reviewer has run. That is the current state of the world, not a
        # clean bill of health: the stage is unbuilt (see the proposal's build
        # order), and this becomes a hard requirement when it is wired in.
        print("review findings: none recorded (no reviews/*.findings.yaml)")
        return 0

    errors: list[str] = []
    for ip in sorted({ip for ip, _kind in pairs}):
        errors += duplicate_ids(ip)
    for ip, kind in pairs:
        errors += check_ip(ip, kind)

    if errors:
        print("review findings: FAIL", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"review findings: OK ({len(pairs)} review(s), every finding accounted for)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
