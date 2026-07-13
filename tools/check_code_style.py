#!/usr/bin/env python3
"""Coding-style gate: ruff lint + format check over src/, tools/, and tests/.

The rules live in ruff.toml (the machine-readable half of the coding style
guide at skills/ip-model-generation/references/coding_style.md). Run with
--fix to apply auto-fixes and reformat instead of just checking.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

CHECKED_PATHS = ["src", "tools", "tests"]


def run_ruff(args: list[str], paths: list[str] | None = None) -> int:
    command = [sys.executable, "-m", "ruff", *args, *(CHECKED_PATHS if paths is None else paths)]
    return subprocess.run(command).returncode


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="apply lint auto-fixes and reformat in place")
    args = parser.parse_args(argv)

    probe = subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True)
    if probe.returncode != 0:
        print("ruff is required. Install project requirements before running this tool.")
        return 2

    if args.fix:
        lint_rc = run_ruff(["check", "--fix"])
        format_rc = run_ruff(["format"])
    else:
        lint_rc = run_ruff(["check"])
        format_rc = run_ruff(["format", "--check"])

    if lint_rc == 0 and format_rc == 0:
        print("code style: OK")
        return 0
    print("code style: FAILED (see findings above; run `python tools/check_code_style.py --fix` to auto-fix)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
