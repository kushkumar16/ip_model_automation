#!/usr/bin/env python3
"""Generate a SimPy model scaffold from a reviewed IP template."""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def _profile():
    """Load the target profile (paths + naming conventions) for this repo."""
    path = Path(__file__).resolve().parent / "target_profile.py"
    spec = importlib.util.spec_from_file_location("target_profile", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_profile(REPO_ROOT)


def require_yaml():
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("PyYAML is required. Install project requirements before running this tool.") from exc
    return yaml


def state_name(state: Any) -> str:
    """The name of a declared FSM state.

    Templates write states either as plain strings or as mappings carrying a
    description. Both are valid; the scaffold used to emit whichever it found,
    so a template using mappings produced `fsm_state[...] = {'name': 'RESET',
    'description': ...}` -- a dict where every tool that reads `fsm_state`
    expects the state's name.
    """
    if isinstance(state, dict):
        return str(state.get("name", ""))
    return str(state)


def sanitize_identifier(name: str) -> str:
    cleaned = re.sub(r"\W+", "_", name.strip().lower())
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned


def inline_repr(values: list[str]) -> str:
    return "{" + ", ".join(f"{value!r}: 0" for value in values) + "}"


def timing_operations(template: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    delays = template.get("timing_model", {}).get("fsm_process_delays", [])
    return {entry["fsm"]: entry.get("operations", []) for entry in delays if "fsm" in entry}


def command_accept_wait_points(template: dict[str, Any]) -> list[str]:
    """The `<fsm>.<STATE>` points a peer *submitting a command* waits at.

    `submit()` serves one kind of interface: an input whose wait_model names the
    peer as requester, and whose transactions carry commands. The command test
    matters -- a configuration interface can be peer-requested and inbound too
    (qos_config_if is), and its wait point has nothing to do with accepting a
    command. Carrying `cmd_id` is the behavioural test for "this is the command
    path", rather than trusting a `type:` spelling.
    """
    points: list[str] = []
    for interface in template.get("interfaces", []) or []:
        wait_model = interface.get("wait_model") or {}
        if interface.get("direction") != "input" or wait_model.get("requester") != "peer":
            continue
        carries_commands = any(
            "cmd_id" in (transaction.get("fields") or []) for transaction in (interface.get("transactions") or [])
        )
        if carries_commands:
            points.extend(str(point) for point in (wait_model.get("wait_points") or []))
    return points


def first_operation_cycles(operations: list[dict[str, Any]]) -> int:
    for operation in operations:
        cycles = operation.get("cycles")
        if isinstance(cycles, int) and cycles > 0:
            return cycles
    return 1


def render_scaffold(template: dict[str, Any]) -> str:
    ip = template["ip"]["name"]
    class_name = _profile().model_class(ip)
    fsm_processes = [fsm for fsm in template["fsm_processes"] if fsm.get("simpy_process") is True]
    timing_by_fsm = timing_operations(template)
    metrics = template.get("performance_model", {}).get("metrics", [])
    state_variables = template.get("functionality_model", {}).get("state_variables", [])
    queues = template.get("queues", [])
    resources = template.get("resources", [])

    lines: list[str] = [
        "from __future__ import annotations",
        "",
        "from collections import defaultdict, deque",
        "from typing import Any",
        "",
        "import simpy",
        "",
        "from .common import Command, get_ip_logger",
        "",
        "",
        f"class {class_name}:",
        f'    """Template-generated SimPy scaffold for {ip}.',
        "",
        "    Fill the TODO hooks using only behavior present in the reviewed",
        "    template. Keep the generated FSM/process structure intact unless the",
        "    template changes and is linted again.",
        '    """',
        "",
        '    def __init__(self, env: simpy.Environment, log_level: str = "WARNING", log_file: str = "run.log"):',
        "        self.env = env",
        f"        self.logger = get_ip_logger({ip!r}, log_level, log_file)",
        "        self.input_q = simpy.Store(env)",
        "        self.completed: list[tuple[float, Command]] = []",
        f"        self.metrics = defaultdict(int, {inline_repr([str(metric) for metric in metrics])})",
        "        self.fsm_state: dict[str, str] = {}",
        "        self.state: dict[str, Any] = {}",
    ]

    for variable in state_variables:
        safe_name = sanitize_identifier(str(variable))
        lines.append(f"        self.state[{safe_name!r}] = None")

    for queue in queues:
        queue_name = sanitize_identifier(str(queue.get("name", "queue")))
        lines.append(f"        self.{queue_name} = deque()")

    for resource in resources:
        resource_name = sanitize_identifier(str(resource.get("name", "resource")))
        capacity = resource.get("capacity") or 1
        lines.append(f"        self.{resource_name} = simpy.Resource(env, capacity={capacity})")

    for fsm in fsm_processes:
        method = sanitize_identifier(str(fsm["name"]))
        first_state = state_name((fsm.get("states") or ["IDLE"])[0])
        lines.append(f"        self.fsm_state[{fsm['name']!r}] = {first_state!r}")
        lines.append(f"        self.env.process(self.{method}_process())")

    accept_points = command_accept_wait_points(template)
    if accept_points:
        wait_point_note = f"        Complete it at the declared wait point: {', '.join(accept_points)}."
    else:
        wait_point_note = (
            "        This template declares no peer wait point on an input interface,"
            " so choose the state that holds the peer and complete it there."
        )

    lines.extend(
        [
            '        self.logger.info("initialized")',
            "",
            "",
            "    def submit(self, command: Command):",
            '        """Offer a command, returning the peer\'s accept.',
            "",
            "        The returned event must NOT be the `input_q.put`: an unbounded Store",
            "        completes a put at the instant of submission, so the peer would be",
            "        acknowledged at t=0 no matter what state this model is in, and a full",
            "        queue would stall nothing upstream. This scaffold used to emit exactly",
            "        that, and a generated model shipped with it.",
            "",
            wait_point_note,
            "",
            "        TODO: in the consuming process, take both halves and complete the",
            "        accept once the command has actually landed:",
            "",
            "            command, accepted = yield self.input_q.get()",
            "            ...  # hold the declared state across its declared cost",
            "            accepted.succeed()",
            '        """',
            '        self.logger.info("submit cmd=%s kind=%s", command.cmd_id, command.kind)',
            "        accepted = self.env.event()",
            "        self.input_q.put((command, accepted))",
            "        return accepted",
            "",
        ]
    )

    for fsm in fsm_processes:
        name = str(fsm["name"])
        method = sanitize_identifier(name)
        operations = timing_by_fsm.get(name, [])
        default_cycles = first_operation_cycles(operations)
        states = ", ".join(state_name(state) for state in fsm.get("states", []))
        lines.extend(
            [
                f"    def {method}_process(self):",
                f'        """FSM `{name}` states: {states}."""',
                "        while True:",
                f"            self.metrics[{name + '_ticks'!r}] += 1",
                f'            self.logger.debug("fsm={name} tick time=%s", self.env.now)',
                "            # TODO: implement transitions/actions from template fsm_processes.",
                "            # TODO: preserve queue/resource/interface behavior from the template.",
                f"            yield self.env.timeout({default_cycles})",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def load_template(path: Path) -> dict[str, Any]:
    yaml = require_yaml()
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: template did not parse to a mapping")
    return data


def write_scaffold(template_path: Path, output_dir: Path | None, stdout: bool) -> Path | None:
    template = load_template(template_path)
    rendered = render_scaffold(template)
    if stdout:
        print(rendered, end="")
        return None
    ip_name = template["ip"]["name"]
    target_dir = output_dir or _profile().model_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{ip_name}.py"
    target.write_text(rendered, encoding="utf-8")
    return target


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("template", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args(argv)

    target = write_scaffold(args.template, args.output_dir, args.stdout)
    if target is not None:
        print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
