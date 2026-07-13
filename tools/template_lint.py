#!/usr/bin/env python3
"""Dependency-free lint checks for IP model templates.

The linter intentionally enforces the generation contract more tightly than a
generic YAML parser would. It checks the modeling fields that must be present
before an LLM/code generator is allowed to create SimPy models and tests.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REQUIRED_TOP_LEVEL = [
    "ip",
    "interfaces",
    "commands",
    "fsm_processes",
    "fsm_relationships",
    "timing_model",
    "functionality_model",
    "performance_model",
    "test_scenarios",
]


REQUIRED_PERFORMANCE_FIELDS = [
    "language: python",
    "library: simpy",
    "model_functionality: true",
    "model_fsm_processes: true",
    "metrics:",
]


REQUIRED_FUNCTIONALITY_FIELDS = [
    "implemented_in_simpy: true",
    "state_variables:",
    "apis:",
    "invariants:",
]


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def top_level_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#") or raw_line[0].isspace():
            continue
        if ":" in line:
            keys.add(line.split(":", 1)[0].strip())
    return keys


def section_lines(text: str, section: str) -> list[str]:
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line == f"{section}:":
            start = idx + 1
            break
    if start is None:
        return []
    end = len(lines)
    for idx in range(start, len(lines)):
        line = lines[idx]
        if line and not line[0].isspace() and line.endswith(":"):
            end = idx
            break
    return lines[start:end]


def count_top_list_items(text: str, section: str) -> int:
    return sum(1 for line in section_lines(text, section) if line.startswith("  - "))


def collect_fsm_names(text: str) -> list[str]:
    names: list[str] = []
    in_fsm_item = False
    for line in section_lines(text, "fsm_processes"):
        if line.startswith("  - name:"):
            names.append(line.split(":", 1)[1].strip())
            in_fsm_item = True
        elif line.startswith("  - "):
            in_fsm_item = False
        elif in_fsm_item and line.startswith("    name:"):
            names.append(line.split(":", 1)[1].strip())
    return names


def collect_fsm_blocks(text: str) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {}
    current_name: str | None = None
    current_lines: list[str] = []
    for line in section_lines(text, "fsm_processes"):
        if line.startswith("  - name:"):
            if current_name is not None:
                blocks[current_name] = current_lines
            current_name = line.split(":", 1)[1].strip()
            current_lines = [line]
            continue
        if current_name is not None:
            current_lines.append(line)
    if current_name is not None:
        blocks[current_name] = current_lines
    return blocks


def collect_timing_fsms(text: str) -> set[str]:
    timing = set()
    in_timing = False
    for line in section_lines(text, "timing_model"):
        stripped = line.strip()
        if stripped == "fsm_process_delays:":
            in_timing = True
            continue
        if (
            in_timing
            and line.startswith("  ")
            and not line.startswith("    ")
            and stripped.endswith(":")
            and stripped != "fsm_process_delays:"
        ):
            in_timing = False
        if in_timing and stripped.startswith("- fsm:"):
            timing.add(stripped.split(":", 1)[1].strip())
    return timing


def collect_timing_blocks(text: str) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {}
    current_name: str | None = None
    current_lines: list[str] = []
    in_timing = False
    for line in section_lines(text, "timing_model"):
        stripped = line.strip()
        if stripped == "fsm_process_delays:":
            in_timing = True
            continue
        if not in_timing:
            continue
        if stripped.startswith("- fsm:"):
            if current_name is not None:
                blocks[current_name] = current_lines
            current_name = stripped.split(":", 1)[1].strip()
            current_lines = [line]
            continue
        if current_name is not None:
            current_lines.append(line)
    if current_name is not None:
        blocks[current_name] = current_lines
    return blocks


def extract_scalar_int(text: str, key: str) -> int | None:
    match = re.search(rf"^\s*{re.escape(key)}:\s*([0-9]+)\s*$", text, re.MULTILINE)
    return int(match.group(1)) if match else None


def list_has_items(text: str, key: str) -> bool:
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if re.match(rf"^\s*{re.escape(key)}:\s*(\[.*\])?\s*$", line):
            if "[" in line and "]" in line:
                return line.split("[", 1)[1].split("]", 1)[0].strip() != ""
            base_indent = indent_of(line)
            for child in lines[idx + 1 :]:
                if not child.strip():
                    continue
                if indent_of(child) <= base_indent:
                    return False
                if child.lstrip().startswith("- "):
                    return True
            return False
    return False


def fsm_count_in_relationships(text: str) -> int | None:
    rel_text = "\n".join(section_lines(text, "fsm_relationships"))
    return extract_scalar_int(rel_text, "fsm_count")


def validate_required_text(text: str, required: list[str], errors: list[str], context: str) -> None:
    for item in required:
        if item not in text:
            errors.append(f"{context}: missing `{item}`")


def lint_generic_contract(text: str) -> list[str]:
    errors: list[str] = []

    keys = top_level_keys(text)
    for key in REQUIRED_TOP_LEVEL:
        if key not in keys:
            errors.append(f"missing top-level section: {key}")

    for section in ["interfaces", "commands", "fsm_processes", "test_scenarios"]:
        if count_top_list_items(text, section) == 0:
            errors.append(f"section has no list items: {section}")

    fsm_names = collect_fsm_names(text)
    if not fsm_names:
        errors.append("no FSMs found in fsm_processes")
    if len(fsm_names) != len(set(fsm_names)):
        errors.append("duplicate FSM names found")

    fsm_blocks = collect_fsm_blocks(text)
    for fsm_name, lines in fsm_blocks.items():
        block = "\n".join(lines)
        for required in [
            "simpy_process: true",
            "states:",
            "transitions:",
            "interfaces_touched:",
            "queues_used:",
            "resources_used:",
        ]:
            if required not in block:
                errors.append(f"fsm_processes.{fsm_name}: missing `{required}`")
        if "- from:" not in block:
            errors.append(f"fsm_processes.{fsm_name}: transitions must contain at least one explicit `from` state")

    relationship_count = fsm_count_in_relationships(text)
    if relationship_count is None:
        errors.append("fsm_relationships: missing integer fsm_count")
    elif relationship_count != len(fsm_names):
        errors.append(
            f"fsm_relationships: fsm_count={relationship_count} but fsm_processes defines {len(fsm_names)} FSMs"
        )

    timing_fsms = collect_timing_fsms(text)
    missing_timing = sorted(set(fsm_names) - timing_fsms)
    extra_timing = sorted(timing_fsms - set(fsm_names))
    if missing_timing:
        errors.append(f"timing_model: missing fsm_process_delays for FSMs: {', '.join(missing_timing)}")
    if extra_timing:
        errors.append(f"timing_model: has delays for unknown FSMs: {', '.join(extra_timing)}")

    for fsm_name, lines in collect_timing_blocks(text).items():
        block = "\n".join(lines)
        if "operations:" not in block:
            errors.append(f"timing_model.{fsm_name}: missing `operations`")
            continue
        if "cycles:" not in block or "ns:" not in block:
            errors.append(f"timing_model.{fsm_name}: operations must include `cycles` and `ns` values")

    for key in ["parallel_processes", "sequential_paths"]:
        if not list_has_items(text, key):
            errors.append(f"fsm_relationships: `{key}` must contain at least one item")

    timing_text = "\n".join(section_lines(text, "timing_model"))
    for key in ["clock_mhz", "cycle_time_ns"]:
        value = extract_scalar_int(timing_text, key)
        if value is None or value <= 0:
            errors.append(f"timing_model: `{key}` must be a positive number")
    if not list_has_items(text, "fsm_process_delays"):
        errors.append("timing_model: `fsm_process_delays` must contain at least one item")
    if not list_has_items(text, "end_to_end_paths"):
        errors.append("timing_model: `end_to_end_paths` must contain at least one item")

    validate_required_text(text, REQUIRED_PERFORMANCE_FIELDS, errors, "performance_model")
    validate_required_text(text, REQUIRED_FUNCTIONALITY_FIELDS, errors, "functionality_model")

    for required in ["valid_conditions:", "completion_conditions:", "error_conditions:"]:
        if required not in "\n".join(section_lines(text, "commands")):
            errors.append(f"commands: every generated template should include `{required}`")

    scenario_text = "\n".join(section_lines(text, "test_scenarios"))
    for required in ["expected_functional_behavior:", "expected_performance_properties:", "fsm_coverage:"]:
        if required not in scenario_text:
            errors.append(f"test_scenarios: missing `{required}` coverage field")
    for fsm_name in fsm_names:
        if (
            f"{fsm_name}." not in scenario_text
            and f"- {fsm_name}" not in scenario_text
            and f"[{fsm_name}" not in scenario_text
        ):
            errors.append(f"test_scenarios: no scenario covers FSM `{fsm_name}`")

    return errors


def lint_arbitration_contract(text: str) -> list[str]:
    errors: list[str] = []
    if "name: arbitration_ip" not in text:
        return errors

    for required in [
        "topology:",
        "port_mode:",
        "pending_bitmaps:",
        "device_burst_available:",
        "tenant_burst_available:",
        "sq_burst_available:",
        "issue_count_rule:",
        "issue_pipeline",
        "pending_count_read",
        "burst_read",
        "min_burst_calculation",
        "downstream_issue_request",
        "burst_debit",
    ]:
        if required not in text:
            errors.append(f"arbitration_ip contract: missing `{required}`")

    for metric in ["burst_stalls", "downstream_requests", "issued_commands", "issue_count"]:
        if metric not in text:
            errors.append(f"arbitration_ip contract: performance metrics missing `{metric}`")

    return errors


def lint_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return lint_generic_contract(text) + lint_arbitration_contract(text)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("templates", nargs="+", type=Path)
    args = parser.parse_args(argv)

    failed = False
    for template in args.templates:
        errors = lint_file(template)
        if errors:
            failed = True
            print(f"{template}: FAIL")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"{template}: OK")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
