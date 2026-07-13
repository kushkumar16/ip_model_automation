#!/usr/bin/env python3
"""Generate an LLM prompt pack from a reviewed IP model template."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any


def require_yaml():
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("PyYAML is required. Install project requirements before running this tool.") from exc
    return yaml


def snake_to_camel(name: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[_\-\s]+", name) if part)


def load_template(path: Path) -> dict[str, Any]:
    yaml = require_yaml()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: template did not parse to a mapping")
    return data


def bullet_list(values: list[Any], indent: str = "- ") -> list[str]:
    if not values:
        return [f"{indent}none"]
    return [f"{indent}{value}" for value in values]


def render_prompt_pack(template: dict[str, Any], template_path: Path) -> str:
    ip_name = template["ip"]["name"]
    class_name = f"{snake_to_camel(ip_name)}Model"
    lines: list[str] = [
        f"# Prompt Pack: {ip_name}",
        "",
        "Use this bundle to generate or review a SimPy transaction-level delay model and unit tests.",
        "",
        "## Non-Negotiable Source Rules",
        "",
        "- Treat the reviewed template as the source of truth.",
        "- Use the DLD only to author or review the template, not to infer missing model behavior.",
        "- Do not invent FSMs, protocols, queues, resources, timing values, side effects, or tests"
        " not represented in the template.",
        "- If the template is incomplete, stop and report the missing fields.",
        "- Keep one flat Python model file under `src/ip_model_automation/`.",
        "- Do not create SystemC models, per-IP folders, `perf_model.py`, or `functional_model.py`.",
        "",
        "## Coding Style",
        "",
        "- Follow `skills/ip-model-generation/references/coding_style.md` (ruff-enforced, config in `ruff.toml`):",
        "  120-column formatting, double quotes, sorted imports (stdlib / third-party / first-party).",
        f"- Model class `{class_name}` with one `<fsm>_process` method per template FSM;",
        "  constructor takes `<operation>_latency` parameters and ends with `log_level`/`log_file`.",
        "- Track a `metrics` counter dict (snake_case keys) and log via `get_ip_logger` with `%s` formatting.",
        "- Check before finishing: `python tools/check_code_style.py` (use `--fix` to auto-repair).",
        "",
        "## Target Artifacts",
        "",
        f"- Template: `{template_path.as_posix()}`",
        f"- Model file: `src/ip_model_automation/{ip_name}.py`",
        f"- Model class: `{class_name}`",
        f"- Test file: `tests/test_{ip_name}.py`",
        "",
        "## IP Summary",
        "",
        f"- Domain: {template['ip'].get('domain')}",
        f"- Description: {template['ip'].get('description')}",
        "",
        "## Interfaces",
        "",
    ]

    for interface in template.get("interfaces", []):
        lines.append(f"- `{interface.get('name')}` ({interface.get('direction')}, {interface.get('type')})")
        for transaction in interface.get("transactions", []):
            lines.append(f"  - transaction `{transaction.get('name')}` fields: {transaction.get('fields', [])}")

    lines.extend(["", "## Commands", ""])
    for command in template.get("commands", []):
        lines.append(f"- `{command.get('name')}`: {command.get('description')}")
        lines.append(f"  - valid: {command.get('valid_conditions', [])}")
        lines.append(f"  - complete: {command.get('completion_conditions', [])}")
        lines.append(f"  - error: {command.get('error_conditions', [])}")

    lines.extend(["", "## FSM Processes", ""])
    for fsm in template.get("fsm_processes", []):
        lines.append(f"- `{fsm.get('name')}`: {fsm.get('purpose')}")
        lines.append(f"  - states: {fsm.get('states', [])}")
        lines.append(f"  - interfaces: {fsm.get('interfaces_touched', [])}")
        lines.append(f"  - queues: {fsm.get('queues_used', [])}")
        lines.append(f"  - resources: {fsm.get('resources_used', [])}")

    relationships = template.get("fsm_relationships", {})
    lines.extend(["", "## FSM Relationships", ""])
    lines.append(f"- FSM count: {relationships.get('fsm_count')}")
    lines.append(f"- Parallel processes: {relationships.get('parallel_processes', [])}")
    for path in relationships.get("sequential_paths", []):
        lines.append(f"- Sequential path `{path.get('name')}`: {path.get('path', [])}")

    lines.extend(["", "## Timing Model", ""])
    timing = template.get("timing_model", {})
    lines.append(f"- Clock MHz: {timing.get('clock_mhz')}")
    lines.append(f"- Cycle time ns: {timing.get('cycle_time_ns')}")
    for delay in timing.get("fsm_process_delays", []):
        lines.append(f"- `{delay.get('fsm')}` ({delay.get('mode')})")
        for op in delay.get("operations", []):
            lines.append(f"  - {op.get('name')}: {op.get('cycles')} cycles / {op.get('ns')} ns")

    lines.extend(["", "## Queues And Resources", ""])
    lines.extend(bullet_list([queue.get("name") for queue in template.get("queues", [])]))
    lines.extend(bullet_list([resource.get("name") for resource in template.get("resources", [])]))

    lines.extend(["", "## State, APIs, Metrics", ""])
    functionality = template.get("functionality_model", {})
    performance = template.get("performance_model", {})
    lines.append(f"- State variables: {functionality.get('state_variables', [])}")
    lines.append(f"- APIs: {functionality.get('apis', [])}")
    lines.append(f"- Invariants: {functionality.get('invariants', [])}")
    lines.append(f"- Metrics: {performance.get('metrics', [])}")

    lines.extend(["", "## Required Unit Tests From Template", ""])
    for scenario in template.get("test_scenarios", []):
        lines.append(f"- `{scenario.get('name')}`: {scenario.get('description')}")
        lines.append(f"  - input: {scenario.get('input_sequence', [])}")
        lines.append(f"  - expected functionality: {scenario.get('expected_functional_behavior', [])}")
        lines.append(f"  - expected performance: {scenario.get('expected_performance_properties', [])}")
        lines.append(f"  - FSM coverage: {scenario.get('fsm_coverage', [])}")

    lines.extend(
        [
            "",
            "## Validation Commands",
            "",
            "```powershell",
            "python tools\\template_lint.py templates\\*.template.yaml",
            f"python tools\\generate_model_scaffold.py templates\\{ip_name}.template.yaml"
            " --output-dir src\\ip_model_automation",
            "python tools\\report_model_coverage.py",
            "python tools\\validate_ip_flow.py",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def write_prompt_pack(template_path: Path, output_dir: Path | None, stdout: bool) -> Path | None:
    template = load_template(template_path)
    rendered = render_prompt_pack(template, template_path)
    if stdout:
        print(rendered)
        return None
    target_dir = output_dir or Path("prompt_packs")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{template['ip']['name']}.prompt.md"
    target.write_text(rendered, encoding="utf-8")
    return target


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("template", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args(argv)

    target = write_prompt_pack(args.template, args.output_dir, args.stdout)
    if target is not None:
        print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
