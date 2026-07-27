#!/usr/bin/env python3
"""Run every repo-wide gate in one command — continuous integration, locally.

This repo has no hosted CI, so its gates run only when someone remembers to run
them. That is a real gap rather than a stylistic one: a branch can be pushed and
merged with a failing coverage threshold, a stale Word overview, or an unstamped
normalization, and nothing would say so. This tool is the thing to run before
pushing, and `.githooks/pre-push` runs it for you.

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
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = REPO_ROOT / "harness" / "ip_generation_loop.yaml"

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
]


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
    args = parser.parse_args(argv)

    gates = all_gates()
    if args.list:
        for name, command, why in gates:
            print(f"  {name:20} {command}")
            if why:
                print(f"  {'':20} {why}")
        return 0

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
