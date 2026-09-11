#!/usr/bin/env python3
"""Check that each model actually implements its template's interface wait models.

The template says *where* an IP waits on each interface:

    wait_model:
      mode: wait_for_ack_before_next_request
      wait_points: [interrupt_notify.WAIT_SW_CLEAR]

``template_lint.py`` proves that wait point names a real FSM state; this tool
closes the loop on the other side and proves the generated model actually enters
it. Without this gate a template can declare that the requester blocks for a
software clear while the model asserts the interrupt and loops on — the numbers
still look plausible, which is exactly what makes the drift expensive to find.

The check is source-level: every model publishes its current state through
``fsm_state``, so a wait point is implemented when the model assigns that state
to that FSM, in either of the two forms used in this repo::

    self.fsm_state["interrupt_notify"] = "WAIT_SW_CLEAR"
    self._set_fsm_state("arbiter_main", "SQ_SCAN")

``TODO_REVIEW`` wait points are skipped: an unpromoted draft is the coverage
check's problem (``check_template_coverage.py --strict``), not this one.

Usage::

    python tools/check_wait_model_coverage.py                 # every promoted template
    python tools/check_wait_model_coverage.py mailbox_ip      # one IP
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "templates"
MODELS_DIR = REPO_ROOT / "src" / "ip_model_automation"
TODO = "TODO_REVIEW"


def require_yaml():
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - environment guard
        raise SystemExit("PyYAML is required. Install project requirements before running this tool.") from exc
    return yaml


def promoted_ips() -> list[str]:
    return sorted(path.name[: -len(".template.yaml")] for path in TEMPLATES_DIR.glob("*.template.yaml"))


def enters_state(source: str, fsm: str, state: str) -> bool:
    """Does the model source assign ``state`` to ``fsm``'s published state?"""
    patterns = (
        rf"""fsm_state\[["']{re.escape(fsm)}["']\]\s*=\s*["']{re.escape(state)}["']""",
        rf"""_set_fsm_state\(\s*["']{re.escape(fsm)}["']\s*,\s*["']{re.escape(state)}["']""",
    )
    return any(re.search(pattern, source) for pattern in patterns)


def check_ip(ip: str) -> list[str]:
    yaml = require_yaml()
    template_path = TEMPLATES_DIR / f"{ip}.template.yaml"
    model_path = MODELS_DIR / f"{ip}.py"
    if not model_path.is_file():
        return [f"{ip}: no model file {model_path.relative_to(REPO_ROOT)}"]

    template: dict[str, Any] = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    source = model_path.read_text(encoding="utf-8")

    errors: list[str] = []
    for interface in template.get("interfaces", []):
        wait_model = interface.get("wait_model") or {}
        for point in wait_model.get("wait_points", []):
            point = str(point)
            if point == TODO or "." not in point:
                continue
            fsm, state = point.split(".", 1)
            if not enters_state(source, fsm, state):
                errors.append(
                    f"{ip}: interface `{interface.get('name')}` declares "
                    f"{wait_model.get('mode')} at `{point}`, but {model_path.name} never enters that state"
                )
    return errors


def check_all(ips: list[str] | None = None) -> list[str]:
    errors: list[str] = []
    for ip in ips or promoted_ips():
        errors.extend(check_ip(ip))
    return errors


def unoccupied_wait_points(ips: list[str]) -> list[str]:
    """Declared wait points that are assigned but never observably occupied.

    ``enters_state`` above is a regex over the model source, so it answers "does
    this assignment appear in the file?" and not "does the process ever park
    there?". Those come apart exactly where it matters: a state assigned and
    overwritten with no ``yield`` between exists at no simulated time, so an
    interface declared to block there does not observably block anywhere, and
    this gate passed anyway.

    Answering it properly needs the model run, which ``check_declared_transitions``
    already does, so its occupancy record is reused rather than rebuilt. These are
    reported and do **not** fail the gate: the declared wait points that fall in
    here are a standing question about the models, not a regression introduced by
    whoever runs this next, and turning them into a hard failure is a decision for
    a person.
    """
    spec = importlib.util.spec_from_file_location("_cdt", REPO_ROOT / "tools" / "check_declared_transitions.py")
    if spec is None or spec.loader is None:  # pragma: no cover
        return []
    cdt = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = cdt
    spec.loader.exec_module(cdt)

    # check_declared_transitions imports the models and runs their tests, both of
    # which need src/ and the repo root importable. It does this in its own main();
    # reusing it as a library means doing it here.
    for path in (REPO_ROOT / "src", REPO_ROOT):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    yaml = require_yaml()
    notes: list[str] = []
    for ip in ips:
        template_path = TEMPLATES_DIR / f"{ip}.template.yaml"
        if not template_path.is_file():
            continue
        try:
            result = cdt.check_ip(ip, template_path)
        except Exception as exc:  # pragma: no cover - never let a note break the gate
            notes.append(f"{ip}: could not measure state occupancy ({exc})")
            continue
        unobservable = {(f, st) for f, st in result.get("unobservable_states", [])}
        if not unobservable:
            continue
        template: dict[str, Any] = yaml.safe_load(template_path.read_text(encoding="utf-8"))
        for interface in template.get("interfaces", []):
            wait_model = interface.get("wait_model") or {}
            for point in wait_model.get("wait_points", []):
                point = str(point)
                if point == TODO or "." not in point:
                    continue
                fsm, state = point.split(".", 1)
                if (fsm, state) in unobservable:
                    notes.append(
                        f"{ip}: interface `{interface.get('name')}` declares "
                        f"{wait_model.get('mode')} at `{point}`, and under this IP's own tests the model "
                        f"assigns that state without ever holding it - no current test observes it parked "
                        f"there. Check the model's default latencies before reading this as a model defect"
                    )
    return notes


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ips", nargs="*", help="IP names to check (default: every promoted template)")
    args = parser.parse_args(argv)

    ips = args.ips or promoted_ips()
    errors = check_all(ips)
    if errors:
        print("wait model coverage: FAIL", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    notes = unoccupied_wait_points(ips)
    print(f"wait model coverage: OK ({len(ips)} models assign every declared wait point)")
    if notes:
        print(
            f"  NOTE {len(notes)} declared wait point(s) are assigned but never observably occupied. "
            "This gate checks the assignment, not the occupancy."
        )
        for note in notes:
            print(f"  - {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
