#!/usr/bin/env python3
"""Dispatch exactly one named agent stage for one IP, on demand.

`tools/auto_ip_pipeline.py` only ever runs an agent stage automatically, as
part of a full per-IP walk through every harness stage in order, and only
when that stage's own gate currently fails. There was no way to say "run
just review_model for watchdog_ip, right now" without either waiting for its
gate to go stale on its own or re-running the entire sequence around it.

This tool is that direct control. It reuses `auto_ip_pipeline.py`'s own
prompt builders and dispatch functions (`dispatch_agent`,
`dispatch_isolated_review`) rather than reimplementing them, so a manually
requested dispatch gets exactly the same isolation, cost tracking, and
committed-files safety checks an automatic one would.

Usage::

    python tools/run_agent.py --list                         # what's available
    python tools/run_agent.py review_model watchdog_ip --agent claude
    python tools/run_agent.py complete_template new_ip --agent-cmd "claude -p ..."

Without --agent/--agent-cmd, the prompt is written to
reports/agent_requests/<ip>.<stage>.prompt.md and nothing is dispatched --
the same graceful degradation `auto_ip_pipeline.py` itself uses.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

try:
    from ._repo_root import find_repo_root
except ImportError:  # not running as part of the `ip_model_automation.tools`
    # package (e.g. `python tools/x.py`, or a test loading this file
    # directly via importlib) -- fall back to a sibling top-level import.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _repo_root import find_repo_root

REPO_ROOT = find_repo_root()

# One human-facing line per dispatchable agent stage, plus the contract that
# governs it. Purely informational for --list; the harness YAML is still the
# only source of truth for what actually runs and which gates decide it.
# tests/test_workflow.py asserts every `kind: agent` stage in the harness has
# an entry here, so this cannot silently go stale as stages are added.
AGENT_CATALOG: dict[str, dict[str, str]] = {
    "normalize_dld": {
        "contract": "agents/dld_normalization_agent.md",
        "does": "Reshapes an author's off-shape DLD into the extractor-readable "
        "structure -- structure only, never content, measurements, or identifiers.",
    },
    "review_normalization": {
        "contract": "agents/normalization_review_agent.md",
        "does": "Adversarial, isolated check that a normalized DLD says exactly what "
        "the author's original said. May file findings; never fixes them.",
    },
    "complete_template": {
        "contract": "agents/ip_model_generation_agent.md",
        "does": "Fills every TODO_REVIEW in a draft template using only DLD-stated behavior.",
    },
    "agent_implementation": {
        "contract": "agents/ip_model_generation_agent.md",
        "does": "Writes a brand-new IP's SimPy model and unit tests from its reviewed template (greenfield only).",
    },
    "amend_implementation": {
        "contract": "agents/ip_model_generation_agent.md",
        "does": "Edits an existing IP's model/tests to match a changed template, from a "
        "structured diff -- never a rewrite from scratch.",
    },
    "review_model": {
        "contract": "agents/model_review_agent.md",
        "does": "Adversarial, isolated check that a model and its tests are faithful to "
        "their template. May file findings; never fixes them.",
    },
}


def _load_pipeline():
    spec = importlib.util.spec_from_file_location("auto_ip_pipeline", REPO_ROOT / "tools" / "auto_ip_pipeline.py")
    pipeline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = pipeline
    spec.loader.exec_module(pipeline)
    return pipeline


def print_catalog(harness: dict) -> None:
    stages_by_name = {s["name"]: s for s in harness["stages"]}
    print("Agent stages dispatchable directly with `python tools/run_agent.py <name> <ip>`:\n")
    for name, info in AGENT_CATALOG.items():
        stage = stages_by_name.get(name, {})
        gates = ", ".join(stage.get("gates") or []) or "(none declared)"
        print(f"{name}")
        print(f"  contract: {info['contract']}")
        print(f"  does:     {info['does']}")
        print(f"  gates:    {gates}")
        print()


def main(argv: list[str]) -> int:
    pipeline = _load_pipeline()
    harness = pipeline.load_harness()

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("agent_name", nargs="?", choices=sorted(AGENT_CATALOG), help="which agent stage to dispatch")
    parser.add_argument("ip", nargs="?", help="the IP to dispatch it for")
    parser.add_argument("--agent", help="named agent profile from the harness YAML's agent_profiles")
    parser.add_argument("--agent-cmd", help="raw shell command; overrides --agent")
    parser.add_argument("--agent-timeout", type=float, default=pipeline.DEFAULT_AGENT_TIMEOUT)
    parser.add_argument("--list", action="store_true", help="print every dispatchable agent stage and exit")
    args = parser.parse_args(argv)

    if args.list or not args.agent_name:
        print_catalog(harness)
        return 0
    if not args.ip:
        parser.error("an IP name is required unless --list is given")

    agent_cmd = pipeline.resolve_agent_command(args.agent, args.agent_cmd, harness)

    stages_by_name = {s["name"]: s for s in harness["stages"]}
    stage = stages_by_name.get(args.agent_name)
    if stage is None or pipeline.stage_kind(stage) != "agent":
        raise SystemExit(f"{args.agent_name!r} is not a `kind: agent` stage in the harness")

    dld_md = REPO_ROOT / "dlds" / f"{args.ip}_dld.md"
    ctx = pipeline.stage_context(harness, args.ip, dld_md)
    options = pipeline.RunOptions(agent_timeout=args.agent_timeout)

    gate_names = stage.get("gates") or []
    if gate_names:
        gates_ok, gate_output = pipeline.run_gates(gate_names, stages_by_name, ctx, {}, options)
    else:
        gates_ok, gate_output = True, ""
    print(
        f"{args.agent_name}/{args.ip}: gates currently {'pass' if gates_ok else 'fail'} "
        f"({', '.join(gate_names) or 'none declared'})"
    )
    if not gates_ok:
        print(gate_output)

    build_prompt = pipeline.AGENT_PROMPT_BUILDERS.get(args.agent_name)
    if build_prompt is None:
        raise SystemExit(f"no prompt builder registered for {args.agent_name!r}")
    extra_context = "" if gates_ok else f"\nCurrent gate output:\n{gate_output[-4000:]}"

    review_kind = pipeline.REVIEW_STAGE_KINDS.get(args.agent_name)
    if review_kind:
        next_id = pipeline.next_finding_id(args.ip, review_kind)
        prompt = build_prompt(args.ip, next_id=next_id, extra_context=extra_context)
        dispatched = pipeline.dispatch_isolated_review(
            agent_cmd, args.ip, args.agent_name, review_kind, prompt, options
        )
    else:
        prompt = build_prompt(args.ip, extra_context)
        dispatched = pipeline.dispatch_agent(agent_cmd, args.ip, args.agent_name, prompt, options)

    pipeline.write_cost_report(args.ip, options)

    if not dispatched:
        if agent_cmd:
            print(f"{args.agent_name}/{args.ip}: dispatch did not run -- see the ABORTED message above")
            return 1
        print(f"{args.agent_name}/{args.ip}: no --agent/--agent-cmd given, prompt request written")
        return 0

    if not gate_names:
        return 0
    gates_ok_after, gate_output_after = pipeline.run_gates(gate_names, stages_by_name, ctx, {}, options)
    print(f"{args.agent_name}/{args.ip}: gates now {'pass' if gates_ok_after else 'fail'}")
    if not gates_ok_after:
        print(gate_output_after)
    return 0 if gates_ok_after else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
