#!/usr/bin/env python3
"""Report actual agent cost per IP: dispatches, wall-clock time, tokens.

Reads ``reports/<ip>.cost.json``, written by ``tools/auto_ip_pipeline.py`` at
the end of each IP's run -- one file per IP, covering every agent stage that
run actually dispatched (``normalize_dld``, ``complete_template``,
``agent_implementation``/``amend_implementation``, the two reviewers).

Token counts are only as complete as what the dispatched agent CLI itself
reported (see ``auto_ip_pipeline._parse_agent_usage`` -- today, Claude Code's
``-p --output-format json`` result shape specifically). A stage dispatched
through a CLI that does not report usage shows dispatches and duration with
tokens as ``-`` (unknown), never a guessed or zeroed number.

Usage::

    python tools/report_pipeline_cost.py                 # every reports/*.cost.json
    python tools/report_pipeline_cost.py arbitration_ip   # one IP
    python tools/report_pipeline_cost.py --json           # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from ._repo_root import find_repo_root
except ImportError:  # not running as part of the `ip_model_automation.tools`
    # package (e.g. `python tools/x.py`, or a test loading this file
    # directly via importlib) -- fall back to a sibling top-level import.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _repo_root import find_repo_root

REPO_ROOT = find_repo_root()
REPORTS_DIR = REPO_ROOT / "reports"


def discover_cost_reports(ips: list[str] | None = None) -> list[Path]:
    if not REPORTS_DIR.is_dir():
        return []
    if ips:
        return [p for ip in sorted(ips) if (p := REPORTS_DIR / f"{ip}.cost.json").is_file()]
    return sorted(REPORTS_DIR.glob("*.cost.json"))


def load_report(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "stages" not in data or "total" not in data:
        raise ValueError(f"{path}: not a recognized cost report")
    return data


def fmt_tokens(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def render_report(report: dict[str, Any]) -> list[str]:
    lines = [f"{report['ip']}:"]
    for stage_name in sorted(report["stages"]):
        entry = report["stages"][stage_name]
        lines.append(
            f"  {stage_name}: {entry['dispatches']} dispatch(es), {entry['duration_s']:.1f}s, "
            f"{fmt_tokens(entry['input_tokens'])} in / {fmt_tokens(entry['output_tokens'])} out tokens"
        )
    total = report["total"]
    lines.append(
        f"  total: {total['dispatches']} dispatch(es), {total['duration_s']:.1f}s, "
        f"{fmt_tokens(total['input_tokens'])} in / {fmt_tokens(total['output_tokens'])} out tokens"
    )
    return lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ips", nargs="*", help="IP names to report (default: every reports/*.cost.json)")
    parser.add_argument("--json", action="store_true", help="print the raw reports as one JSON array instead")
    args = parser.parse_args(argv)

    paths = discover_cost_reports(args.ips or None)
    if not paths:
        if args.ips:
            print(f"no cost report(s) found for: {', '.join(sorted(args.ips))}", file=sys.stderr)
            return 1
        print("no reports/*.cost.json found -- no agent dispatches recorded yet")
        return 0

    reports = [load_report(p) for p in paths]
    if args.json:
        print(json.dumps(reports, indent=2, sort_keys=True))
        return 0

    for report in reports:
        print("\n".join(render_report(report)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
