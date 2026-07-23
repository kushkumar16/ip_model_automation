#!/usr/bin/env python3
"""Structured diff between two IP model templates, for incremental model amends.

When a DLD is edited you do not want to regenerate the model and tests from
scratch. Because the reviewed *template* is the normalized source of truth,
diffing two template revisions yields a clean, typed change list — "FSM gained a
state", "queue depth 8 -> 16", "operation enqueue 2 -> 3 cycles", "scenario X
added" — that maps almost one-to-one to model and test edit sites. Prose DLD
diffs are noisy; template diffs are not.

Each change is tagged with a blast radius:

  - SURGICAL   — a localized value/addition (queue depth, timing number, a new
                 scenario, a description tweak): edit in place.
  - STRUCTURAL — topology change (FSM added/removed, a state added/removed, an
                 interface or command added/removed): may cascade; review before
                 a surgical edit.

The "old" side is normally the previous committed template (git is the history
store — ``git show HEAD:templates/<ip>.template.yaml``); the "new" side is the
working-tree template after re-extraction. ``--amend-prompt`` wraps the delta
into a ready-to-send instruction for an agent to amend the existing model and
tests instead of rewriting them.

Usage::

    python tools/diff_template.py OLD.yaml NEW.yaml
    python tools/diff_template.py OLD.yaml NEW.yaml --json
    python tools/diff_template.py OLD.yaml NEW.yaml --amend-prompt --ip mailbox_ip
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SURGICAL = "SURGICAL"
STRUCTURAL = "STRUCTURAL"


@dataclass
class Change:
    category: str
    target: str
    kind: str  # added | removed | changed
    detail: str
    radius: str


def require_yaml():
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("PyYAML is required. Install project requirements before running this tool.") from exc
    return yaml


def load(path: Path) -> dict[str, Any]:
    yaml = require_yaml()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: template did not parse to a mapping")
    return data


def _by_name(items: list[Any], key: str = "name") -> dict[str, dict]:
    return {str(item[key]): item for item in items or [] if isinstance(item, dict) and key in item}


def _set(values: Any) -> set[str]:
    return {str(v) for v in (values or [])}


def _transitions(fsm: dict) -> set[tuple[str, str, str]]:
    out = set()
    for t in fsm.get("transitions", []) or []:
        if isinstance(t, dict):
            out.add((str(t.get("from")), str(t.get("to")), str(t.get("condition"))))
    return out


def diff_named_section(
    old: dict, new: dict, category: str, key: str, add_remove_radius: str
) -> tuple[list[Change], dict[str, tuple[dict, dict]]]:
    """Report added/removed named items; return the matched pairs for deeper diffing."""
    changes: list[Change] = []
    old_items = _by_name(old.get(category, []), key)
    new_items = _by_name(new.get(category, []), key)
    for name in sorted(new_items.keys() - old_items.keys()):
        changes.append(Change(category, name, "added", f"{category[:-1]} `{name}` added", add_remove_radius))
    for name in sorted(old_items.keys() - new_items.keys()):
        changes.append(Change(category, name, "removed", f"{category[:-1]} `{name}` removed", add_remove_radius))
    matched = {name: (old_items[name], new_items[name]) for name in old_items.keys() & new_items.keys()}
    return changes, matched


def diff_templates(old: dict, new: dict) -> list[Change]:
    changes: list[Change] = []

    # ip meta
    for field in ("name", "domain", "description"):
        o, n = old.get("ip", {}).get(field), new.get("ip", {}).get(field)
        if o != n:
            changes.append(Change("ip", field, "changed", f"ip.{field}: {o!r} -> {n!r}", SURGICAL))

    # interfaces / commands: add/remove is structural; a field change inside is surgical
    iface_changes, iface_pairs = diff_named_section(old, new, "interfaces", "name", STRUCTURAL)
    changes += iface_changes
    changes += _diff_wait_models(iface_pairs)
    cmd_changes, cmd_pairs = diff_named_section(old, new, "commands", "name", STRUCTURAL)
    changes += cmd_changes
    for name, (o, n) in cmd_pairs.items():
        for field in ("fields", "valid_conditions", "completion_conditions", "error_conditions"):
            added, removed = _set(n.get(field)) - _set(o.get(field)), _set(o.get(field)) - _set(n.get(field))
            if added or removed:
                changes.append(
                    Change(
                        "commands",
                        name,
                        "changed",
                        f"command `{name}`.{field}: +{sorted(added)} -{sorted(removed)}",
                        SURGICAL,
                    )
                )

    # fsm_processes: an added/removed FSM or state is structural; touched wiring is surgical
    fsm_changes, fsm_pairs = diff_named_section(old, new, "fsm_processes", "name", STRUCTURAL)
    changes += fsm_changes
    for name, (o, n) in fsm_pairs.items():
        added, removed = _set(n.get("states")) - _set(o.get("states")), _set(o.get("states")) - _set(n.get("states"))
        if added or removed:
            changes.append(
                Change(
                    "fsm_processes",
                    name,
                    "changed",
                    f"FSM `{name}` states: +{sorted(added)} -{sorted(removed)}",
                    STRUCTURAL,
                )
            )
        t_added, t_removed = _transitions(n) - _transitions(o), _transitions(o) - _transitions(n)
        for t in sorted(t_added):
            changes.append(Change("fsm_processes", name, "changed", f"FSM `{name}` transition added {t}", SURGICAL))
        for t in sorted(t_removed):
            changes.append(Change("fsm_processes", name, "changed", f"FSM `{name}` transition removed {t}", SURGICAL))
        for field in ("queues_used", "resources_used", "interfaces_touched"):
            added, removed = _set(n.get(field)) - _set(o.get(field)), _set(o.get(field)) - _set(n.get(field))
            if added or removed:
                changes.append(
                    Change(
                        "fsm_processes",
                        name,
                        "changed",
                        f"FSM `{name}`.{field}: +{sorted(added)} -{sorted(removed)}",
                        SURGICAL,
                    )
                )

    # queues / resources
    q_changes, q_pairs = diff_named_section(old, new, "queues", "name", STRUCTURAL)
    changes += q_changes
    for name, (o, n) in q_pairs.items():
        for field in ("depth", "blocking_behavior", "ordering"):
            if o.get(field) != n.get(field):
                changes.append(
                    Change(
                        "queues",
                        name,
                        "changed",
                        f"queue `{name}`.{field}: {o.get(field)!r} -> {n.get(field)!r}",
                        SURGICAL,
                    )
                )
    r_changes, _ = diff_named_section(old, new, "resources", "name", STRUCTURAL)
    changes += r_changes

    # timing_model
    changes += _diff_timing(old.get("timing_model", {}), new.get("timing_model", {}))

    # test_scenarios: adding a scenario is surgical (new test); removing warrants review
    s_changes, s_pairs = diff_named_section(old, new, "test_scenarios", "name", SURGICAL)
    for change in s_changes:
        if change.kind == "removed":
            change.radius = STRUCTURAL
    changes += s_changes
    for name, (o, n) in s_pairs.items():
        for field in (
            "fsm_coverage",
            "expected_functional_behavior",
            "expected_performance_properties",
            "input_sequence",
        ):
            added, removed = _set(n.get(field)) - _set(o.get(field)), _set(o.get(field)) - _set(n.get(field))
            if added or removed:
                changes.append(
                    Change(
                        "test_scenarios",
                        name,
                        "changed",
                        f"scenario `{name}`.{field}: +{sorted(added)} -{sorted(removed)}",
                        SURGICAL,
                    )
                )

    return changes


def _diff_wait_models(pairs: dict[str, tuple[dict, dict]]) -> list[Change]:
    """Wait-model deltas per interface.

    A changed `mode` moves where the requester blocks — the model's stall sites
    move with it — so it is STRUCTURAL. The supporting fields (where it blocks,
    what releases it, how many requests may be in flight) are localized edits.
    """
    changes: list[Change] = []
    for name in sorted(pairs):
        o, n = (pairs[name][0].get("wait_model") or {}), (pairs[name][1].get("wait_model") or {})
        if o.get("mode") != n.get("mode"):
            changes.append(
                Change(
                    "interfaces",
                    name,
                    "changed",
                    f"interface `{name}` wait_model.mode: {o.get('mode')!r} -> {n.get('mode')!r}",
                    STRUCTURAL,
                )
            )
        for field in ("requester", "wait_points", "resumes_on", "outstanding_limit", "timeout"):
            if o.get(field) != n.get(field):
                changes.append(
                    Change(
                        "interfaces",
                        name,
                        "changed",
                        f"interface `{name}` wait_model.{field}: {o.get(field)!r} -> {n.get(field)!r}",
                        SURGICAL,
                    )
                )
    return changes


def _diff_timing(old: dict, new: dict) -> list[Change]:
    changes: list[Change] = []
    for field in ("clock_mhz", "cycle_time_ns"):
        if old.get(field) != new.get(field):
            changes.append(
                Change(
                    "timing_model",
                    field,
                    "changed",
                    f"timing_model.{field}: {old.get(field)} -> {new.get(field)}",
                    SURGICAL,
                )
            )
    old_ops = {
        (d.get("fsm"), o.get("name")): o for d in old.get("fsm_process_delays", []) for o in d.get("operations", [])
    }
    new_ops = {
        (d.get("fsm"), o.get("name")): o for d in new.get("fsm_process_delays", []) for o in d.get("operations", [])
    }
    for key in sorted(new_ops.keys() - old_ops.keys(), key=str):
        changes.append(
            Change("timing_model", f"{key[0]}.{key[1]}", "added", f"timing op `{key[0]}.{key[1]}` added", SURGICAL)
        )
    for key in sorted(old_ops.keys() - new_ops.keys(), key=str):
        changes.append(
            Change("timing_model", f"{key[0]}.{key[1]}", "removed", f"timing op `{key[0]}.{key[1]}` removed", SURGICAL)
        )
    for key in sorted(old_ops.keys() & new_ops.keys(), key=str):
        o, n = old_ops[key], new_ops[key]
        if (o.get("cycles"), o.get("ns")) != (n.get("cycles"), n.get("ns")):
            changes.append(
                Change(
                    "timing_model",
                    f"{key[0]}.{key[1]}",
                    "changed",
                    f"timing op `{key[0]}.{key[1]}`: "
                    f"{o.get('cycles')}cyc/{o.get('ns')}ns -> {n.get('cycles')}cyc/{n.get('ns')}ns",
                    SURGICAL,
                )
            )
    return changes


def overall_radius(changes: list[Change]) -> str:
    return STRUCTURAL if any(c.radius == STRUCTURAL for c in changes) else SURGICAL


def render_text(changes: list[Change]) -> str:
    if not changes:
        return "no template changes"
    lines = [f"{len(changes)} change(s), overall blast radius: {overall_radius(changes)}", ""]
    for c in changes:
        lines.append(f"  [{c.radius:10s}] {c.detail}")
    return "\n".join(lines)


def render_amend_prompt(changes: list[Change], ip: str) -> str:
    radius = overall_radius(changes)
    lines = [
        f"# Amend request: {ip}",
        "",
        f"The reviewed template `templates/{ip}.template.yaml` changed. Amend the "
        "EXISTING model and tests to match — do not rewrite them from scratch.",
        "",
        f"- Model: `src/ip_model_automation/{ip}.py`",
        f"- Tests: `tests/test_{ip}.py`",
        f"- Overall blast radius: **{radius}**"
        + (
            " — localized edits should suffice."
            if radius == SURGICAL
            else " — a topology change; review whether the edits cascade before proceeding."
        ),
        "",
        "## Exactly what changed (make the minimal corresponding edits)",
        "",
    ]
    for c in changes:
        lines.append(f"- [{c.radius}] {c.detail}")
    lines += [
        "",
        "## Rules",
        "- Change only what the delta requires; leave unrelated model/test code untouched.",
        "- Keep timing values, queue depths, and FSM structure consistent with the new template.",
        "- Update or add unit tests for changed/added scenarios; keep every FSM covered.",
        "- Then run: `python tools/template_lint.py templates/" + ip + ".template.yaml` and "
        "`python tools/validate_dld_flow.py`.",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Structured diff between two IP model templates.")
    parser.add_argument(
        "old", type=Path, help="previous template (e.g. `git show HEAD:templates/<ip>.template.yaml` saved to a file)"
    )
    parser.add_argument("new", type=Path, help="new template")
    parser.add_argument("--json", action="store_true", help="emit the change list as JSON")
    parser.add_argument(
        "--amend-prompt", action="store_true", help="emit an agent amend prompt instead of a plain diff"
    )
    parser.add_argument("--ip", help="IP name (for the amend prompt); defaults to the new template's ip.name")
    args = parser.parse_args(argv)

    changes = diff_templates(load(args.old), load(args.new))

    if args.amend_prompt:
        ip = args.ip or load(args.new).get("ip", {}).get("name", "unknown_ip")
        print(render_amend_prompt(changes, ip))
    elif args.json:
        print(
            json.dumps(
                {
                    "overall_radius": overall_radius(changes) if changes else "NONE",
                    "changes": [asdict(c) for c in changes],
                },
                indent=2,
            )
        )
    else:
        print(render_text(changes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
