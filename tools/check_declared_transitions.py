#!/usr/bin/env python3
"""Compare the transitions a model *takes* against the ones its template declares.

Every other check on a model reads it. This one runs it. The template's
``fsm_processes[].transitions`` is a list of ``(from, to)`` pairs, and
``_set_fsm_state`` is the single point every model passes through when it moves
between states — so the two can simply be compared, with the IP's own test suite
as the stimulus.

It reports in both directions, because a contract can be broken either way:

* **undeclared** — the model took a transition the template does not list. The
  model is doing something the contract never described.
* **never taken** — the template lists a transition no test ever drove. Either
  the model cannot take it, or no scenario reaches it; both are worth knowing,
  and the second is a coverage gap rather than a defect.

Neither is automatically a bug. A template can be wrong and the model right —
that is how four of the six transition findings on ``sram_ctrl_ip`` were
resolved. This tool says *these two documents disagree here*; which side to
change is a person's call, as it is for the review stage.

**This is a report, not a gate.** It exits 0 whatever it finds unless ``--strict``
is passed. The point of the first run is to learn the size of the problem across
all the IPs before deciding what a gate should refuse.

Usage::

    python tools/check_declared_transitions.py                 # every IP
    python tools/check_declared_transitions.py sram_ctrl_ip    # one
    python tools/check_declared_transitions.py --strict        # exit 1 on undeclared
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import inspect
import io
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

Transition = tuple[str, str, str]  # (fsm, from_state, to_state)


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


def state_names(fsm: dict[str, Any]) -> set[str]:
    """A state may be a bare name or a mapping carrying a description."""
    return {str(s.get("name")) if isinstance(s, dict) else str(s) for s in fsm.get("states", [])}


def states_without_exit(template: dict[str, Any]) -> list[tuple[str, str]]:
    """States the template lists with no declared way out.

    This needs no model and no tests — it is a property of the contract alone,
    which is what makes it checkable at promotion time, before a model exists to
    deviate from it. A state with no declared exit does not constrain the model
    at all: whatever the model does there is undeclared by construction.
    """
    missing: list[tuple[str, str]] = []
    for fsm in template.get("fsm_processes", []):
        if not isinstance(fsm, dict):
            continue
        name = str(fsm.get("name"))
        sources = {str(t.get("from")) for t in fsm.get("transitions", []) if isinstance(t, dict)}
        missing.extend((name, state) for state in sorted(state_names(fsm) - sources))
    return missing


def declared_transitions(template: dict[str, Any]) -> set[Transition]:
    """Every ``(fsm, from, to)`` the template lists."""
    declared: set[Transition] = set()
    for fsm in template.get("fsm_processes", []):
        if not isinstance(fsm, dict):
            continue
        name = str(fsm.get("name"))
        for transition in fsm.get("transitions", []):
            if isinstance(transition, dict):
                declared.add((name, str(transition.get("from")), str(transition.get("to"))))
    return declared


# --------------------------------------------------------------------------- #
# Instrumentation
# --------------------------------------------------------------------------- #
class RecordingState(dict):
    """A model's ``fsm_state`` dict that reports every state change.

    Instrumenting the *data* rather than a setter is what makes this work across
    the whole repo: two models route their changes through a ``_set_fsm_state``
    helper and ten assign ``self.fsm_state[fsm] = ...`` directly. Both end up
    here.
    """

    def __init__(self, initial: dict, sink: list[Transition]):
        super().__init__(initial)
        self._sink = sink
        self._entered: set[str] = set()

    def __setitem__(self, fsm, state) -> None:
        previous = self.get(fsm)
        # A process whose first act is to announce the state the constructor
        # already set (RESET -> RESET) is agreeing, not transitioning. A later
        # self-entry is real and is kept -- that is the duplicate-idle shape.
        first = fsm not in self._entered
        self._entered.add(fsm)
        if not (first and previous == state):
            self._sink.append((str(fsm), str(previous), str(state)))
        super().__setitem__(fsm, state)


def model_classes(ip: str) -> list[type]:
    """Classes defined in ``ip_model_automation.<ip>`` — filtered later by whether
    an instance actually turns out to carry an ``fsm_state`` mapping."""
    module = importlib.import_module(f"ip_model_automation.{ip}")
    return [obj for _, obj in inspect.getmembers(module, inspect.isclass) if obj.__module__ == module.__name__]


@contextlib.contextmanager
def recording(classes: list[type]):
    """Swap each constructed model's ``fsm_state`` for a recording one."""
    taken: list[Transition] = []
    originals = {cls: cls.__init__ for cls in classes}

    def make(original):
        def wrapper(self, *args, **kwargs):
            original(self, *args, **kwargs)
            state = getattr(self, "fsm_state", None)
            if isinstance(state, dict) and not isinstance(state, RecordingState):
                self.fsm_state = RecordingState(state, taken)

        return wrapper

    for cls, original in originals.items():
        cls.__init__ = make(original)
    try:
        yield taken
    finally:
        for cls, original in originals.items():
            cls.__init__ = original


def run_tests(ip: str) -> tuple[bool, int]:
    """Run this IP's own test module as the stimulus. Returns (passed, count)."""
    suite = unittest.TestLoader().loadTestsFromName(f"tests.test_{ip}")
    stream = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()):
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    return result.wasSuccessful(), result.testsRun


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def check_ip(ip: str, template_path: Path) -> dict[str, Any]:
    template = load_template(template_path)
    declared = declared_transitions(template)
    no_exit = states_without_exit(template)
    classes = model_classes(ip)
    with recording(classes) as taken:
        passed, test_count = run_tests(ip)
    observed = set(taken)
    undeclared = sorted(observed - declared)
    dead_ends = {(fsm, state) for fsm, state in no_exit}
    return {
        "ip": ip,
        "tests_passed": passed,
        "test_count": test_count,
        "undeclared": undeclared,
        # An undeclared transition out of a state with no declared exit is not
        # really the model disagreeing with the contract; the contract said
        # nothing there. Separating the two is what stops a gate blaming the
        # model for a hole in the template.
        "undeclared_from_dead_end": [t for t in undeclared if (t[0], t[1]) in dead_ends],
        "never_taken": sorted(declared - observed),
        "states_without_exit": no_exit,
        "declared_count": len(declared),
        "observed_count": len(observed),
    }


def format_report(results: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for result in results:
        ip = result["ip"]
        matched = result["observed_count"] - len(result["undeclared"])
        lines.append(
            f"{ip}: {matched}/{result['declared_count']} declared transitions taken, "
            f"{len(result['undeclared'])} undeclared "
            f"({len(result['undeclared_from_dead_end'])} from a state with no declared exit), "
            f"{len(result['never_taken'])} never taken, "
            f"{len(result['states_without_exit'])} states with no declared exit "
            f"({result['test_count']} tests)"
        )
        if not result["tests_passed"]:
            lines.append("  WARN tests did not all pass, so this run is not a full sweep of the declared graph")
        for fsm, state in result["states_without_exit"]:
            lines.append(f"  NO DECLARED EXIT {fsm}: {state}")
        for fsm, source, target in result["undeclared"]:
            marker = "*" if (fsm, source) in {(f, s) for f, s in result["states_without_exit"]} else " "
            lines.append(f"  UNDECLARED{marker} {fsm}: {source} -> {target}")
        for fsm, source, target in result["never_taken"]:
            lines.append(f"  NEVER TAKEN {fsm}: {source} -> {target}")
    lines.append("")
    lines.append("* the state it leaves has no declared exit at all, so the template constrained nothing here")
    return lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ips", nargs="*", help="IP names; default is every IP with a template and a test module")
    parser.add_argument("--strict", action="store_true", help="exit 1 if any undeclared transition was taken")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(SRC_DIR))
    sys.path.insert(0, str(REPO_ROOT))
    from ip_model_automation.common import IP_ARTIFACTS  # noqa: E402  (needs sys.path above)

    names = args.ips or sorted(IP_ARTIFACTS)
    results = []
    for ip in names:
        if ip not in IP_ARTIFACTS:
            print(f"unknown IP: {ip}", file=sys.stderr)
            return 2
        template_path = REPO_ROOT / IP_ARTIFACTS[ip].template
        if not template_path.is_file():
            continue
        if not (REPO_ROOT / "tests" / f"test_{ip}.py").is_file():
            continue
        results.append(check_ip(ip, template_path))

    for line in format_report(results):
        print(line)

    undeclared_total = sum(len(result["undeclared"]) for result in results)
    dead_end_total = sum(len(result["undeclared_from_dead_end"]) for result in results)
    never_taken_total = sum(len(result["never_taken"]) for result in results)
    no_exit_total = sum(len(result["states_without_exit"]) for result in results)
    print(
        f"\ntotal: {undeclared_total} undeclared ({dead_end_total} from a state with no declared exit), "
        f"{never_taken_total} declared-but-never-taken, {no_exit_total} states with no declared exit"
    )

    if args.strict and undeclared_total:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
