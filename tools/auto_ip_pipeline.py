#!/usr/bin/env python3
"""Automated DLD -> template -> model -> tests pipeline runner.

Watches ``dlds/*_dld.md`` (and ``*.docx`` DLDs) for new or modified files and
drives the ``harness/ip_generation_loop.yaml`` stages for each changed IP:

  1. ``.docx`` DLDs are converted to markdown (``dlds/<ip>_dld.md``).
  2. ``dld_to_template.py`` extracts a draft template + gaps report + HTML doc.
  3. If the promoted template is missing or stale, the *template review* agent
     stage runs (resolve ``TODO_REVIEW``, promote) — see "Agent stages" below.
  4. Coverage (``--strict``) and lint gates run on the promoted template.
  5. A scaffold is generated for brand-new IPs (never overwrites an existing
     model) and a prompt pack is always regenerated.
  6. If the model or its tests are missing/failing, the *model implementation*
     agent stage runs.
  7. Unit tests for the IP, then the full ``validate_dld_flow.py`` gate.

Agent stages (``review_template`` and ``agent_implementation`` in the harness)
need an LLM or a human. By default the runner writes a ready-to-send prompt to
``reports/agent_requests/<ip>.<stage>.prompt.md`` and reports the IP as
*awaiting* that stage. Pass ``--agent-cmd`` to run them unattended, e.g.::

    python tools/auto_ip_pipeline.py --agent-cmd "claude -p --permission-mode acceptEdits"

The prompt is piped to the command's stdin. Failed validations re-invoke the
agent with the failure log up to ``loop_policy.max_iterations`` from the
harness config.

Change detection hashes DLD sources into ``reports/.dld_pipeline_state.json``
(gitignored); an IP is only marked processed after its full chain passes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"
DLDS_DIR = REPO_ROOT / "dlds"
TEMPLATES_DIR = REPO_ROOT / "templates"
MODELS_DIR = REPO_ROOT / "src" / "ip_model_automation"
TESTS_DIR = REPO_ROOT / "tests"
REPORTS_DIR = REPO_ROOT / "reports"
STATE_PATH = REPORTS_DIR / ".dld_pipeline_state.json"
AGENT_REQUEST_DIR = REPORTS_DIR / "agent_requests"
HARNESS_PATH = REPO_ROOT / "harness" / "ip_generation_loop.yaml"
SKILL_PATH = REPO_ROOT / "skills" / "ip-model-generation" / "SKILL.md"
AGENT_CONTRACT_PATH = REPO_ROOT / "agents" / "ip_model_generation_agent.md"


def _load_tool(module_name: str):
    path = TOOLS_DIR / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"cannot load tool: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dld_tool = _load_tool("dld_to_template")


# --------------------------------------------------------------------------- #
# docx -> markdown
# --------------------------------------------------------------------------- #

def docx_to_markdown(docx_path: Path) -> str:
    """Convert a DLD authored in Word into the markdown shape the extractor
    expects (headings, bullets, tables). Requires python-docx."""
    try:
        from docx import Document  # type: ignore
        from docx.table import Table  # type: ignore
        from docx.text.paragraph import Paragraph  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "python-docx is required to convert .docx DLDs. Install it or supply the DLD as markdown."
        ) from exc

    document = Document(str(docx_path))
    lines: list[str] = []
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                lines.append("")
                continue
            style = (paragraph.style.name or "").lower()
            if style.startswith("heading"):
                try:
                    level = int(style.rsplit(" ", 1)[-1])
                except ValueError:
                    level = 2
                lines += ["", "#" * max(1, min(level, 6)) + " " + text, ""]
            elif "list" in style:
                lines.append(f"- {text}")
            else:
                lines.append(text)
        elif child.tag.endswith("}tbl"):
            table = Table(child, document)
            rows = [[cell.text.strip().replace("|", "\\|") for cell in row.cells] for row in table.rows]
            if not rows:
                continue
            lines.append("")
            lines.append("| " + " | ".join(rows[0]) + " |")
            lines.append("| " + " | ".join("---" for _ in rows[0]) + " |")
            for row in rows[1:]:
                lines.append("| " + " | ".join(row) + " |")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def ensure_markdown_dld(source: Path) -> Path:
    """Return a markdown DLD path for the given source, converting .docx if needed."""
    if source.suffix.lower() == ".md":
        return source
    if source.suffix.lower() != ".docx":
        raise SystemExit(f"unsupported DLD format: {source} (expected .md or .docx)")
    stem = source.stem if source.stem.endswith("_dld") else f"{source.stem}_dld"
    md_path = DLDS_DIR / f"{stem}.md"
    md_path.write_text(docx_to_markdown(source), encoding="utf-8")
    print(f"  converted {source.name} -> {md_path.relative_to(REPO_ROOT)}")
    return md_path


# --------------------------------------------------------------------------- #
# Change detection
# --------------------------------------------------------------------------- #

def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_state() -> dict[str, str]:
    if STATE_PATH.is_file():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict[str, str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def discover_dld_sources() -> list[Path]:
    return sorted(list(DLDS_DIR.glob("*_dld.md")) + list(DLDS_DIR.glob("*_dld.docx")))


def state_key(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


# --------------------------------------------------------------------------- #
# Stage helpers
# --------------------------------------------------------------------------- #

def run_cmd(args: list[str], label: str) -> tuple[bool, str]:
    result = subprocess.run(
        args, cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    output = (result.stdout or "") + (result.stderr or "")
    status = "OK" if result.returncode == 0 else "FAIL"
    print(f"  {label}: {status}")
    return result.returncode == 0, output


def tool_cmd(tool: str, *args: str) -> list[str]:
    return [sys.executable, str(TOOLS_DIR / f"{tool}.py"), *args]


def template_gates_pass(ip_name: str, dld_md: Path) -> tuple[bool, str]:
    template_path = TEMPLATES_DIR / f"{ip_name}.template.yaml"
    if not template_path.is_file():
        return False, f"promoted template missing: {template_path.name}"
    ok_lint, lint_out = run_cmd(tool_cmd("template_lint", str(template_path)), "lint_template")
    if not ok_lint:
        return False, lint_out
    ok_cov, cov_out = run_cmd(
        tool_cmd("check_template_coverage", str(template_path), str(dld_md), "--strict"),
        "check_dld_coverage",
    )
    if not ok_cov:
        return False, cov_out
    return True, ""


def unit_tests_pass(ip_name: str) -> tuple[bool, str]:
    test_path = TESTS_DIR / f"test_{ip_name}.py"
    if not test_path.is_file():
        return False, f"missing test file: tests/test_{ip_name}.py"
    env_path = str(REPO_ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", "unittest", f"tests.test_{ip_name}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": env_path},
    )
    output = (result.stdout or "") + (result.stderr or "")
    print(f"  unit_tests({ip_name}): {'OK' if result.returncode == 0 else 'FAIL'}")
    return result.returncode == 0, output


# --------------------------------------------------------------------------- #
# Agent stages
# --------------------------------------------------------------------------- #

def review_prompt(ip_name: str, extra_context: str = "") -> str:
    return "\n".join(
        [
            f"Work in the repository at {REPO_ROOT}.",
            f"Follow the skill instructions in {SKILL_PATH.relative_to(REPO_ROOT).as_posix()} (Stage 0)",
            f"and the agent contract in {AGENT_CONTRACT_PATH.relative_to(REPO_ROOT).as_posix()}.",
            "",
            f"Task: complete and promote the draft template for `{ip_name}`.",
            f"1. Read reports/{ip_name}.gaps.md and dlds/{ip_name}_dld.md.",
            f"2. Replace every TODO_REVIEW in templates/{ip_name}.template.draft.yaml using only",
            "   DLD-stated behavior. For DLD open items, choose a conservative default and record",
            "   it in the gaps report — never invent silent behavior.",
            f"3. Run: python tools/template_lint.py templates/{ip_name}.template.draft.yaml",
            f"4. Run: python tools/check_template_coverage.py templates/{ip_name}.template.draft.yaml dlds/{ip_name}_dld.md",
            f"5. When both pass with no TODO_REVIEW left, copy the draft to templates/{ip_name}.template.yaml",
            "   and re-run the coverage check with --strict on the promoted file.",
            extra_context,
        ]
    )


def implementation_prompt(ip_name: str, extra_context: str = "") -> str:
    pack_path = REPO_ROOT / "prompt_packs" / f"{ip_name}.prompt.md"
    pack = pack_path.read_text(encoding="utf-8") if pack_path.is_file() else ""
    return "\n".join(
        [
            f"Work in the repository at {REPO_ROOT}.",
            f"Follow the agent contract in {AGENT_CONTRACT_PATH.relative_to(REPO_ROOT).as_posix()} and the",
            f"skill instructions in {SKILL_PATH.relative_to(REPO_ROOT).as_posix()} (Template -> Model workflow).",
            "",
            f"Task: implement the SimPy model and unit tests for `{ip_name}` from its reviewed template.",
            f"- Model file: src/ip_model_automation/{ip_name}.py (a scaffold may already exist; fill it).",
            f"- Tests: tests/test_{ip_name}.py covering every template test_scenario.",
            "- Validate with: python tools/validate_ip_flow.py",
            "",
            "The full prompt pack follows.",
            "",
            pack,
            extra_context,
        ]
    )


def dispatch_agent(agent_cmd: str | None, ip_name: str, stage: str, prompt: str) -> bool:
    """Run the agent command with the prompt on stdin, or write a request file.

    Returns True if an agent actually ran (so gates should be re-checked)."""
    AGENT_REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    request_path = AGENT_REQUEST_DIR / f"{ip_name}.{stage}.prompt.md"
    request_path.write_text(prompt, encoding="utf-8")
    if not agent_cmd:
        print(f"  {stage}: agent request written -> {request_path.relative_to(REPO_ROOT)}")
        return False
    print(f"  {stage}: running agent: {agent_cmd}")
    result = subprocess.run(
        agent_cmd, cwd=REPO_ROOT, input=prompt, text=True, encoding="utf-8", errors="replace", shell=True
    )
    print(f"  {stage}: agent exited {result.returncode}")
    return True


# --------------------------------------------------------------------------- #
# Harness config
# --------------------------------------------------------------------------- #

def harness_max_iterations(default: int = 3) -> int:
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(HARNESS_PATH.read_text(encoding="utf-8"))
        return int(data.get("loop_policy", {}).get("max_iterations", default))
    except Exception:
        return default


# --------------------------------------------------------------------------- #
# Per-IP pipeline
# --------------------------------------------------------------------------- #

def process_dld(source: Path, agent_cmd: str | None, max_iterations: int) -> str:
    """Run the pipeline for one DLD source. Returns a status string."""
    dld_md = ensure_markdown_dld(source)
    ip_name = dld_tool.ip_name_from_path(dld_md)
    print(f"\n=== {ip_name} ===")

    # Stage: parse_dld (draft template + gaps report + draft HTML doc)
    ok, out = run_cmd(tool_cmd("dld_to_template", str(dld_md)), "parse_dld")
    if not ok:
        print(out)
        return "failed: dld extraction"

    # Stage: review_template (agent) — only if the promoted template is missing/stale
    gates_ok, gate_out = template_gates_pass(ip_name, dld_md)
    if not gates_ok:
        agent_ran = dispatch_agent(
            agent_cmd, ip_name, "review_template", review_prompt(ip_name, f"\nCurrent gate output:\n{gate_out}")
        )
        if not agent_ran:
            return "awaiting template review"
        gates_ok, gate_out = template_gates_pass(ip_name, dld_md)
        if not gates_ok:
            print(gate_out)
            return "failed: template gates after review"

    template_path = TEMPLATES_DIR / f"{ip_name}.template.yaml"

    # Stage: generate_scaffold — new IPs only; never clobber an implemented model
    model_path = MODELS_DIR / f"{ip_name}.py"
    if not model_path.is_file():
        ok, out = run_cmd(
            tool_cmd("generate_model_scaffold", str(template_path), "--output-dir", str(MODELS_DIR)),
            "generate_scaffold",
        )
        if not ok:
            print(out)
            return "failed: scaffold generation"

    # Stage: generate_prompt_pack
    ok, out = run_cmd(
        tool_cmd("generate_prompt_pack", str(template_path), "--output-dir", str(REPO_ROOT / "prompt_packs")),
        "generate_prompt_pack",
    )
    if not ok:
        print(out)
        return "failed: prompt pack generation"

    # Stage: agent_implementation + validation loop
    tests_ok, test_out = unit_tests_pass(ip_name)
    iteration = 0
    while not tests_ok and iteration < max_iterations:
        iteration += 1
        agent_ran = dispatch_agent(
            agent_cmd,
            ip_name,
            "agent_implementation",
            implementation_prompt(ip_name, f"\nIteration {iteration}. Current failure output:\n{test_out[-4000:]}"),
        )
        if not agent_ran:
            return "awaiting model implementation"
        tests_ok, test_out = unit_tests_pass(ip_name)
    if not tests_ok:
        print(test_out[-2000:])
        return f"failed: unit tests after {max_iterations} agent iterations"

    return "complete"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "dlds",
        nargs="*",
        type=Path,
        help="DLD files (.md or .docx) to process (default: every changed dlds/*_dld.*)",
    )
    parser.add_argument("--force", action="store_true", help="Process even if the DLD content is unchanged")
    parser.add_argument(
        "--agent-cmd",
        help='Shell command to run agent stages unattended; the prompt is piped to stdin '
        '(e.g. "claude -p --permission-mode acceptEdits"). Without it, agent prompts are '
        "written to reports/agent_requests/ and the IP is reported as awaiting that stage.",
    )
    parser.add_argument(
        "--skip-final-gate", action="store_true", help="Skip the repo-wide validate_dld_flow gate at the end"
    )
    args = parser.parse_args(argv)

    state = load_state()
    sources = [p.resolve() for p in args.dlds] if args.dlds else discover_dld_sources()
    for src in sources:
        if not src.is_file():
            raise SystemExit(f"DLD not found: {src}")

    changed = [
        src
        for src in sources
        if args.force or args.dlds or state.get(state_key(src)) != sha256(src)
    ]
    if not changed:
        print("no new or modified DLDs; nothing to do")
        return 0

    print(f"processing {len(changed)} DLD(s): {', '.join(p.name for p in changed)}")
    max_iterations = harness_max_iterations()
    statuses: dict[str, str] = {}
    for src in changed:
        status = process_dld(src, args.agent_cmd, max_iterations)
        statuses[src.name] = status
        if status == "complete":
            state[state_key(src)] = sha256(src)
            save_state(state)

    completed = [name for name, status in statuses.items() if status == "complete"]
    if completed and not args.skip_final_gate:
        print("\n=== repo-wide validation gate ===")
        ok, out = run_cmd(tool_cmd("validate_dld_flow"), "validate_dld_flow")
        if not ok:
            print(out[-3000:])
            for name in completed:
                statuses[name] = "complete (repo-wide gate FAILED — see output)"

    print("\n=== summary ===")
    for name, status in statuses.items():
        print(f"  {name}: {status}")
    return 0 if all(s.startswith("complete") or s.startswith("awaiting") for s in statuses.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
