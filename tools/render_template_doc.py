#!/usr/bin/env python3
"""Render an IP model template into a human-readable Markdown document.

The YAML template stays the source of truth for generation; the rendered
document is a derived, review-friendly view of the same content. Nothing is
inferred or added during rendering.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "templates"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "template_docs"


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
        raise SystemExit(f"{path}: template did not parse to a mapping")
    return data


def md_escape(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(md_escape(item) for item in value) if value else "—"
    if isinstance(value, dict):
        return ", ".join(f"{key}={md_escape(val)}" for key, val in value.items())
    return str(value).replace("|", "\\|").replace("\n", " ")


def md_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(md_escape(cell) for cell in row) + " |")
    return lines


def bullets(values: list[Any]) -> list[str]:
    if not values:
        return ["- none"]
    return [f"- {md_escape(value)}" for value in values]


def render_ip_section(template: dict[str, Any]) -> list[str]:
    ip = template.get("ip", {})
    lines = [
        f"# {ip.get('name', 'unknown_ip')} — Template Overview",
        "",
        f"> Rendered from the YAML template. The YAML file remains the source of truth.",
        "",
        f"**Domain:** {md_escape(ip.get('domain'))}",
        "",
        f"{ip.get('description', '')}",
        "",
        "## Clocks And Reset",
        "",
    ]
    clock_domains = ip.get("clock_domains", [])
    lines += md_table(
        ["Clock Domain", "Frequency (MHz)"],
        [[clock.get("name"), clock.get("frequency_mhz")] for clock in clock_domains],
    )
    reset = ip.get("reset", {})
    lines += [
        "",
        f"**Reset:** type `{md_escape(reset.get('type'))}`, behavior `{md_escape(reset.get('behavior'))}`",
    ]
    return lines


def render_subsystem(template: dict[str, Any]) -> list[str]:
    subsystem = template.get("subsystem")
    if not subsystem:
        return []
    lines = ["", "## Subsystem Composition", "", "### Members", ""]
    lines += md_table(
        ["IP", "Model Class", "Role"],
        [[member.get("ip"), member.get("model"), member.get("role")] for member in subsystem.get("members", [])],
    )
    lines += ["", "### Connections", ""]
    lines += md_table(
        ["From", "To", "Payload"],
        [[conn.get("from"), conn.get("to"), conn.get("payload")] for conn in subsystem.get("connections", [])],
    )
    return lines


def render_interfaces(template: dict[str, Any]) -> list[str]:
    lines = ["", "## Interfaces", ""]
    for interface in template.get("interfaces", []):
        lines += [
            f"### `{interface.get('name')}`",
            "",
            f"Type `{md_escape(interface.get('type'))}`, direction `{md_escape(interface.get('direction'))}`, "
            f"clock domain `{md_escape(interface.get('clock_domain'))}`.",
            "",
        ]
        lines += md_table(
            ["Transaction", "Fields", "Handshake", "Timing Notes"],
            [
                [txn.get("name"), txn.get("fields"), txn.get("handshake"), txn.get("timing_notes")]
                for txn in interface.get("transactions", [])
            ],
        )
        lines.append("")
    return lines


def render_commands(template: dict[str, Any]) -> list[str]:
    lines = ["## Commands", ""]
    for command in template.get("commands", []):
        lines += [
            f"### `{command.get('name')}`",
            "",
            f"{command.get('description', '')}",
            "",
            f"- **Fields:** {md_escape(command.get('fields'))}",
            f"- **Valid when:** {md_escape(command.get('valid_conditions'))}",
            f"- **Completes when:** {md_escape(command.get('completion_conditions'))}",
            f"- **Errors:** {md_escape(command.get('error_conditions'))}",
            "",
        ]
    return lines


def render_fsm_processes(template: dict[str, Any]) -> list[str]:
    lines = ["## FSM Processes", ""]
    for fsm in template.get("fsm_processes", []):
        lines += [
            f"### `{fsm.get('name')}`",
            "",
            f"{fsm.get('purpose', '')}",
            "",
            f"- **SimPy process:** {md_escape(fsm.get('simpy_process'))}",
            f"- **States:** {md_escape(fsm.get('states'))}",
            f"- **Interfaces touched:** {md_escape(fsm.get('interfaces_touched'))}",
            f"- **Queues used:** {md_escape(fsm.get('queues_used'))}",
            f"- **Resources used:** {md_escape(fsm.get('resources_used'))}",
            "",
        ]
        transitions = fsm.get("transitions", [])
        if transitions:
            lines += md_table(
                ["From", "To", "Condition", "Actions", "Latency (cycles)"],
                [
                    [
                        txn.get("from"),
                        txn.get("to"),
                        txn.get("condition"),
                        txn.get("actions"),
                        txn.get("latency_cycles"),
                    ]
                    for txn in transitions
                ],
            )
            lines.append("")
    return lines


def render_resources(template: dict[str, Any]) -> list[str]:
    resources = template.get("resources", [])
    if not resources:
        return []
    lines = ["## Resources", ""]
    lines += md_table(
        ["Name", "Type", "Capacity", "Bandwidth", "Latency (cycles)", "Arbitration", "Sharing Scope"],
        [
            [
                res.get("name"),
                res.get("type"),
                res.get("capacity"),
                res.get("bandwidth"),
                res.get("latency_cycles"),
                res.get("arbitration"),
                res.get("sharing_scope"),
            ]
            for res in resources
        ],
    )
    lines.append("")
    return lines


def render_queues(template: dict[str, Any]) -> list[str]:
    queues = template.get("queues", [])
    if not queues:
        return []
    lines = ["## Queues", ""]
    lines += md_table(
        ["Name", "Producer", "Consumer", "Depth", "Blocking", "Ordering"],
        [
            [
                queue.get("name"),
                queue.get("producer"),
                queue.get("consumer"),
                queue.get("depth"),
                queue.get("blocking_behavior"),
                queue.get("ordering"),
            ]
            for queue in queues
        ],
    )
    lines.append("")
    return lines


def render_fsm_relationships(template: dict[str, Any]) -> list[str]:
    relationships = template.get("fsm_relationships", {})
    lines = ["## FSM Relationships", "", f"**FSM count:** {md_escape(relationships.get('fsm_count'))}", ""]
    parallel = relationships.get("parallel_processes", [])
    if parallel:
        lines += ["### Parallel Process Groups", ""]
        for group in parallel:
            lines.append(f"- {md_escape(group)}")
        lines.append("")
    sequential = relationships.get("sequential_paths", [])
    if sequential:
        lines += ["### Sequential Paths", ""]
        for path in sequential:
            lines += [
                f"**{path.get('name')}** — {path.get('description', '')}",
                "",
                f"`{' -> '.join(str(step) for step in path.get('path', []))}`",
                "",
            ]
    gating = relationships.get("gating_relationships", [])
    if gating:
        lines += ["### Gating Relationships", ""]
        lines += md_table(
            ["Source", "Gates", "Stall Reason"],
            [[gate.get("source"), gate.get("gates"), gate.get("stall_reason")] for gate in gating],
        )
        lines.append("")
    return lines


def render_timing_model(template: dict[str, Any]) -> list[str]:
    timing = template.get("timing_model", {})
    lines = [
        "## Timing Model",
        "",
        f"**Clock:** {md_escape(timing.get('clock_mhz'))} MHz "
        f"({md_escape(timing.get('cycle_time_ns'))} ns/cycle)",
        "",
        f"**Assumption:** {md_escape(timing.get('assumption'))}",
        "",
    ]
    delays = timing.get("fsm_process_delays", [])
    if delays:
        lines += ["### Per-FSM Operation Delays", ""]
        rows = []
        for entry in delays:
            for operation in entry.get("operations", []):
                rows.append(
                    [
                        entry.get("fsm"),
                        entry.get("mode"),
                        operation.get("name"),
                        operation.get("cycles"),
                        operation.get("ns"),
                    ]
                )
        lines += md_table(["FSM", "Mode", "Operation", "Cycles", "ns"], rows)
        lines.append("")
    paths = timing.get("end_to_end_paths", [])
    if paths:
        lines += ["### End-To-End Paths", ""]
        lines += md_table(
            ["Path", "Cycles", "ns", "Steps"],
            [[path.get("name"), path.get("cycles"), path.get("ns"), path.get("path")] for path in paths],
        )
        lines.append("")
    return lines


def render_functionality_model(template: dict[str, Any]) -> list[str]:
    functionality = template.get("functionality_model", {})
    lines = [
        "## Functionality Model",
        "",
        f"- **Implemented in SimPy:** {md_escape(functionality.get('implemented_in_simpy'))}",
        f"- **State variables:** {md_escape(functionality.get('state_variables'))}",
        f"- **APIs:** {md_escape(functionality.get('apis'))}",
        f"- **Ignored payload details:** {md_escape(functionality.get('ignored_payload_details'))}",
        "",
        "### Invariants",
        "",
    ]
    lines += bullets(functionality.get("invariants", []))
    lines.append("")
    return lines


def render_performance_model(template: dict[str, Any]) -> list[str]:
    performance = template.get("performance_model", {})
    lines = ["## Performance Model", ""]
    lines += md_table(
        ["Setting", "Value"],
        [[key, value] for key, value in performance.items()],
    )
    lines.append("")
    return lines


def render_test_scenarios(template: dict[str, Any]) -> list[str]:
    lines = ["## Test Scenarios", ""]
    for scenario in template.get("test_scenarios", []):
        lines += [
            f"### `{scenario.get('name')}`",
            "",
            f"{scenario.get('description', '')}",
            "",
            f"- **Input sequence:** {md_escape(scenario.get('input_sequence'))}",
            f"- **Expected functional behavior:** {md_escape(scenario.get('expected_functional_behavior'))}",
            f"- **Expected performance properties:** {md_escape(scenario.get('expected_performance_properties'))}",
            f"- **FSM coverage:** {md_escape(scenario.get('fsm_coverage'))}",
            "",
        ]
    return lines


def render_document(template: dict[str, Any], template_path: Path) -> str:
    sections: list[str] = []
    sections += render_ip_section(template)
    sections += render_subsystem(template)
    sections += render_interfaces(template)
    sections += render_commands(template)
    sections += render_fsm_processes(template)
    sections += render_resources(template)
    sections += render_queues(template)
    sections += render_fsm_relationships(template)
    sections += render_timing_model(template)
    sections += render_functionality_model(template)
    sections += render_performance_model(template)
    sections += render_test_scenarios(template)
    try:
        source = template_path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        source = template_path.as_posix()
    sections += ["---", "", f"*Source template: `{source}`*", ""]
    return "\n".join(sections)


HTML_STYLE = """
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       max-width: 960px; margin: 2rem auto; padding: 0 1.5rem; line-height: 1.55;
       color: #1f2933; background: #ffffff; }
h1 { border-bottom: 2px solid #d0d7de; padding-bottom: .4rem; }
h2 { border-bottom: 1px solid #d0d7de; padding-bottom: .3rem; margin-top: 2.2rem; }
h3 { margin-top: 1.6rem; }
code { background: #f0f2f5; border-radius: 4px; padding: .1rem .35rem;
       font-family: Consolas, "SF Mono", Menlo, monospace; font-size: .9em; }
table { border-collapse: collapse; width: 100%; margin: .8rem 0; font-size: .92em; }
th, td { border: 1px solid #d0d7de; padding: .35rem .6rem; text-align: left; vertical-align: top; }
th { background: #f6f8fa; }
tr:nth-child(even) td { background: #fafbfc; }
blockquote { border-left: 4px solid #d0d7de; margin: 1rem 0; padding: .2rem 1rem; color: #57606a; }
hr { border: none; border-top: 1px solid #d0d7de; margin: 2rem 0; }
@media (prefers-color-scheme: dark) {
  body { color: #d4d8dd; background: #14181c; }
  h1, h2 { border-color: #3a4149; }
  code { background: #22272e; }
  th, td { border-color: #3a4149; }
  th { background: #1c2128; }
  tr:nth-child(even) td { background: #191e23; }
  blockquote { border-color: #3a4149; color: #8b949e; }
  hr { border-top-color: #3a4149; }
}
"""


def inline_html(text: str) -> str:
    escaped = html.escape(text.replace("\\|", "|"))
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return escaped


def table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def markdown_to_html(markdown: str, title: str) -> str:
    """Convert the renderer's own Markdown subset (headings, tables, bullet
    lists, blockquotes, bold, code spans, hr) into a standalone HTML page."""
    body: list[str] = []
    lines = markdown.splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        if not stripped:
            idx += 1
            continue
        heading = re.match(r"^(#{1,4}) (.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            body.append(f"<h{level}>{inline_html(heading.group(2))}</h{level}>")
            idx += 1
        elif stripped == "---":
            body.append("<hr>")
            idx += 1
        elif stripped.startswith("> "):
            body.append(f"<blockquote><p>{inline_html(stripped[2:])}</p></blockquote>")
            idx += 1
        elif stripped.startswith("| "):
            table_lines = []
            while idx < len(lines) and lines[idx].strip().startswith("|"):
                table_lines.append(lines[idx].strip())
                idx += 1
            body.append("<table>")
            body.append(
                "<tr>" + "".join(f"<th>{inline_html(cell)}</th>" for cell in table_cells(table_lines[0])) + "</tr>"
            )
            for row in table_lines[2:]:
                body.append(
                    "<tr>" + "".join(f"<td>{inline_html(cell)}</td>" for cell in table_cells(row)) + "</tr>"
                )
            body.append("</table>")
        elif stripped.startswith("- "):
            body.append("<ul>")
            while idx < len(lines) and lines[idx].strip().startswith("- "):
                body.append(f"<li>{inline_html(lines[idx].strip()[2:])}</li>")
                idx += 1
            body.append("</ul>")
        else:
            body.append(f"<p>{inline_html(stripped)}</p>")
            idx += 1
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{html.escape(title)}</title>\n<style>{HTML_STYLE}</style>\n</head>\n<body>\n"
        + "\n".join(body)
        + "\n</body>\n</html>\n"
    )


def discover_templates() -> list[Path]:
    return sorted(
        path
        for path in TEMPLATES_DIR.glob("*.template.yaml")
        if not path.name.endswith(".template.draft.yaml")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "templates",
        nargs="*",
        type=Path,
        help="Template YAML files to render (default: every reviewed templates/*.template.yaml)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for rendered documents (default: reports/template_docs)",
    )
    parser.add_argument(
        "--format",
        choices=["md", "html", "both"],
        default="md",
        help="Output format: Markdown, standalone HTML, or both (default: md)",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the rendered document instead of writing files (single template, single format only)",
    )
    args = parser.parse_args()

    template_paths = args.templates or discover_templates()
    if not template_paths:
        raise SystemExit("no templates found to render")
    if args.stdout and (len(template_paths) != 1 or args.format == "both"):
        raise SystemExit("--stdout requires exactly one template and a single format")

    for template_path in template_paths:
        if not template_path.exists():
            raise SystemExit(f"template not found: {template_path}")
        template = load_template(template_path)
        markdown = render_document(template, template_path)
        ip_name = template.get("ip", {}).get("name", template_path.stem)
        outputs: list[tuple[str, str]] = []
        if args.format in ("md", "both"):
            outputs.append((".template.md", markdown))
        if args.format in ("html", "both"):
            outputs.append((".template.html", markdown_to_html(markdown, f"{ip_name} — Template Overview")))
        if args.stdout:
            sys.stdout.write(outputs[0][1])
            return 0
        for suffix, content in outputs:
            output_path = args.output_dir / template_path.name.replace(".template.yaml", suffix)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content, encoding="utf-8")
            print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
