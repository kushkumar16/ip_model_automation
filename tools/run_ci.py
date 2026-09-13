#!/usr/bin/env python3
"""Run every repo-wide gate in one command — continuous integration, locally.

CI here is deliberately local: the gates run on this machine, before a push, and
nowhere else. That is a supported choice, but it removes the two things a hosted
runner gives for free, so this tool has to replace them:

* **A clean environment.** A hosted runner installs `requirements.txt` from
  scratch, so an undeclared dependency fails immediately. Locally, a gate can
  depend on a package someone installed by hand and pass forever — until a fresh
  clone. The declared set is checked before any gate runs.
* **A defined commit.** A hosted runner tests the commit that was pushed. A local
  hook tests the files on disk, which with a dirty tree are not the same thing,
  so the run says so.

Beyond that: a branch can be pushed with a failing coverage threshold, a stale
Word overview, or an unstamped normalization, and nothing would say so. This is
the thing to run before pushing, and `.githooks/pre-push` runs it for you.

**The harness YAML stays the source of truth.** The repo-wide stages are read
from `harness/ip_generation_loop.yaml` (`scope: repo`) rather than listed again
here, so adding a gate to the pipeline adds it to CI with no edit to this file —
the same discipline `auto_ip_pipeline.py` follows. A test asserts the two cannot
drift apart.

A few repo-wide checks are deliberately *not* pipeline stages: they guard
artifacts the pipeline produces rather than steps it runs, and they are listed in
``EXTRA_CHECKS`` with the reason.

Usage::

    python tools/run_ci.py            # every gate, stop at the end, report all
    python tools/run_ci.py --fail-fast
    python tools/run_ci.py --list
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = REPO_ROOT / "harness" / "ip_generation_loop.yaml"
REQUIREMENTS_PATH = REPO_ROOT / "requirements.txt"

# Repo-wide guards that are not pipeline stages. Each protects an artifact the
# pipeline writes, so it has no place in the per-IP stage sequence — but a push
# that breaks one is exactly as broken as a failing test.
EXTRA_CHECKS: list[tuple[str, str, str]] = [
    (
        "model_provenance",
        "python tools/check_model_provenance.py",
        "every model is stamped against the template revision it was built from",
    ),
    (
        "dld_normalization",
        "python tools/check_dld_normalization.py",
        "every normalized DLD is faithful to its author source, and human-stamped",
    ),
    (
        "overview_sync",
        "python tools/check_overview_sync.py",
        "the Word overview has not drifted from project_overview.md",
    ),
    (
        "review_findings",
        "python tools/check_review_findings.py",
        "every recorded model or normalization review finding is fixed or dismissed by name",
    ),
]


def missing_requirements() -> list[str]:
    """Declared dependencies that are not importable in this interpreter.

    Hosted CI starts from a clean machine and installs `requirements.txt`, so a
    missing or undeclared dependency fails loudly on the first run. Local CI has
    no such moment: it runs in whatever environment happens to exist, and a gate
    that needs a package someone installed by hand two months ago passes here and
    fails for everyone else. Checking the declared set is the cheapest substitute
    for the clean room this repo does not have.
    """
    import_names = {"pyyaml": "yaml", "python-docx": "docx", "jsonschema": "jsonschema"}
    missing: list[str] = []
    for line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
        name = re.split(r"[<>=!~\s]", line.strip(), maxsplit=1)[0]
        if not name or name.startswith("#"):
            continue
        if importlib.util.find_spec(import_names.get(name.lower(), name.lower().replace("-", "_"))) is None:
            missing.append(name)
    return missing


def working_tree_is_dirty() -> bool:
    """Whether the tree differs from HEAD.

    A local hook checks the files on disk, not the commits being pushed. With a
    dirty tree those are different things: the gates can pass on uncommitted work
    while the commits actually leaving the machine fail. Hosted CI never has this
    problem because it starts from the pushed commit — so local CI has to say so
    out loud.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    return bool(result.stdout.strip())


def install_hook() -> int:
    result = subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"], cwd=REPO_ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"could not set core.hooksPath: {result.stderr.strip()}", file=sys.stderr)
        return 1
    print("pre-push hook enabled (core.hooksPath = .githooks)")
    print("the gates now run before every push; SKIP_CI=1 git push bypasses them")
    return 0


def hook_enabled() -> bool:
    result = subprocess.run(
        ["git", "config", "core.hooksPath"], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    return result.stdout.strip() == ".githooks"


def require_yaml():
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("PyYAML is required. Install project requirements before running this tool.") from exc
    return yaml


def harness_repo_gates() -> list[tuple[str, str, str]]:
    """The `scope: repo` stages, in the order the harness declares them."""
    yaml = require_yaml()
    harness = yaml.safe_load(HARNESS_PATH.read_text(encoding="utf-8"))
    gates: list[tuple[str, str, str]] = []
    for stage in harness.get("stages", []):
        if not isinstance(stage, dict) or stage.get("scope") != "repo":
            continue
        command = str(stage["command"]).replace("python ", f"{sys.executable} ", 1)
        note = " ".join(str(stage.get("notes", "")).split())[:90]
        # Console output stays ASCII: the notes are prose from the YAML, and a
        # stray em-dash renders as a replacement character on a cp1252 console.
        gates.append((str(stage["name"]), command, note.encode("ascii", "replace").decode("ascii")))
    return gates


def all_gates() -> list[tuple[str, str, str]]:
    extras = [(name, cmd.replace("python ", f"{sys.executable} ", 1), why) for name, cmd, why in EXTRA_CHECKS]
    return harness_repo_gates() + extras


def run_gate(name: str, command: str) -> tuple[bool, str, float]:
    started = time.monotonic()
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output, time.monotonic() - started


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fail-fast", action="store_true", help="stop at the first failing gate")
    parser.add_argument("--list", action="store_true", help="list the gates without running them")
    parser.add_argument(
        "--install-hook", action="store_true", help="enable the pre-push hook for this clone, then exit"
    )
    args = parser.parse_args(argv)

    if args.install_hook:
        return install_hook()

    gates = all_gates()
    if args.list:
        for name, command, why in gates:
            print(f"  {name:20} {command}")
            if why:
                print(f"  {'':20} {why}")
        return 0

    missing = missing_requirements()
    if missing:
        print("CI: FAIL - declared dependencies are not installed: " + ", ".join(missing), file=sys.stderr)
        print("  python -m pip install -r requirements.txt", file=sys.stderr)
        return 1

    if not hook_enabled():
        print("note: the pre-push hook is not enabled for this clone, so these gates run only")
        print("      when you remember to. Enable it: python tools/run_ci.py --install-hook\n")

    if working_tree_is_dirty():
        print("note: the working tree is dirty, so these gates check the files on disk, not")
        print("      the commits being pushed. Commit first for the two to agree.\n")

    print(f"running {len(gates)} repo-wide gate(s)\n")
    failures: list[tuple[str, str]] = []
    for name, command, _why in gates:
        ok, output, seconds = run_gate(name, command)
        print(f"  {'PASS' if ok else 'FAIL'}  {name:22} {seconds:5.1f}s")
        if not ok:
            failures.append((name, output))
            if args.fail_fast:
                break

    if not failures:
        print("\nCI: OK - every repo-wide gate passes")
        return 0

    for name, output in failures:
        print(f"\n=== {name} ===")
        print(output[-2500:].rstrip())
    print(f"\nCI: FAIL ({len(failures)} of {len(gates)} gate(s))", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
