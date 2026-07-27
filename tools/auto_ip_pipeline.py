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
  - ``kind: agent`` stages (``normalize_dld``, ``review_template``,
    ``agent_implementation``) need an LLM or a human. Their ``gates`` — other
    tool stages — decide the outcome: if the gates already pass the agent is
    skipped; otherwise the agent runs and the gates re-run, up to
    ``max_attempts`` (default ``loop_policy.max_iterations``).
  - ``when: model_missing`` stages are skipped once the IP has a model file
    (scaffolds never overwrite an implemented model).
  - ``when: src_dld_exists`` stages run only for an IP that has an
    unnormalized ``dlds/<ip>_dld.src.md``; an in-shape DLD skips them. A
    ``.docx`` DLD always has one — its conversion output *is* the author
    source.
  - ``awaiting_human: true`` marks a required stage whose failure means a human
    step is pending (the normalization review stamp), so the IP is reported as
    *awaiting* rather than failed.
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
(gitignored); an IP is only marked processed after its full chain passes. The
hash covers the IP's ``.src.md`` too, where one exists, so editing the author's
document re-triggers normalization.
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
NORMALIZATION_CONTRACT_PATH = REPO_ROOT / "agents" / "dld_normalization_agent.md"


def _load_tool(module_name: str):
    path = TOOLS_DIR / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"cannot load tool: {path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so tools that use @dataclass can resolve annotations.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dld_tool = _load_tool("dld_to_template")
diff_tool = _load_tool("diff_template")


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


SRC_SUFFIX = "_dld.src.md"


def ip_stem(source: Path) -> str:
    """The ``<ip>_dld`` stem shared by an IP's .md, .src.md and .docx files.

    ``Path.stem`` is wrong for an author source: ``mailbox_ip_dld.src.md`` stems
    to ``mailbox_ip_dld.src``, which then grows a second suffix everywhere it is
    used. One function so the three file kinds cannot disagree about which IP
    they belong to.
    """
    name = source.name
    if name.endswith(SRC_SUFFIX):
        return name[: -len(".src.md")]
    stem = source.stem
    return stem if stem.endswith("_dld") else f"{stem}_dld"


def ensure_markdown_dld(source: Path) -> Path:
    """Return the normalized markdown DLD path for a source, converting .docx first.

    A ``.docx`` DLD is by definition written in whatever shape its author chose —
    Word offers no way to write the extractor's conventions and no reason to
    guess them — so its conversion lands in ``<ip>_dld.src.md``, the author
    source, and ``normalize_dld`` is what produces ``<ip>_dld.md`` from it. The
    returned path is the normalized one either way, so every downstream stage
    reads the same file it always did, whether or not it exists yet: when it does
    not, the normalization gate is the stage that says so.

    The same holds for a ``.src.md`` handed in directly: it is an *input* to
    normalization, never the pipeline's DLD. Returning it here would point every
    downstream stage at the author's original — the one file the contract says
    must not be written.
    """
    if source.name.endswith(SRC_SUFFIX):
        return DLDS_DIR / f"{ip_stem(source)}.md"
    if source.suffix.lower() == ".md":
        return source
    if source.suffix.lower() != ".docx":
        raise SystemExit(f"unsupported DLD format: {source} (expected .md or .docx)")
    stem = ip_stem(source)
    src_path = DLDS_DIR / f"{stem}.src.md"
    converted = docx_to_markdown(source)
    # Conversion is deterministic, so an unchanged document must not rewrite the
    # file — that would break the normalization stamp for no reason.
    if not src_path.is_file() or src_path.read_text(encoding="utf-8") != converted:
        src_path.write_text(converted, encoding="utf-8")
        print(f"  converted {source.name} -> {src_path.relative_to(REPO_ROOT)} (author source)")
    return DLDS_DIR / f"{stem}.md"


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
    """One source file per IP, in the order a run should read them.

    An IP can be present as up to three files — the author's ``.docx``, the
    author's ``.src.md``, and the normalized ``.md`` — and they are one IP, not
    three. Returning each file separately would process the IP once per file: the
    normal state of a normalized IP is *two* files, so that is not a corner case.

    Preference order is most-authoritative-first: the ``.docx`` an author edits
    beats the ``.src.md`` derived from it, which beats the ``.md`` derived from
    that. A lone ``.src.md`` must be returned too, or a brand-new IP that arrives
    in some other shape is invisible to the pipeline that exists to reshape it.
    """
    by_ip: dict[str, Path] = {}
    for pattern in ("*_dld.md", f"*{SRC_SUFFIX}", "*_dld.docx"):
        for path in DLDS_DIR.glob(pattern):
            if path.name.endswith(SRC_SUFFIX) and pattern == "*_dld.md":
                continue  # `*_dld.md` must not swallow the author source
            current = by_ip.get(ip_stem(path))
            if current is None or _source_rank(path) < _source_rank(current):
                by_ip[ip_stem(path)] = path
    return [by_ip[key] for key in sorted(by_ip)]


def resolve_requested_dld(path: Path) -> Path:
    """Resolve an explicitly requested DLD to a file that exists.

    Naming the normalized DLD is the natural thing to type, and for an IP that
    has not been normalized yet that file does not exist. Fall back to its author
    source rather than refusing: the pipeline's answer for that IP is "normalize
    it first", which it can only say if it accepts the argument.
    """
    resolved = path.resolve()
    if resolved.is_file():
        return resolved
    fallback = DLDS_DIR / f"{ip_stem(resolved)}.src.md"
    return fallback if fallback.is_file() else resolved


def _source_rank(path: Path) -> int:
    if path.suffix.lower() == ".docx":
        return 0
    return 1 if path.name.endswith(SRC_SUFFIX) else 2


def src_dld_path(source: Path) -> Path:
    """The author's unnormalized DLD for this source, whether or not it exists."""
    return DLDS_DIR / f"{ip_stem(source)}.src.md"


def source_fingerprint(source: Path) -> str:
    """Change-detection hash covering every file that feeds one IP's run.

    An IP can have up to three: the author's ``.docx``, the ``.src.md``, and the
    normalized ``.md``. A run must be re-triggered by an edit to any of them —
    hashing only one leaves a corrected source document silently unprocessed,
    which is the failure most likely to go unnoticed, since the source is the
    file the engineer actually edits. Files that do not exist contribute nothing,
    so an IP with no author source hashes exactly as it did before the
    normalization stage existed.
    """
    stem = ip_stem(source)
    candidates = [source, DLDS_DIR / f"{stem}.src.md", DLDS_DIR / f"{stem}.md"]
    seen: list[Path] = []
    for path in candidates:
        if path.is_file() and path not in seen:
            seen.append(path)
    return "+".join(sha256(path) for path in seen)


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


def normalize_prompt(ip_name: str, extra_context: str = "") -> str:
    """Prompt for the normalize_dld stage: reshape the author's DLD, change nothing.

    The whole task description is the contract plus the extraction rules, so the
    prompt stays short on purpose — restating the permitted and forbidden edits
    here would create a second place for them to drift.
    """
    return "\n".join(
        [
            f"Work in the repository at {REPO_ROOT}.",
            f"Follow the agent contract in {NORMALIZATION_CONTRACT_PATH.relative_to(REPO_ROOT).as_posix()}",
            "and the target shape defined in",
            "skills/ip-model-generation/references/dld_extraction_rules.md.",
            "",
            f"Task: rewrite dlds/{ip_name}_dld.src.md into dlds/{ip_name}_dld.md in the shape the",
            "extractor reads, changing structure only — never adding, removing, or altering an",
            "engineering claim, and never rewording one.",
            "",
            f"1. Read dlds/{ip_name}_dld.src.md. Do not edit it; it is the author's original.",
            f"2. Write dlds/{ip_name}_dld.md. Move statements to the sections the extractor looks in;",
            "   preserve their wording. Anything mapping to no convention goes verbatim under",
            '   "## Unplaced Source Content" — never dropped.',
            "3. Leave out any wait model the source does not state, and record ambiguities as Open",
            "   Items rather than resolving them.",
            f"4. Run: python tools/check_dld_normalization.py {ip_name} --no-stamp-check",
            f"5. Run: python tools/dld_to_template.py dlds/{ip_name}_dld.md",
            "",
            "Do not run --stamp. The stamp is a human confirming the meaning survived; stamping",
            "your own normalization would void the only judgment gate this stage has.",
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


def _previous_template(ip_name: str) -> str | None:
    """The last committed template for this IP (git is the history store), or
    None if it is not committed yet."""
    result = subprocess.run(
        ["git", "show", f"HEAD:templates/{ip_name}.template.yaml"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout if result.returncode == 0 else None


def amend_prompt(ip_name: str, extra_context: str = "") -> str:
    """Amend prompt for an IP whose model already exists: diff the previous
    (committed) template against the current one and instruct the agent to make
    only the corresponding edits. Falls back to a full implementation prompt if
    there is no committed baseline to diff against."""
    import yaml  # type: ignore

    old_text = _previous_template(ip_name)
    current = TEMPLATES_DIR / f"{ip_name}.template.yaml"
    if old_text is None or not current.is_file():
        return implementation_prompt(ip_name, extra_context)
    old = yaml.safe_load(old_text)
    new = yaml.safe_load(current.read_text(encoding="utf-8"))
    if not isinstance(old, dict) or not isinstance(new, dict):
        return implementation_prompt(ip_name, extra_context)
    changes = diff_tool.diff_templates(old, new)
    if not changes:
        # Template unchanged vs HEAD; nothing to amend beyond what the gates check.
        return implementation_prompt(ip_name, extra_context)
    return "\n".join(
        [
            f"Work in the repository at {REPO_ROOT}.",
            f"Follow the agent contract in {AGENT_CONTRACT_PATH.relative_to(REPO_ROOT).as_posix()}.",
            "",
            diff_tool.render_amend_prompt(changes, ip_name),
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
    "normalize_dld": normalize_prompt,
    "review_template": review_prompt,
    "agent_implementation": implementation_prompt,
    "amend_implementation": amend_prompt,
}

# Conditions usable as a stage's `when:` field, keyed by condition name.
# `model_new`/`model_exists` read a snapshot (`model_preexisted`) taken when the
# per-IP context is built, BEFORE any stage runs — so `generate_scaffold`
# creating the model file does not flip `model_exists` mid-run. This is what
# separates the greenfield (generate) path from the brownfield (amend) path, and
# those two are mutually exclusive.
#
# `src_dld_exists` is independent of both: it asks whether this IP has an
# unnormalized source document at all. An in-shape DLD has no `.src.md`, so the
# normalization stage and its gate skip entirely and that IP's pipeline is
# exactly what it was before the stage existed.
STAGE_CONDITIONS = {
    "model_new": lambda ctx: not ctx.get("model_preexisted"),
    "model_exists": lambda ctx: bool(ctx.get("model_preexisted")),
    "src_dld_exists": lambda ctx: bool(ctx.get("src_dld_exists")),
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
        model_file = Path(ctx["model_dir"]) / f"{ip_name}.py"
        src_dld = src_dld_path(dld_md)
        ctx.update(
            ip=ip_name,
            dld=str(dld_md),
            src_dld=str(src_dld),
            src_dld_exists="1" if src_dld.is_file() else "",
            template=str(TEMPLATES_DIR / f"{ip_name}.template.yaml"),
            draft=str(TEMPLATES_DIR / f"{ip_name}.template.draft.yaml"),
            model_file=str(model_file),
            # Snapshot at run start: did the model exist before any stage ran?
            model_preexisted="1" if model_file.is_file() else "",
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
                if stage.get("awaiting_human"):
                    # Not a defect — a human step the pipeline cannot do for itself.
                    print(out[-1000:])
                    return f"awaiting {name}"
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
    sources = [resolve_requested_dld(p) for p in args.dlds] if args.dlds else discover_dld_sources()
    for src in sources:
        if not src.is_file():
            raise SystemExit(f"DLD not found: {src}")

    changed = [
        src for src in sources if args.force or args.dlds or state.get(state_key(src)) != source_fingerprint(src)
    ]
    if not changed:
        print("no new or modified DLDs; nothing to do")
        return 0

    print(f"processing {len(changed)} DLD(s): {', '.join(p.name for p in changed)}")
    statuses: dict[str, str] = {}
    for src in changed:
        status = process_dld(src, harness, agent_cmd)
        statuses[src.name] = status
        if status == "complete":
            state[state_key(src)] = source_fingerprint(src)
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
