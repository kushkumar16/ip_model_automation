#!/usr/bin/env python3
"""End-to-end validation for the DLD -> template -> model -> test flow.

Extends ``validate_ip_flow.py`` with a front-end gate: every ``docs/<ip>_dld.md``
must have a promoted ``templates/<ip>.template.yaml`` that (a) lints and (b) covers
the FSMs the DLD declares (calibration/regression on the golden IPs). It then runs
the existing model/test validation.

Usage::

    python tools/validate_dld_flow.py            # full flow
    python tools/validate_dld_flow.py --skip-model-flow
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"


def _load(module_name: str):
    path = TOOLS_DIR / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"cannot load tool: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


template_lint = _load("template_lint")
check_cov = _load("check_template_coverage")
dld_tool = _load("dld_to_template")


def discover_dlds(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "docs").glob("*_dld.md"))


def validate_front_end(repo_root: Path) -> list[str]:
    """Return a list of failure messages for the DLD->template gate (empty if OK)."""
    failures: list[str] = []
    dlds = discover_dlds(repo_root)
    if not dlds:
        return ["no docs/*_dld.md files found"]

    print(f"checking DLD -> template coverage for {len(dlds)} DLDs")
    for dld_path in dlds:
        ip_name = dld_tool.ip_name_from_path(dld_path)
        template_path = repo_root / "templates" / f"{ip_name}.template.yaml"
        rel_dld = dld_path.relative_to(repo_root)

        if not template_path.is_file():
            failures.append(f"{rel_dld}: no promoted template templates/{ip_name}.template.yaml")
            print(f"  {ip_name}: MISSING template")
            continue

        lint_errors = template_lint.lint_file(template_path)
        if lint_errors:
            failures.append(f"{template_path.name}: lint failed ({len(lint_errors)} issues)")
            print(f"  {ip_name}: LINT FAIL")
            continue

        _report, cov_errors = check_cov.coverage(template_path, dld_path, strict=True)
        if cov_errors:
            for error in cov_errors:
                failures.append(f"{ip_name}: {error}")
            print(f"  {ip_name}: COVERAGE FAIL")
            continue

        print(f"  {ip_name}: OK")
    return failures


def run_model_flow(repo_root: Path) -> int:
    cmd = [sys.executable, str(TOOLS_DIR / "validate_ip_flow.py")]
    return subprocess.run(cmd, cwd=repo_root).returncode


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--skip-model-flow", action="store_true", help="only run the DLD->template gate")
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    failures = validate_front_end(repo_root)
    if failures:
        print("\nDLD -> template gate: FAIL", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("DLD -> template gate: OK\n")

    if args.skip_model_flow:
        print("DLD validation flow: OK (model flow skipped)")
        return 0

    rc = run_model_flow(repo_root)
    if rc != 0:
        print("\nDLD validation flow: FAIL (model/test flow)", file=sys.stderr)
        return rc
    print("\nDLD validation flow: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
