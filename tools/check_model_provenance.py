#!/usr/bin/env python3
"""Model -> template provenance stamp.

Records, per IP, the content hash of the template each model+tests were last
generated or amended against, in ``templates/model_baselines.json`` (committed,
so provenance travels with the repo). The check fails when a template has moved
on from its recorded baseline — i.e. the template was edited but the model has
not been re-amended and re-stamped. That is the signal to run the amend flow:

    git show HEAD:templates/<ip>.template.yaml > old.yaml          # history = git
    python tools/diff_template.py old.yaml templates/<ip>.template.yaml --amend-prompt --ip <ip>
    # ... agent amends src/ip_model_automation/<ip>.py and tests/test_<ip>.py ...
    python tools/validate_dld_flow.py
    python tools/check_model_provenance.py --stamp <ip>            # refresh the baseline

Same discipline as ``check_overview_sync.py`` (docx provenance), applied to the
DLD -> template -> model chain.

The stamp records a *claim* — "this model was amended against that template" —
that the hash alone cannot verify, so stamping is per IP and follows the amend.
``--stamp-all`` exists only to bootstrap provenance and refuses to run once
baselines exist (override with ``--force`` for a deliberate bulk re-baseline).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "templates"
BASELINES_PATH = TEMPLATES_DIR / "model_baselines.json"


def template_path(ip: str) -> Path:
    return TEMPLATES_DIR / f"{ip}.template.yaml"


def promoted_ips() -> list[str]:
    return sorted(p.name[: -len(".template.yaml")] for p in TEMPLATES_DIR.glob("*.template.yaml"))


def template_fingerprint(ip: str) -> str:
    text = template_path(ip).read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_baselines() -> dict[str, str]:
    if not BASELINES_PATH.is_file():
        return {}
    return json.loads(BASELINES_PATH.read_text(encoding="utf-8"))


def save_baselines(baselines: dict[str, str]) -> None:
    BASELINES_PATH.write_text(json.dumps(baselines, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def stamp(ip: str) -> str:
    baselines = load_baselines()
    fingerprint = template_fingerprint(ip)
    baselines[ip] = fingerprint
    save_baselines(baselines)
    return fingerprint


def check() -> list[str]:
    baselines = load_baselines()
    errors: list[str] = []
    for ip in promoted_ips():
        current = template_fingerprint(ip)
        recorded = baselines.get(ip)
        if recorded is None:
            errors.append(f"{ip}: no recorded baseline; run `check_model_provenance.py --stamp {ip}`")
        elif recorded != current:
            errors.append(
                f"{ip}: template has changed since the model was last amended against it — "
                f"run the amend flow (diff_template.py), then `check_model_provenance.py --stamp {ip}`"
            )
    for ip in sorted(baselines.keys() - set(promoted_ips())):
        errors.append(f"{ip}: baseline recorded for a template that no longer exists")
    return errors


def stamp_all_blockers() -> list[str]:
    """IPs that already have a baseline, i.e. the ones --stamp-all would overwrite.

    Blanket stamping is for bootstrapping provenance, not for silencing the
    gate: stamping an IP whose template moved on asserts its model was amended
    against that template, which is a claim only the amend flow can earn. Once
    baselines exist, stamping is per IP, after the amend.
    """
    return [ip for ip in promoted_ips() if ip in load_baselines()]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp", metavar="IP", help="record the current template hash as this IP's model baseline")
    parser.add_argument("--stamp-all", action="store_true", help="stamp every promoted template (initial provenance)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow --stamp-all to overwrite baselines that already exist (bulk re-baseline)",
    )
    args = parser.parse_args(argv)

    if args.stamp_all:
        blockers = stamp_all_blockers()
        if blockers and not args.force:
            print(
                "refusing --stamp-all: "
                f"{len(blockers)} template(s) already have a baseline ({', '.join(blockers[:3])}"
                f"{', ...' if len(blockers) > 3 else ''}).",
                file=sys.stderr,
            )
            print(
                "--stamp-all is for bootstrapping provenance. To record a template change, amend the "
                "model and tests first (tools/diff_template.py --amend-prompt), then stamp that IP:\n"
                "  python tools/check_model_provenance.py --stamp <ip>\n"
                "Pass --force only for a deliberate bulk re-baseline.",
                file=sys.stderr,
            )
            return 1
        for ip in promoted_ips():
            stamp(ip)
        print(f"stamped baselines for {len(promoted_ips())} templates")
        return 0
    if args.stamp:
        fingerprint = stamp(args.stamp)
        print(f"stamped {args.stamp} baseline: sha256={fingerprint[:12]}...")
        return 0

    errors = check()
    if errors:
        for error in errors:
            print(f"model provenance: {error}")
        return 1
    print("model provenance: OK (every model is stamped against its current template)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
