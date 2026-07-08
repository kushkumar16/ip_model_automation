#!/usr/bin/env python3
"""Extract a draft IP template from a design-level document (DLD) markdown file.

This is the front-end of the generation pipeline: it turns a human-authored
``docs/<ip>_dld.md`` into a *draft* ``templates/<ip>.template.draft.yaml`` plus a
``reports/<ip>.gaps.md`` report.

The extractor is deliberately best-effort. DLDs vary in format and routinely omit
details the template schema requires (commands, test scenarios, invariants, ...).
Rather than invent that behavior, the extractor:

  * fills everything it can parse from the DLD (FSMs, states, interfaces, timing),
  * leaves ``TODO_REVIEW`` markers for schema-required fields the DLD does not
    state, and
  * writes a gaps report listing the DLD "Open Items" and every ``TODO_REVIEW``.

A human or LLM then completes the draft (replacing every ``TODO_REVIEW``), lints
it, checks DLD coverage, and promotes it to the golden
``templates/<ip>.template.yaml``. See ``skills/ip-model-generation``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

TODO = "TODO_REVIEW"


def require_yaml():
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - environment guard
        raise SystemExit(
            "PyYAML is required. Install project requirements before running this tool."
        ) from exc
    return yaml


def _make_dumper(yaml):
    """Build a PyYAML dumper that indents block sequences under their key.

    The template linter (`tools/template_lint.py`) is text-based and expects
    list items indented beneath their parent key (``  - name: ...``). Default
    PyYAML emits them at the parent's indentation, which the linter reads as
    "no list items". Indenting block sequences keeps generated drafts
    lint-shaped.
    """

    class IndentDumper(yaml.Dumper):
        def increase_indent(self, flow=False, indentless=False):
            return super().increase_indent(flow, False)

    return IndentDumper


def snake(name: str) -> str:
    cleaned = re.sub(r"\W+", "_", name.strip().lower()).strip("_")
    if not cleaned:
        return "unnamed"
    if cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned


# --------------------------------------------------------------------------- #
# Low-level markdown helpers
# --------------------------------------------------------------------------- #

def split_blocks(lines: list[str], pattern: re.Pattern[str]) -> list[tuple[str, list[str]]]:
    """Group lines into (heading_capture, block_lines) for headings matching pattern.

    A block runs from a matching heading until the next ``##``/``###`` heading.
    """
    blocks: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for line in lines:
        match = pattern.match(line)
        if match:
            if current is not None:
                blocks.append(current)
            current = (match.group(1).strip(), [])
            continue
        if current is not None:
            if re.match(r"^#{2,3}\s", line):  # next heading ends the block
                blocks.append(current)
                current = None
            else:
                current[1].append(line)
    if current is not None:
        blocks.append(current)
    return blocks


def bullet_tokens_after(block: list[str], label: str) -> list[str]:
    """Return bullet items appearing under a ``<label>:`` line within a block."""
    tokens: list[str] = []
    in_section = False
    for line in block:
        stripped = line.strip()
        if stripped.lower().rstrip(":") == label.lower() and stripped.endswith(":"):
            in_section = True
            continue
        if not in_section:
            continue
        if not stripped:
            continue
        if stripped.startswith("- "):
            tokens.append(stripped[2:].strip())
        else:
            break  # a non-bullet, non-blank line ends the sub-section
    return tokens


def strip_code(token: str) -> str:
    return token.strip().strip("`").strip()


# --------------------------------------------------------------------------- #
# Section extractors
# --------------------------------------------------------------------------- #

FSM_HEADING = re.compile(r"^#{3}\s+[\d.]*\s*(.*?)\s+FSM\s*$", re.IGNORECASE)
IFACE_HEADING = re.compile(r"^#{3}\s+[\d.]*\s*(.*?)\s+Interfaces?\s*$", re.IGNORECASE)


def extract_fsms(lines: list[str]) -> list[dict[str, Any]]:
    fsms: list[dict[str, Any]] = []
    for raw_name, block in split_blocks(lines, FSM_HEADING):
        name = snake(raw_name)
        states = [snake_state(strip_code(t)) for t in bullet_tokens_after(block, "States")]
        states = [s for s in states if s]
        purpose = first_role(block) or TODO
        fsms.append({"name": name, "purpose": purpose, "states": states, "raw": raw_name})
    return fsms


def snake_state(token: str) -> str:
    # States are usually already UPPER_SNAKE; keep them verbatim if so.
    word = re.split(r"\s", token.strip())[0] if token.strip() else ""
    return re.sub(r"[^A-Za-z0-9_]", "", word).upper()


def first_role(block: list[str]) -> str | None:
    for line in block:
        stripped = line.strip()
        if stripped.lower().startswith("role:"):
            return stripped.split(":", 1)[1].strip()
        if stripped.lower().startswith("purpose:"):
            return stripped.split(":", 1)[1].strip()
    return None


def extract_interfaces(lines: list[str]) -> list[dict[str, Any]]:
    interfaces: list[dict[str, Any]] = []
    for raw_name, block in split_blocks(lines, IFACE_HEADING):
        base = snake(raw_name)
        name = base if base.endswith("_if") else f"{base}_if"
        fields = [snake(strip_code(t).split()[0]) for t in bullet_tokens_after(block, "Fields") if strip_code(t)]
        direction = guess_direction(raw_name, block)
        interfaces.append(
            {
                "name": name,
                "type": TODO,
                "direction": direction,
                "transactions": [
                    {
                        "name": f"{base}_access",
                        "fields": fields or [TODO],
                        "handshake": TODO,
                        "timing_notes": TODO,
                    }
                ],
                "raw": raw_name,
            }
        )
    return interfaces


def guess_direction(raw_name: str, block: list[str]) -> str:
    text = (raw_name + " " + " ".join(block)).lower()
    if "output" in text or "response" in text or "completion" in text or "interrupt out" in text:
        return "output"
    if "input" in text or "ingress" in text or "source" in text or "register" in text:
        return "input"
    return TODO


def extract_clock(text: str) -> tuple[int, int, list[str]]:
    gaps: list[str] = []
    mhz_match = re.search(r"([\d.]+)\s*MHz", text)
    ns_match = re.search(r"[Cc]ycle time:\s*([\d.]+)\s*ns", text)
    clock_mhz = int(float(mhz_match.group(1))) if mhz_match else 0
    cycle_ns = int(float(ns_match.group(1))) if ns_match else 0
    if not clock_mhz:
        clock_mhz = 500
        gaps.append("timing_model.clock_mhz not stated in DLD; defaulted to 500")
    if not cycle_ns:
        cycle_ns = 2
        gaps.append("timing_model.cycle_time_ns not stated in DLD; defaulted to 2")
    return clock_mhz, cycle_ns, gaps


OP_RE = re.compile(r"([A-Za-z][A-Za-z0-9/ \-]*?):\s*(\d+)\s*cycles?\s*=\s*(\d+)\s*ns")


def extract_timing_table(lines: list[str], fsm_names: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Parse the ``| FSM/process | Runs as | Delay model |`` table into ops per FSM."""
    ops_by_fsm: dict[str, list[dict[str, Any]]] = {}
    in_table = False
    for line in lines:
        if "FSM/process" in line and "Delay model" in line:
            in_table = True
            continue
        if in_table:
            if not line.strip().startswith("|"):
                if line.strip():
                    in_table = False
                continue
            if set(line.strip()) <= set("|-: "):  # separator row
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            fsm_name = snake(re.sub(r"\bFSMs?\b|\bprocess\b", "", cells[0], flags=re.IGNORECASE))
            ops = [
                {"name": snake(m.group(1)), "cycles": int(m.group(2)), "ns": int(m.group(3))}
                for m in OP_RE.finditer(cells[-1])
            ]
            if fsm_name and ops:
                ops_by_fsm[fsm_name] = ops
    # Keep only FSMs that exist; the linter forbids timing for unknown FSMs.
    return {name: ops_by_fsm[name] for name in fsm_names if name in ops_by_fsm}


def extract_open_items(lines: list[str]) -> list[str]:
    for raw_name, block in split_blocks(lines, re.compile(r"^#{2}\s+[\d.]*\s*(Open Items.*)$", re.IGNORECASE)):
        return [strip_code(l.strip()[2:]) for l in block if l.strip().startswith("- ")]
    return []


def extract_fsm_count(text: str, fallback: int) -> int:
    match = re.search(r"Total FSM/processes:\s*(\d+)", text)
    return int(match.group(1)) if match else fallback


# --------------------------------------------------------------------------- #
# Draft assembly
# --------------------------------------------------------------------------- #

def build_draft(ip_name: str, text: str) -> tuple[dict[str, Any], list[str]]:
    lines = text.splitlines()
    gaps: list[str] = []

    fsms = extract_fsms(lines)
    if not fsms:
        gaps.append("fsm_processes: no `### N.M <Name> FSM` sections found in DLD")
    fsm_names = [f["name"] for f in fsms]

    interfaces = extract_interfaces(lines)
    if not interfaces:
        gaps.append("interfaces: no `### N.M <Name> Interface` sections found in DLD")

    clock_mhz, cycle_ns, clock_gaps = extract_clock(text)
    gaps.extend(clock_gaps)
    timing_ops = extract_timing_table(lines, fsm_names)

    description = extract_description(lines) or TODO
    if description == TODO:
        gaps.append("ip.description not found; using TODO_REVIEW")

    draft: dict[str, Any] = {
        "ip": {
            "name": ip_name,
            "domain": f"{TODO}_domain",
            "description": description,
            "clock_domains": [{"name": "core_clk", "frequency_mhz": clock_mhz}],
            "reset": {"type": "sync", "behavior": f"{TODO}_reset_behavior"},
        },
        "interfaces": [strip_raw(i) for i in interfaces]
        or [placeholder_interface()],
        "commands": [placeholder_command()],
        "fsm_processes": [build_fsm_entry(f, interfaces) for f in fsms]
        or [placeholder_fsm()],
        "fsm_relationships": {
            "fsm_count": extract_fsm_count(text, len(fsm_names)),
            "parallel_processes": [fsm_names or [f"{TODO}_fsm"]],
            "sequential_paths": [
                {
                    "name": f"{TODO}_primary_path",
                    "path": [f"{n}.{(dict_states(fsms, n) or ['STATE'])[0]}" for n in fsm_names[:3]]
                    or [f"{TODO}_fsm.STATE"],
                    "description": TODO,
                }
            ],
            "gating_relationships": [],
        },
        "timing_model": build_timing(clock_mhz, cycle_ns, fsm_names, timing_ops, gaps),
        "functionality_model": {
            "implemented_in_simpy": True,
            "state_variables": ["fsm_state", "transition_counts", f"{TODO}_state"],
            "apis": ["run", "get_metrics", f"{TODO}_api"],
            "invariants": [f"{TODO}_invariant"],
            "ignored_payload_details": [],
        },
        "performance_model": {
            "language": "python",
            "library": "simpy",
            "abstraction_level": "transaction",
            "model_functionality": True,
            "model_fsm_processes": True,
            "model_data_payloads": False,
            "model_interface_backpressure": True,
            "model_arbitration": False,
            "model_queue_occupancy": True,
            "model_latency_sources": [f"{TODO}_latency_source"],
            "metrics": ["transition_counts", f"{TODO}_metric"],
        },
        "test_scenarios": [build_placeholder_scenario(fsms)],
    }

    # Record every schema-required field left as TODO for the gaps report.
    gaps.append("commands: not derivable from DLD; TODO_REVIEW placeholder emitted")
    gaps.append("test_scenarios: author from DLD behavior; TODO_REVIEW placeholder emitted")
    gaps.append("functionality_model.invariants/apis/state_variables: complete from DLD")
    missing_timing = [n for n in fsm_names if n not in timing_ops]
    if missing_timing:
        gaps.append(
            "timing_model: no DLD timing row for FSMs "
            f"{', '.join(missing_timing)}; TODO_REVIEW operations emitted"
        )
    return draft, gaps


def extract_description(lines: list[str]) -> str | None:
    for raw_name, block in split_blocks(lines, re.compile(r"^#{2}\s+[\d.]*\s*(Purpose.*)$", re.IGNORECASE)):
        for line in block:
            if line.strip():
                return line.strip()
    return None


def dict_states(fsms: list[dict[str, Any]], name: str) -> list[str]:
    for f in fsms:
        if f["name"] == name:
            return f["states"]
    return []


def strip_raw(interface: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in interface.items() if k != "raw"}


def build_fsm_entry(fsm: dict[str, Any], interfaces: list[dict[str, Any]]) -> dict[str, Any]:
    states = fsm["states"] or ["RESET", "IDLE"]
    first, second = states[0], (states[1] if len(states) > 1 else states[0])
    return {
        "name": fsm["name"],
        "purpose": fsm["purpose"],
        "simpy_process": True,
        "states": states,
        "transitions": [
            {
                "from": first,
                "to": second,
                "condition": TODO,
                "actions": [TODO],
                "latency_cycles": 1,
            }
        ],
        "functional_responsibilities": [TODO],
        "interfaces_touched": [interfaces[0]["name"]] if interfaces else [TODO],
        "queues_used": [TODO],
        "resources_used": [TODO],
    }


def build_timing(
    clock_mhz: int,
    cycle_ns: int,
    fsm_names: list[str],
    timing_ops: dict[str, list[dict[str, Any]]],
    gaps: list[str],
) -> dict[str, Any]:
    delays = []
    for name in fsm_names or [f"{TODO}_fsm"]:
        ops = timing_ops.get(name) or [{"name": f"{TODO}_op", "cycles": 1, "ns": cycle_ns}]
        delays.append({"fsm": name, "mode": "parallel_process", "operations": ops})
    return {
        "clock_mhz": clock_mhz,
        "cycle_time_ns": cycle_ns,
        "assumption": f"Hard-coded starter timing extracted from DLD ({TODO}: review).",
        "fsm_process_delays": delays,
        "end_to_end_paths": [
            {
                "name": f"{TODO}_end_to_end_path",
                "cycles": 1,
                "ns": cycle_ns,
                "path": [f"{TODO}_operation"],
            }
        ],
    }


def build_placeholder_scenario(fsms: list[dict[str, Any]]) -> dict[str, Any]:
    coverage = []
    for f in fsms:
        state = (f["states"] or ["STATE"])[0]
        coverage.append(f"{f['name']}.{state}")
    return {
        "name": f"{TODO}_scenario",
        "description": TODO,
        "input_sequence": [TODO],
        "expected_functional_behavior": [TODO],
        "expected_performance_properties": [TODO],
        "fsm_coverage": coverage or [f"{TODO}_fsm.STATE"],
    }


def placeholder_interface() -> dict[str, Any]:
    return {
        "name": f"{TODO}_if",
        "type": TODO,
        "direction": TODO,
        "transactions": [{"name": f"{TODO}_txn", "fields": [TODO], "handshake": TODO, "timing_notes": TODO}],
    }


def placeholder_command() -> dict[str, Any]:
    return {
        "name": f"{TODO}_COMMAND",
        "description": TODO,
        "fields": ["cmd_id"],
        "valid_conditions": [TODO],
        "completion_conditions": [TODO],
        "error_conditions": [TODO],
        "functional_effects": [TODO],
        "timing_effects": [TODO],
    }


def placeholder_fsm() -> dict[str, Any]:
    return build_fsm_entry({"name": f"{TODO}_fsm", "purpose": TODO, "states": ["RESET", "IDLE"]}, [])


# --------------------------------------------------------------------------- #
# Gaps report
# --------------------------------------------------------------------------- #

def render_gaps_report(ip_name: str, draft: dict[str, Any], gaps: list[str], open_items: list[str]) -> str:
    fsms = [f["name"] for f in draft["fsm_processes"]]
    interfaces = [i["name"] for i in draft["interfaces"]]
    lines = [
        f"# Gaps Report: {ip_name}",
        "",
        "Generated by `tools/dld_to_template.py`. Resolve every item before promoting",
        f"`templates/{ip_name}.template.draft.yaml` to `templates/{ip_name}.template.yaml`.",
        "",
        "## Extracted",
        "",
        f"- FSMs ({len(fsms)}): {', '.join(fsms)}",
        f"- Interfaces ({len(interfaces)}): {', '.join(interfaces)}",
        f"- Declared fsm_count: {draft['fsm_relationships']['fsm_count']}",
        "",
        "## Missing / To Review",
        "",
    ]
    lines += [f"- {g}" for g in gaps] or ["- (none)"]
    lines += ["", "## DLD Open Items", ""]
    lines += [f"- {item}" for item in open_items] or ["- (none stated in DLD)"]
    lines += [
        "",
        "## How To Resolve",
        "",
        "1. Replace every `TODO_REVIEW` in the draft using only DLD-stated behavior.",
        "2. If the DLD does not state a detail, choose a conservative default and note",
        "   it here rather than inventing silent behavior.",
        "3. Run `python tools/template_lint.py templates/" + ip_name + ".template.draft.yaml`.",
        "4. Run `python tools/check_template_coverage.py templates/" + ip_name
        + ".template.draft.yaml docs/" + ip_name + "_dld.md`.",
        "5. Promote the draft to the golden template name and re-run validation.",
        "",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def ip_name_from_path(path: Path) -> str:
    stem = path.name
    for suffix in ("_dld.md", ".dld.md", ".md"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return path.stem


def _load_doc_renderer():
    import importlib.util

    path = Path(__file__).resolve().parent / "render_template_doc.py"
    spec = importlib.util.spec_from_file_location("render_template_doc", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"cannot load tool: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_draft_doc(draft: dict[str, Any], draft_path: Path, reports_dir: Path) -> Path:
    """Render a readable HTML view of the draft next to the gaps report."""
    doc_tool = _load_doc_renderer()
    ip_name = draft["ip"]["name"]
    markdown = doc_tool.render_document(draft, draft_path)
    doc = doc_tool.markdown_to_html(markdown, f"{ip_name} — Template Overview (DRAFT)")
    doc_path = reports_dir / "template_docs" / f"{ip_name}.template.draft.html"
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(doc, encoding="utf-8")
    return doc_path


def write_outputs(dld_path: Path, templates_dir: Path, reports_dir: Path) -> tuple[Path, Path, Path]:
    yaml = require_yaml()
    dumper = _make_dumper(yaml)
    text = dld_path.read_text(encoding="utf-8")
    ip_name = ip_name_from_path(dld_path)

    draft, gaps = build_draft(ip_name, text)
    open_items = extract_open_items(text.splitlines())

    templates_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    draft_path = templates_dir / f"{ip_name}.template.draft.yaml"
    header = (
        f"# DRAFT template for {ip_name} generated by tools/dld_to_template.py.\n"
        f"# Replace every {TODO} using only DLD-stated behavior, then promote to\n"
        f"# templates/{ip_name}.template.yaml. See reports/{ip_name}.gaps.md.\n"
    )
    body = yaml.dump(draft, Dumper=dumper, sort_keys=False, default_flow_style=False, width=100)
    draft_path.write_text(header + body, encoding="utf-8")

    report_path = reports_dir / f"{ip_name}.gaps.md"
    report_path.write_text(render_gaps_report(ip_name, draft, gaps, open_items), encoding="utf-8")

    doc_path = write_draft_doc(draft, draft_path, reports_dir)
    return draft_path, report_path, doc_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dld", type=Path, help="Path to docs/<ip>_dld.md")
    parser.add_argument("--templates-dir", type=Path, default=Path("templates"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)

    if not args.dld.is_file():
        raise SystemExit(f"DLD not found: {args.dld}")

    draft_path, report_path, doc_path = write_outputs(args.dld, args.templates_dir, args.reports_dir)
    print(f"wrote draft:  {draft_path}")
    print(f"wrote report: {report_path}")
    print(f"wrote doc:    {doc_path}")
    print("Next: resolve TODO_REVIEW markers, lint, check coverage, then promote.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
