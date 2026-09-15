#!/usr/bin/env python3
"""Generate a SystemC model scaffold from a reviewed IP template.

Sibling to ``generate_model_scaffold.py`` (SimPy), reusing its template-parsing
helpers so both backends read the exact same template the exact same way and
cannot drift apart on what a state, an operation, or a command wait point
means. Only the render target differs: a `.h`/`.cpp` SC_MODULE pair instead of
a single Python file, into ``systemc/models/`` instead of
``src/ip_model_automation/``.

Only IPs whose template declares ``ip.modeling_backends`` including
``systemc`` get a SystemC scaffold; an IP that omits the field, or names only
``simpy``, is untouched -- the default stays exactly what it was before this
tool existed.
"""

from __future__ import annotations

import argparse
import importlib.util
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
SYSTEMC_MODELS_DIR = REPO_ROOT / "systemc" / "models"


def _simpy_scaffold_module():
    """Import generate_model_scaffold.py's helpers without duplicating them."""
    path = Path(__file__).resolve().parent / "generate_model_scaffold.py"
    spec = importlib.util.spec_from_file_location("generate_model_scaffold", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def modeling_backends(template: dict[str, Any]) -> list[str]:
    """Declared backends for this IP; absent means SimPy only (the pre-existing default)."""
    return list(template.get("ip", {}).get("modeling_backends") or ["simpy"])


def cpp_identifier(name: str, helpers) -> str:
    """A valid C++ identifier -- sanitize_identifier's rules already produce
    one (leading underscore on a digit, non-word chars collapsed), so this is
    just naming the reuse, not new logic."""
    return helpers.sanitize_identifier(name)


def render_header(template: dict[str, Any], class_name: str, fsm_processes: list[dict[str, Any]], helpers) -> str:
    ip = template["ip"]["name"]
    lines: list[str] = [
        "#pragma once",
        "",
        "#include <map>",
        "#include <string>",
        "",
        "#include <systemc.h>",
        "",
        f"// Template-generated SystemC scaffold for {ip}.",
        "//",
        "// Fill the TODO hooks using only behavior present in the reviewed",
        "// template. Keep the generated FSM/process structure intact unless the",
        "// template changes and is linted again.",
        f"class {class_name} : public sc_module {{",
        "  public:",
        f"    SC_HAS_PROCESS({class_name});",
        f"    explicit {class_name}(sc_module_name name);",
        "",
        "    // Mirrors the SimPy model's fsm_state: one entry per live FSM process,",
        "    // updated at every transition so a testbench can trace it the same way.",
        "    std::map<std::string, std::string> fsm_state;",
        "    std::map<std::string, long> metrics;",
        "",
        "  private:",
    ]
    for fsm in fsm_processes:
        method = cpp_identifier(str(fsm["name"]), helpers)
        lines.append(f"    void {method}_process();")
    lines.extend(
        [
            "",
            "    // TODO: declare command queues / sc_event handshakes for each",
            "    // interface's wait_model, and any state variables from",
            "    // functionality_model.state_variables.",
            "};",
            "",
        ]
    )
    return "\n".join(lines)


def render_source(
    template: dict[str, Any],
    class_name: str,
    header_name: str,
    fsm_processes: list[dict[str, Any]],
    timing_by_fsm: dict[str, list[dict[str, Any]]],
    helpers,
) -> str:
    lines: list[str] = [
        f'#include "{header_name}"',
        "",
        f"{class_name}::{class_name}(sc_module_name name) : sc_module(name) {{",
    ]
    for fsm in fsm_processes:
        method = cpp_identifier(str(fsm["name"]), helpers)
        first_state = helpers.state_name((fsm.get("states") or ["IDLE"])[0])
        lines.append(f'    fsm_state["{fsm["name"]}"] = "{first_state}";')
        lines.append(f"    SC_THREAD({method}_process);")
    lines.extend(["}", ""])

    for fsm in fsm_processes:
        name = str(fsm["name"])
        method = cpp_identifier(name, helpers)
        operations = timing_by_fsm.get(name, [])
        default_cycles = helpers.first_operation_cycles(operations)
        states = ", ".join(helpers.state_name(state) for state in fsm.get("states", []))
        lines.extend(
            [
                f"void {class_name}::{method}_process() {{",
                f"    // FSM `{name}` states: {states}.",
                "    while (true) {",
                f'        metrics["{name}_ticks"]++;',
                "        // TODO: implement transitions/actions from template fsm_processes.",
                "        // TODO: preserve queue/resource/interface behavior from the template.",
                f"        wait({default_cycles}, SC_NS);",
                "    }",
                "}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_scaffold(template: dict[str, Any]) -> tuple[str, str, str]:
    """Returns (header_text, source_text, ip_name)."""
    helpers = _simpy_scaffold_module()
    ip = template["ip"]["name"]
    class_name = helpers._profile().model_class(ip)
    fsm_processes = [fsm for fsm in template["fsm_processes"] if fsm.get("simpy_process") is True]
    timing_by_fsm = helpers.timing_operations(template)
    header_name = f"{ip}.h"

    header = render_header(template, class_name, fsm_processes, helpers)
    source = render_source(template, class_name, header_name, fsm_processes, timing_by_fsm, helpers)
    return header, source, ip


def write_scaffold(template_path: Path, output_dir: Path | None, stdout: bool) -> tuple[Path, Path] | None:
    helpers = _simpy_scaffold_module()
    template = helpers.load_template(template_path)
    backends = modeling_backends(template)
    if "systemc" not in backends:
        raise SystemExit(
            f"{template_path}: ip.modeling_backends is {backends!r}, which does not include 'systemc' -- "
            "add it to the template before generating a SystemC scaffold."
        )
    header_text, source_text, ip_name = render_scaffold(template)
    if stdout:
        print(f"// ---- {ip_name}.h ----")
        print(header_text, end="")
        print(f"// ---- {ip_name}.cpp ----")
        print(source_text, end="")
        return None
    target_dir = output_dir or SYSTEMC_MODELS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    header_path = target_dir / f"{ip_name}.h"
    source_path = target_dir / f"{ip_name}.cpp"
    header_path.write_text(header_text, encoding="utf-8")
    source_path.write_text(source_text, encoding="utf-8")
    return header_path, source_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("template", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args(argv)

    targets = write_scaffold(args.template, args.output_dir, args.stdout)
    if targets is not None:
        header_path, source_path = targets
        print(f"wrote {header_path}")
        print(f"wrote {source_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
