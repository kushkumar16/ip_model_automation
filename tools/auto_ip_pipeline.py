#!/usr/bin/env python3
"""Automated DLD -> template -> model -> tests pipeline runner.

Watches ``dlds/*_dld.md`` (and ``*.docx`` DLDs) for new or modified files and
executes the stage sequence declared in ``harness/ip_generation_loop.yaml``
for each changed IP. The harness YAML is the single source of truth for the
pipeline: to add, remove, or reorder a stage, edit the YAML — not this file.
(``.docx`` DLDs are converted to markdown before the stages run.)

Stage semantics (the schema comment at the top of the harness YAML is the
authoritative reference):

  - ``kind: tool`` stages run their ``command`` from the repo root with
    ``{placeholder}`` substitution ({ip}, {dld}, {template}, {model_dir}, ...).
  - ``kind: agent`` stages (``review_template``, ``agent_implementation``)
    need an LLM or a human. Their ``gates`` — other tool stages — decide the
    outcome: if the gates already pass the agent is skipped; otherwise the
    agent runs and the gates re-run, up to ``max_attempts`` (default
    ``loop_policy.max_iterations``).
  - ``when: model_missing`` stages are skipped once the IP has a model file
    (scaffolds never overwrite an implemented model).
  - ``scope: repo`` stages run once after every changed IP completes.

Agent stages: by default the runner writes a ready-to-send prompt to
``reports/agent_requests/<ip>.<stage>.prompt.md`` and reports the IP as
*awaiting* that stage. To run them unattended, pick an agent profile from the
harness YAML's ``agent_profiles`` (any vendor's coding agent works — see the
requirements listed there), or pass a raw command::

    python tools/auto_ip_pipeline.py --agent codex
    python tools/auto_ip_pipeline.py --agent-cmd "claude -p --permission-mode acceptEdits"

The prompt is piped to the command's stdin; while the gates fail, the agent
is re-invoked with the failure log.

Change detection hashes DLD sources into ``reports/.dld_pipeline_state.json``
(gitignored); an IP is only marked processed after its full chain passes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"
DLDS_DIR = REPO_ROOT / "dlds"
TEMPLATES_DIR = REPO_ROOT / "templates"
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


def run_stage_command(stage: dict, ctx: dict[str, str]) -> tuple[bool, str]:
    """Run one tool stage's harness command with {placeholder} substitution."""
    name = stage["name"]
    try:
        argv = [arg.format_map(ctx) for arg in shlex.split(str(stage["command"]))]
    except KeyError as exc:
        raise SystemExit(f"harness stage '{name}': unknown placeholder {exc} in command")
    if argv and argv[0] == "python":
        argv[0] = sys.executable
    result = subprocess.run(
        argv,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    output = (result.stdout or "") + (result.stderr or "")
    print(f"  {name}: {'OK' if result.returncode == 0 else 'FAIL'}")
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
            f"4. Run: python tools/check_template_coverage.py templates/{ip_name}.template.draft.yaml"
            f" dlds/{ip_name}_dld.md",
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


def resolve_agent_command(agent: str | None, agent_cmd: str | None, harness: dict) -> str | None:
    """Resolve the agent shell command from --agent-cmd (raw, wins) or an
    ``agent_profiles`` entry in the harness YAML. Returns None when neither
    is given (agent stages then write prompt requests instead of running)."""
    if agent_cmd:
        return agent_cmd
    if not agent:
        return None
    profiles = harness.get("agent_profiles") or {}
    command = profiles.get(agent)
    if not command:
        known = ", ".join(sorted(profiles)) or "none defined"
        raise SystemExit(f"unknown agent profile '{agent}' (harness agent_profiles: {known})")
    return str(command)


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

AGENT_STAGE_MARKER = "external_agent_or_manual_edit"

# Prompt builders for agent stages, keyed by harness stage name.
AGENT_PROMPT_BUILDERS = {
    "review_template": review_prompt,
    "agent_implementation": implementation_prompt,
}

# Conditions usable as a stage's `when:` field, keyed by condition name.
STAGE_CONDITIONS = {
    "model_missing": lambda ctx: not Path(ctx["model_file"]).is_file(),
}


def load_harness() -> dict:
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("PyYAML is required to run the pipeline. Install project requirements.") from exc
    data = yaml.safe_load(HARNESS_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("stages"), list):
        raise SystemExit(f"{HARNESS_PATH}: expected a YAML mapping with a 'stages' list")
    for stage in data["stages"]:
        if not isinstance(stage, dict) or "name" not in stage or "command" not in stage:
            raise SystemExit(f"{HARNESS_PATH}: every stage needs 'name' and 'command'")
    return data


def stage_kind(stage: dict) -> str:
    return stage.get("kind", "agent" if stage.get("command") == AGENT_STAGE_MARKER else "tool")


def stage_context(harness: dict, ip_name: str | None = None, dld_md: Path | None = None) -> dict[str, str]:
    """Placeholder values for harness stage commands (repo-level, plus per-IP)."""
    inputs = harness.get("inputs", {})
    ctx = {
        "model_dir": str(REPO_ROOT / inputs.get("model_dir", "src/ip_model_automation")),
        "tests_dir": str(REPO_ROOT / inputs.get("tests_dir", "tests")),
        "prompt_pack_dir": str(REPO_ROOT / inputs.get("prompt_pack_dir", "prompt_packs")),
        "reports_dir": str(REPO_ROOT / inputs.get("reports_dir", "reports")),
    }
    if ip_name is not None and dld_md is not None:
        ctx.update(
            ip=ip_name,
            dld=str(dld_md),
            template=str(TEMPLATES_DIR / f"{ip_name}.template.yaml"),
            draft=str(TEMPLATES_DIR / f"{ip_name}.template.draft.yaml"),
            model_file=str(Path(ctx["model_dir"]) / f"{ip_name}.py"),
        )
    return ctx


# --------------------------------------------------------------------------- #
# Per-IP pipeline (generic engine over the harness stage list)
# --------------------------------------------------------------------------- #


def run_gates(
    gate_names: list[str],
    stages_by_name: dict[str, dict],
    ctx: dict[str, str],
    gate_results: dict[str, bool],
) -> tuple[bool, str]:
    """Run an agent stage's gate stages in order; stop at the first failure."""
    for gate_name in gate_names:
        gate = stages_by_name.get(gate_name)
        if gate is None:
            raise SystemExit(f"harness: agent gate references unknown stage '{gate_name}'")
        ok, out = run_stage_command(gate, ctx)
        gate_results[gate_name] = ok
        if not ok:
            return False, out
    return True, ""


def run_agent_stage(
    stage: dict,
    ctx: dict[str, str],
    agent_cmd: str | None,
    stages_by_name: dict[str, dict],
    gate_results: dict[str, bool],
    default_attempts: int,
) -> tuple[str, str]:
    """Skip the agent if its gates pass; otherwise dispatch it and re-check the
    gates, up to max_attempts. Returns (outcome, output) with outcome in
    {'pass', 'awaiting', 'failed'}."""
    name = stage["name"]
    gate_names = stage.get("gates") or []
    if not gate_names:
        raise SystemExit(f"harness: agent stage '{name}' needs a 'gates' list")
    ok, out = run_gates(gate_names, stages_by_name, ctx, gate_results)
    if ok:
        print(f"  {name}: skipped (gates already pass)")
        return "pass", ""
    build_prompt = AGENT_PROMPT_BUILDERS.get(name)
    if build_prompt is None:
        raise SystemExit(f"harness: no prompt builder registered for agent stage '{name}'")
    max_attempts = int(stage.get("max_attempts", default_attempts))
    for attempt in range(1, max_attempts + 1):
        prompt = build_prompt(ctx["ip"], f"\nAttempt {attempt}. Current gate output:\n{out[-4000:]}")
        if not dispatch_agent(agent_cmd, ctx["ip"], name, prompt):
            return "awaiting", ""
        ok, out = run_gates(gate_names, stages_by_name, ctx, gate_results)
        if ok:
            return "pass", ""
    return "failed", out


def process_dld(source: Path, harness: dict, agent_cmd: str | None) -> str:
    """Run the harness's per-IP stages for one DLD source. Returns a status string."""
    dld_md = ensure_markdown_dld(source)
    ip_name = dld_tool.ip_name_from_path(dld_md)
    print(f"\n=== {ip_name} ===")

    ctx = stage_context(harness, ip_name, dld_md)
    stages_by_name = {s["name"]: s for s in harness["stages"]}
    default_attempts = int(harness.get("loop_policy", {}).get("max_iterations", 3))
    gate_results: dict[str, bool] = {}

    for stage in harness["stages"]:
        if stage.get("scope", "ip") != "ip":
            continue
        name = stage["name"]
        when = stage.get("when")
        if when is not None:
            condition = STAGE_CONDITIONS.get(when)
            if condition is None:
                raise SystemExit(f"harness stage '{name}': unknown 'when' condition '{when}'")
            if not condition(ctx):
                print(f"  {name}: skipped (when: {when} is false)")
                continue
        if stage_kind(stage) == "agent":
            outcome, out = run_agent_stage(stage, ctx, agent_cmd, stages_by_name, gate_results, default_attempts)
            if outcome == "awaiting":
                return f"awaiting {name}"
            if outcome == "failed":
                print(out[-2000:])
                return f"failed: {name} gates still failing after agent attempts"
        else:
            if gate_results.get(name):
                print(f"  {name}: OK (already verified as an agent gate)")
                continue
            ok, out = run_stage_command(stage, ctx)
            gate_results[name] = ok
            if not ok:
                if stage.get("required", True):
                    print(out[-2000:])
                    return f"failed: {name}"
                print(f"  {name}: FAIL (optional stage; continuing)")
    return "complete"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "dlds",
        nargs="*",
        type=Path,
        help="DLD files (.md or .docx) to process (default: every changed dlds/*_dld.*)",
    )
    parser.add_argument("--force", action="store_true", help="Process even if the DLD content is unchanged")
    parser.add_argument(
        "--agent",
        help="Named agent profile from the harness YAML's agent_profiles (e.g. claude, codex, "
        "gemini) to run agent stages unattended. Any vendor's coding agent qualifies if it "
        "meets the requirements listed next to agent_profiles.",
    )
    parser.add_argument(
        "--agent-cmd",
        help="Raw shell command to run agent stages unattended (overrides --agent); the prompt "
        'is piped to stdin (e.g. "claude -p --permission-mode acceptEdits"). Without either '
        "flag, agent prompts are written to reports/agent_requests/ and the IP is reported "
        "as awaiting that stage.",
    )
    parser.add_argument(
        "--skip-final-gate", action="store_true", help="Skip the repo-wide (scope: repo) harness stages at the end"
    )
    args = parser.parse_args(argv)

    harness = load_harness()
    agent_cmd = resolve_agent_command(args.agent, args.agent_cmd, harness)
    state = load_state()
    sources = [p.resolve() for p in args.dlds] if args.dlds else discover_dld_sources()
    for src in sources:
        if not src.is_file():
            raise SystemExit(f"DLD not found: {src}")

    changed = [src for src in sources if args.force or args.dlds or state.get(state_key(src)) != sha256(src)]
    if not changed:
        print("no new or modified DLDs; nothing to do")
        return 0

    print(f"processing {len(changed)} DLD(s): {', '.join(p.name for p in changed)}")
    statuses: dict[str, str] = {}
    for src in changed:
        status = process_dld(src, harness, agent_cmd)
        statuses[src.name] = status
        if status == "complete":
            state[state_key(src)] = sha256(src)
            save_state(state)

    completed = [name for name, status in statuses.items() if status == "complete"]
    repo_stages = [s for s in harness["stages"] if s.get("scope") == "repo"]
    if completed and repo_stages and not args.skip_final_gate:
        print("\n=== repo-wide validation gate ===")
        repo_ctx = stage_context(harness)
        for stage in repo_stages:
            ok, out = run_stage_command(stage, repo_ctx)
            if not ok:
                if not stage.get("required", True):
                    print(f"  {stage['name']}: FAIL (optional stage; continuing)")
                    continue
                print(out[-3000:])
                for name in completed:
                    statuses[name] = f"complete (repo-wide gate FAILED at {stage['name']} — see output)"
                break

    print("\n=== summary ===")
    for name, status in statuses.items():
        print(f"  {name}: {status}")
    return 0 if all(s.startswith("complete") or s.startswith("awaiting") for s in statuses.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
