#!/usr/bin/env python3
"""Report template FSM coverage from test_scenarios."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def require_yaml():
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("PyYAML is required. Install project requirements before running this tool.") from exc
    return yaml


def load_template(path: Path) -> dict[str, Any]:
    yaml = require_yaml()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"{path}: expected YAML mapping")
    return data


def discover_templates(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "templates").glob("*.template.yaml"))


def coverage_for_template(template: dict[str, Any]) -> tuple[str, dict[str, list[str]]]:
    ip_name = template["ip"]["name"]
    fsms = [str(fsm["name"]) for fsm in template.get("fsm_processes", [])]
    coverage: dict[str, list[str]] = {fsm: [] for fsm in fsms}
    for scenario in template.get("test_scenarios", []):
        scenario_name = str(scenario.get("name", "unnamed_scenario"))
        for item in scenario.get("fsm_coverage", []):
            fsm_name = str(item).split(".", 1)[0]
            if fsm_name in coverage:
                coverage[fsm_name].append(scenario_name)
    return ip_name, coverage


def build_report(repo_root: Path) -> tuple[list[str], dict[str, list[str]]]:
    lines: list[str] = []
    missing: dict[str, list[str]] = defaultdict(list)
    for template_path in discover_templates(repo_root):
        ip_name, coverage = coverage_for_template(load_template(template_path))
        covered = sum(1 for scenarios in coverage.values() if scenarios)
        total = len(coverage)
        lines.append(f"{ip_name}: {covered}/{total} FSMs covered")
        for fsm_name, scenarios in coverage.items():
            if scenarios:
                lines.append(f"  OK {fsm_name}: {', '.join(scenarios)}")
            else:
                lines.append(f"  MISS {fsm_name}")
                missing[ip_name].append(fsm_name)
    return lines, missing


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--fail-on-missing", action="store_true", default=True)
    args = parser.parse_args(argv)

    lines, missing = build_report(args.repo_root.resolve())
    print("\n".join(lines))
    if args.fail_on_missing and missing:
        print("\nUncovered FSMs found:", file=sys.stderr)
        for ip_name, fsms in missing.items():
            print(f"  {ip_name}: {', '.join(fsms)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
