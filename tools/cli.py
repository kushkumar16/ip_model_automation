#!/usr/bin/env python3
"""``ipmodel`` — one console-script entry point for every tool in ``tools/``.

Each subcommand is a tool module's own ``main(argv)`` (or, for the one tool
that reads ``sys.argv`` directly, ``main()``), looked up by name and invoked
in-process. The tools themselves are unchanged: each keeps its own argparse
parser, its own ``--help``, and stays runnable directly as
``python tools/x.py`` for local development. This file only adds a second,
installable way to reach them — ``ipmodel x`` — so the tooling can be
``pip install``-ed and pointed at any repo, without every subcommand's
argument handling being rewritten.

Subcommand names are the module's filename with underscores turned to
hyphens, e.g. ``tools/run_ci.py`` -> ``ipmodel run-ci``.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

_EXCLUDED = {"_repo_root", "target_profile", "cli", "__init__"}


def discover_commands() -> dict[str, str]:
    """Map ``command-name`` -> module name, for every tool alongside this file."""
    package_dir = Path(__file__).resolve().parent
    return {
        path.stem.replace("_", "-"): path.stem
        for path in sorted(package_dir.glob("*.py"))
        if path.stem not in _EXCLUDED
    }


def _load(module_name: str):
    try:
        return importlib.import_module(f".{module_name}", __package__)
    except ImportError:  # pragma: no cover - exercised via `python tools/cli.py`
        return importlib.import_module(module_name)


def _print_usage(commands: dict[str, str]) -> None:
    print("usage: ipmodel <command> [args...]")
    print()
    print("commands:")
    for name in sorted(commands):
        print(f"  {name}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    commands = discover_commands()

    if not argv:
        _print_usage(commands)
        return 1
    if argv[0] in ("-h", "--help"):
        _print_usage(commands)
        return 0

    command, rest = argv[0], argv[1:]
    module_name = commands.get(command)
    if module_name is None:
        print(f"ipmodel: unknown command {command!r}", file=sys.stderr)
        print("run `ipmodel --help` for the list of commands", file=sys.stderr)
        return 2

    module = _load(module_name)
    sys.argv = [f"ipmodel {command}", *rest]
    if len(inspect.signature(module.main).parameters) == 0:
        return module.main()
    return module.main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
