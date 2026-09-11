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

A note on what "taken" means here. This tool used to count any assignment to
``fsm_state`` as a transition taken, which made it blind to the defect it was
closest to catching: a state assigned and then overwritten with no ``yield``
between exists at no simulated time, so no test, log or trace can observe it, and
a scenario naming it in ``fsm_coverage`` claims coverage no assertion could have.
The transition out of such a state was reported as taken and the state as
reached. It now records the clock alongside each change and reports those states
as ``NEVER OCCUPIED``. A state the constructor sets is excluded: it is readable
before ``env.run()`` and so genuinely assertable.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
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


def declared_states(template: dict[str, Any]) -> set[tuple[str, str]]:
    """Every ``(fsm, state)`` the template lists, so an unobservable state the
    template never declared is not reported against the contract."""
    out: set[tuple[str, str]] = set()
    for fsm in template.get("fsm_processes", []):
        if isinstance(fsm, dict):
            name = str(fsm.get("name"))
            out |= {(name, state) for state in state_names(fsm)}
    return out


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

    def __init__(self, initial: dict, sink: list[Transition], occupancy: "Occupancy", now=None):
        super().__init__(initial)
        self._sink = sink
        self._entered: set[str] = set()
        # Recording the transition alone cannot answer whether the state it left
        # was ever *occupied*. A state assigned and overwritten with no yield in
        # between exists at no simulated time, so no test, log or trace can ever
        # see it -- and this tool used to count the resulting transition as taken,
        # which made the defect invisible to the one check closest to catching it.
        # The clock is what distinguishes the two, so it is recorded alongside.
        self._occupancy = occupancy
        self._now = now or (lambda: None)
        self._since: dict[str, Any] = {}
        for fsm, state in initial.items():
            occupancy.assigned.setdefault(str(fsm), set()).add(str(state))
            # The constructor's value is assertable before env.run() is ever
            # called, so it counts as observable even though no time passes while
            # it holds. Reporting it would repeat the mistake this check exists to
            # catch: claiming a state cannot be seen when a test can in fact see
            # it.
            occupancy.observable.setdefault(str(fsm), set()).add(str(state))

    def __setitem__(self, fsm, state) -> None:
        previous = self.get(fsm)
        # A process whose first act is to announce the state the constructor
        # already set (RESET -> RESET) is agreeing, not transitioning. A later
        # self-entry is real and is kept -- that is the duplicate-idle shape.
        first = fsm not in self._entered
        self._entered.add(fsm)
        if not (first and previous == state):
            self._sink.append((str(fsm), str(previous), str(state)))

        now = self._now()
        key = str(fsm)
        if now is not None and previous is not None:
            entered_at = self._since.get(key, now)
            if now > entered_at:
                # Time passed while `previous` was the current state, so somebody
                # could have observed it.
                self._occupancy.observable.setdefault(key, set()).add(str(previous))
        self._since[key] = now
        self._occupancy.assigned.setdefault(key, set()).add(str(state))
        self._occupancy.resting[key] = str(state)
        super().__setitem__(fsm, state)


def model_classes(ip: str) -> list[type]:
    """Classes defined in ``ip_model_automation.<ip>`` — filtered later by whether
    an instance actually turns out to carry an ``fsm_state`` mapping."""
    module = importlib.import_module(f"ip_model_automation.{ip}")
    return [obj for _, obj in inspect.getmembers(module, inspect.isclass) if obj.__module__ == module.__name__]


@dataclasses.dataclass
class Occupancy:
    """Which declared states were ever actually occupied, and which were not.

    ``assigned`` is every state the model ever wrote. ``observable`` is the
    subset that was still the current state when the clock advanced. A state in
    the first and not the second, and not a process's resting state at the end of
    the run, held for no simulated time, so no assertion in this run could have
    seen it, and a scenario naming it in ``fsm_coverage`` claims coverage no test
    could have.

    **This is measured under the IP's own test suite, which makes it a statement
    about the model as exercised, not about the model.** A state can be
    unoccupied here purely because the tests zero the latency that would have held
    it -- `arbitration_ip` is exactly that case, where passing `scan_latency`
    silently sets three other latencies to 0. Read a result here as "no current
    test could observe this", and check the defaults before concluding the model
    can never occupy it.
    """

    assigned: dict[str, set[str]] = dataclasses.field(default_factory=dict)
    #: States that were assertable: held while the clock advanced, or set by the
    #: constructor and therefore readable before the simulation starts.
    observable: dict[str, set[str]] = dataclasses.field(default_factory=dict)
    resting: dict[str, str] = dataclasses.field(default_factory=dict)

    def unobservable(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for fsm, states in self.assigned.items():
            seen = self.observable.get(fsm, set())
            for state in sorted(states - seen):
                # The state a process rests in when the run ends was occupied
                # from its assignment to the end of the simulation.
                if self.resting.get(fsm) != state:
                    out.append((fsm, state))
        return sorted(out)


@contextlib.contextmanager
def recording(classes: list[type]):
    """Swap each constructed model's ``fsm_state`` for a recording one."""
    taken: list[Transition] = []
    occupancy = Occupancy()
    originals = {cls: cls.__init__ for cls in classes}

    def make(original):
        def wrapper(self, *args, **kwargs):
            original(self, *args, **kwargs)
            state = getattr(self, "fsm_state", None)
            if isinstance(state, dict) and not isinstance(state, RecordingState):
                env = getattr(self, "env", None)
                now = (lambda e=env: e.now) if env is not None else None
                self.fsm_state = RecordingState(state, taken, occupancy, now)

        return wrapper

    for cls, original in originals.items():
        cls.__init__ = make(original)
    try:
        yield taken, occupancy
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
    with recording(classes) as (taken, occupancy):
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
        "declared": sorted(declared),
        "states_without_exit": no_exit,
        # Declared states the model wrote but never held for any simulated time.
        "unobservable_states": [t for t in occupancy.unobservable() if t in declared_states(template)],
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
            f"{len(result['states_without_exit'])} states with no declared exit, "
            f"{len(result['unobservable_states'])} declared states never observably occupied "
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
        for fsm, state in result["unobservable_states"]:
            lines.append(
                f"  NEVER OCCUPIED {fsm}: {state}  (under this IP's own tests, the clock never moved while it held)"
            )
    lines.append("")
    lines.append("* the state it leaves has no declared exit at all, so the template constrained nothing here")
    return lines


def sha256_text(path: Path) -> str:
    """Hash a subject file exactly as check_review_findings.py does.

    The two must agree or an emitted review would read as stale the moment it was
    written, so this deliberately mirrors that tool rather than inventing its own
    normalisation.
    """
    import hashlib

    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def finding_for(ip: str, kind: str, fsm: str, source: str, target: str, declared_exits: list[str]) -> dict[str, Any]:
    """One disagreement, written as a finding a person has to resolve.

    The tool states what it saw and refuses to guess which side is wrong. That is
    not modesty — on sram_ctrl_ip the answer split both ways across six
    transitions of this exact shape, and the one time an agent picked for itself
    the author reversed it.
    """
    exits = ", ".join(declared_exits) or "(none)"
    if kind == "undeclared":
        return {
            "id": "",
            "class": "fsm_structure",
            "severity": "high",
            "claim": f"{fsm} takes {source} -> {target}, which the template does not declare.",
            "template": (
                f"templates/{ip}.template.yaml fsm_processes.{fsm}.transitions declares no edge "
                f"{source} -> {target}. Declared exits from {source}: {exits}."
            ),
            "model": (
                f"src/ip_model_automation/{ip}.py set {fsm} to {target} while it was in {source}, "
                f"observed while running tests/test_{ip}.py. Reproduce with "
                f"`python tools/check_declared_transitions.py {ip}`."
            ),
            "why": (
                f"The contract and the model describe different machines at this point. Until someone "
                f"rules on which is right -- declare the edge, or stop the model taking it -- {fsm}'s "
                f"behaviour in {source} is not established, and no downstream stage should treat this "
                f"part of {ip} as done."
            ),
        }
    return {
        "id": "",
        "class": "fsm_structure",
        "severity": "medium",
        "claim": f"{fsm} declares {source} -> {target}, which no test ever drives.",
        "template": f"templates/{ip}.template.yaml fsm_processes.{fsm}.transitions declares {source} -> {target}.",
        "model": (
            f"Never observed across tests/test_{ip}.py. Either the model cannot take this edge, or no "
            f"scenario reaches it. Reproduce with `python tools/check_declared_transitions.py {ip}`."
        ),
        "why": (
            f"A declared edge nothing exercises is either behaviour the model does not implement or a "
            f"scenario gap. Both leave {fsm}'s {source} behaviour unverified, and which one it is "
            f"cannot be told from here."
        ),
    }


def emit_findings(result: dict[str, Any], ip: str) -> tuple[Path, int, str | None]:
    """Write this run's disagreements to reviews/<ip>.model.findings.yaml.

    Only the disagreements a person must rule on. The transitions leaving a state
    with no declared exit are excluded on purpose: the template constrained
    nothing there, so the model cannot be said to disagree with it, and filing
    those would bury the real ones under a systematic gap that belongs to the
    promotion gate instead.

    Refuses to overwrite findings it did not write. A reviewer's findings are
    somebody's reading of this model, and a tool must not delete them.
    """
    yaml = require_yaml()
    path = REPO_ROOT / "reviews" / f"{ip}.model.findings.yaml"

    if path.is_file():
        existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        foreign = [f for f in existing.get("findings") or [] if not str(f.get("id", "")).startswith("T")]
        if foreign:
            ids = ", ".join(str(f.get("id")) for f in foreign)
            return path, 0, f"{path.name} holds findings this tool did not write ({ids}); refusing to overwrite"

    dead_ends = {(fsm, state) for fsm, state in result["states_without_exit"]}
    declared_by_source: dict[tuple[str, str], list[str]] = {}
    for fsm, source, target in result["declared"]:
        declared_by_source.setdefault((fsm, source), []).append(target)

    findings = []
    for fsm, source, target in result["undeclared"]:
        if (fsm, source) in dead_ends:
            continue
        findings.append(finding_for(ip, "undeclared", fsm, source, target, declared_by_source.get((fsm, source), [])))
    for fsm, source, target in result["never_taken"]:
        findings.append(finding_for(ip, "never_taken", fsm, source, target, []))
    for index, finding in enumerate(findings, start=1):
        finding["id"] = f"T{index}"

    document = {
        "ip": ip,
        "kind": "model",
        "template_sha256": sha256_text(REPO_ROOT / f"templates/{ip}.template.yaml"),
        "model_sha256": sha256_text(REPO_ROOT / f"src/ip_model_automation/{ip}.py"),
        "tests_sha256": sha256_text(REPO_ROOT / f"tests/test_{ip}.py"),
        "findings": findings,
    }
    header = (
        f"# Transition disagreements between {ip}'s model and its template, recorded by\n"
        f"# tools/check_declared_transitions.py --emit-findings. Not an LLM review: this is a\n"
        f"# mechanical comparison of the transitions the model took while its own tests ran\n"
        f"# against the ones the template declares.\n"
        f"#\n"
        f"# The tool does not decide which side is wrong. Each finding is fixed -- by declaring\n"
        f"# the edge or by stopping the model taking it -- or dismissed by name in\n"
        f"# decisions/{ip}.md. Until then check_review_findings holds this IP as awaiting a\n"
        f"# person, and the states named below are not settled behaviour.\n"
        f"#\n"
        f"# Transitions leaving a state the template gives no exit for are deliberately absent.\n"
        f"# The template constrained nothing there, so there is no disagreement to rule on.\n"
    )
    body = yaml.safe_dump(document, sort_keys=False, width=100, allow_unicode=True)
    path.write_text(header + body, encoding="utf-8")
    return path, len(findings), None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ips", nargs="*", help="IP names; default is every IP with a template and a test module")
    parser.add_argument("--strict", action="store_true", help="exit 1 if any undeclared transition was taken")
    parser.add_argument(
        "--emit-findings",
        action="store_true",
        help="record each disagreement as a blocking finding in reviews/<ip>.model.findings.yaml",
    )
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

    if args.emit_findings:
        print()
        refused = False
        for result in results:
            path, count, error = emit_findings(result, result["ip"])
            if error:
                print(f"  SKIPPED {error}", file=sys.stderr)
                refused = True
                continue
            print(f"  wrote {path.relative_to(REPO_ROOT).as_posix()}: {count} finding(s)")
        print("\nrun `python tools/check_review_findings.py --list` to see them as the gate does")
        if refused:
            return 1

    if args.strict and undeclared_total:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
