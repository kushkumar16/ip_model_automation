#!/usr/bin/env python3
"""Contract checks for IP model templates.

The template contract is enforced in two layers, and this tool runs both:

1. **Structure** — the JSON Schema at ``schemas/ip_model_template.schema.json``
   is the single source of truth for the *shape* of a template: which sections
   and fields must exist, their types, and simple value constraints (e.g. a
   queue ``depth`` is a positive integer or the literal ``unbounded``, and an
   unbounded queue must carry a ``depth_note``). The schema is validated with
   ``jsonschema`` — it is not decorative documentation.

2. **Cross-field semantics** — the checks below that JSON Schema cannot express:
   the declared ``fsm_count`` matching the number of FSMs, every FSM having a
   timing entry and appearing in a test scenario, and the arbitration-IP
   sub-contract. Each rule lives in exactly one layer, so the schema and this
   file cannot drift apart.

Both layers operate on the same parsed mapping (``yaml.safe_load``); nothing
here parses YAML by hand.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "schemas" / "ip_model_template.schema.json"


def require_deps():
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("PyYAML is required. Install project requirements before running this tool.") from exc
    try:
        import jsonschema  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("jsonschema is required. Install project requirements before running this tool.") from exc
    return yaml, jsonschema


def load_template(path: Path, yaml) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: template did not parse to a mapping")
    return data


# --------------------------------------------------------------------------- #
# Layer 1: structure (JSON Schema)
# --------------------------------------------------------------------------- #
def schema_errors(template: dict[str, Any], jsonschema) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors: list[str] = []
    for error in sorted(validator.iter_errors(template), key=lambda e: list(e.absolute_path)):
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        errors.append(f"schema[{location}]: {error.message}")
    return errors


# --------------------------------------------------------------------------- #
# Layer 2: cross-field semantics (not expressible in JSON Schema)
# --------------------------------------------------------------------------- #
def fsm_names(template: dict[str, Any]) -> list[str]:
    return [str(fsm.get("name")) for fsm in template.get("fsm_processes", []) if isinstance(fsm, dict)]


def semantic_errors(template: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    names = fsm_names(template)

    if len(names) != len(set(names)):
        errors.append("fsm_processes: duplicate FSM names found")

    declared = template.get("fsm_relationships", {}).get("fsm_count")
    if isinstance(declared, int) and declared != len(names):
        errors.append(f"fsm_relationships: fsm_count={declared} but fsm_processes defines {len(names)} FSMs")

    # every FSM has exactly one timing entry, and no timing entry names an unknown FSM
    timing_fsms = [
        str(entry.get("fsm"))
        for entry in template.get("timing_model", {}).get("fsm_process_delays", [])
        if isinstance(entry, dict)
    ]
    for missing in sorted(set(names) - set(timing_fsms)):
        errors.append(f"timing_model: missing fsm_process_delays entry for FSM `{missing}`")
    for extra in sorted(set(timing_fsms) - set(names)):
        errors.append(f"timing_model: fsm_process_delays entry for unknown FSM `{extra}`")
    for entry in template.get("timing_model", {}).get("fsm_process_delays", []):
        if not isinstance(entry, dict):
            continue
        ops = entry.get("operations", [])
        if not ops:
            errors.append(f"timing_model.{entry.get('fsm')}: missing `operations`")
            continue
        for op in ops:
            if not isinstance(op, dict) or "cycles" not in op or "ns" not in op:
                errors.append(f"timing_model.{entry.get('fsm')}: each operation must include `cycles` and `ns`")
                break

    errors += timing_coherence_errors(template)

    # every FSM must appear in at least one test scenario's fsm_coverage
    covered = " ".join(
        str(item)
        for scenario in template.get("test_scenarios", [])
        if isinstance(scenario, dict)
        for item in scenario.get("fsm_coverage", [])
    )
    for name in names:
        if name and f"{name}." not in covered and name not in covered.split():
            errors.append(f"test_scenarios: no scenario covers FSM `{name}`")

    return errors


def timing_coherence_errors(template: dict[str, Any]) -> list[str]:
    """The template's own timing numbers must be internally consistent, so a
    transcription slip (e.g. cycles: 2 with ns: 5) fails the gate rather than
    silently seeding a wrong delay into a generated model. This checks the
    template against itself; it does not assert the model uses these numbers.
    """
    errors: list[str] = []
    timing = template.get("timing_model", {})
    clock = timing.get("clock_mhz")
    cycle_ns = timing.get("cycle_time_ns")
    if isinstance(clock, (int, float)) and clock > 0 and isinstance(cycle_ns, (int, float)):
        # one cycle at C MHz is 1000/C ns
        if round(1000 / clock) != cycle_ns:
            errors.append(
                f"timing_model: cycle_time_ns={cycle_ns} disagrees with clock_mhz={clock} "
                f"(expected {round(1000 / clock)} ns/cycle)"
            )
    if not isinstance(cycle_ns, (int, float)):
        return errors

    for entry in timing.get("fsm_process_delays", []):
        if not isinstance(entry, dict):
            continue
        for op in entry.get("operations", []):
            if not isinstance(op, dict):
                continue
            cycles, ns = op.get("cycles"), op.get("ns")
            if isinstance(cycles, (int, float)) and isinstance(ns, (int, float)) and ns != cycles * cycle_ns:
                errors.append(
                    f"timing_model.{entry.get('fsm')}.{op.get('name')}: "
                    f"ns={ns} but cycles={cycles} x cycle_time_ns={cycle_ns} = {cycles * cycle_ns}"
                )
    for path in timing.get("end_to_end_paths", []):
        if not isinstance(path, dict):
            continue
        cycles, ns = path.get("cycles"), path.get("ns")
        if isinstance(cycles, (int, float)) and isinstance(ns, (int, float)) and ns != cycles * cycle_ns:
            errors.append(
                f"timing_model.end_to_end_paths.{path.get('name')}: "
                f"ns={ns} but cycles={cycles} x cycle_time_ns={cycle_ns} = {cycles * cycle_ns}"
            )
    return errors


# --------------------------------------------------------------------------- #
# arbitration_ip sub-contract: concepts that must be modeled by name.
# Operates on the flattened set of keys/values from the parsed template, so it
# never touches raw text.
# --------------------------------------------------------------------------- #
ARBITRATION_REQUIRED = [
    "topology",
    "port_mode",
    "pending_bitmaps",
    "device_burst_available",
    "tenant_burst_available",
    "sq_burst_available",
    "issue_count_rule",
    "issue_pipeline",
    "pending_count_read",
    "burst_read",
    "min_burst_calculation",
    "downstream_issue_request",
    "burst_debit",
]
ARBITRATION_METRICS = ["burst_stalls", "downstream_requests", "issued_commands", "issue_count"]


def flatten_tokens(obj: Any) -> set[str]:
    """Collect every key and scalar string from a parsed template."""
    tokens: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            tokens.add(str(key))
            tokens |= flatten_tokens(value)
    elif isinstance(obj, list):
        for item in obj:
            tokens |= flatten_tokens(item)
    elif isinstance(obj, str):
        tokens.add(obj)
    return tokens


def arbitration_errors(template: dict[str, Any]) -> list[str]:
    if template.get("ip", {}).get("name") != "arbitration_ip":
        return []
    tokens = flatten_tokens(template)
    metrics = set(template.get("performance_model", {}).get("metrics", []))
    errors = [f"arbitration_ip contract: missing `{token}`" for token in ARBITRATION_REQUIRED if token not in tokens]
    errors += [
        f"arbitration_ip contract: performance metrics missing `{metric}`"
        for metric in ARBITRATION_METRICS
        if metric not in metrics
    ]
    return errors


def lint_template(template: dict[str, Any], jsonschema) -> list[str]:
    structure = schema_errors(template, jsonschema)
    # Cross-field checks assume a well-formed shape; skip them if structure failed.
    if structure:
        return structure
    return semantic_errors(template) + arbitration_errors(template)


def lint_file(path: Path) -> list[str]:
    yaml, jsonschema = require_deps()
    try:
        template = load_template(path, yaml)
    except ValueError as exc:
        return [str(exc)]
    return lint_template(template, jsonschema)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate IP model templates against the contract.")
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
