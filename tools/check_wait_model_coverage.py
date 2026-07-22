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
    print(f"wait model coverage: OK ({len(ips)} models implement every declared wait point)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
