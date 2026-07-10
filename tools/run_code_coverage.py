#!/usr/bin/env python3
"""Run the unit test suite under coverage.py and report line coverage of the
IP models (src/ip_model_automation).

This is *code* coverage — which model lines the unit tests actually execute —
and complements tools/report_model_coverage.py, which reports FSM/scenario
coverage declared in the templates.

Output lands in reports/code_coverage/ (gitignored): coverage.txt always,
plus a browsable HTML report with --html. Exit code is nonzero if the tests
fail, total coverage is below --fail-under, or any single model file is
below --fail-under-file.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = REPO_ROOT / "src" / "ip_model_automation"
OUTPUT_DIR = REPO_ROOT / "reports" / "code_coverage"


def require_coverage() -> None:
    try:
        import coverage  # type: ignore # noqa: F401
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "coverage is required. Install project requirements before running this tool."
        ) from exc


def run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--html", action="store_true", help="Also write an HTML report to reports/code_coverage/html/"
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=0.0,
        help="Fail if total line coverage is below this percentage (default: 0, informational)",
    )
    parser.add_argument(
        "--fail-under-file",
        type=float,
        default=0.0,
        help="Fail if any single model file's line coverage is below this percentage (default: 0, informational)",
    )
    args = parser.parse_args(argv)

    require_coverage()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "COVERAGE_FILE": str(OUTPUT_DIR / ".coverage"),
    }

    tests = run(
        [sys.executable, "-m", "coverage", "run", f"--source={SOURCE_DIR}", "-m", "unittest", "discover", "-s", "tests"],
        env,
    )
    if tests.returncode != 0:
        print((tests.stdout or "") + (tests.stderr or ""))
        print("unit tests failed; no coverage report generated")
        return tests.returncode

    report = run(
        [sys.executable, "-m", "coverage", "report", f"--fail-under={args.fail_under}", "--precision=1"],
        env,
    )
    table = (report.stdout or "") + (report.stderr or "")
    print(table.rstrip())
    (OUTPUT_DIR / "coverage.txt").write_text(table, encoding="utf-8")
    print(f"\nwritten: {(OUTPUT_DIR / 'coverage.txt').relative_to(REPO_ROOT)}")

    if args.html:
        html = run([sys.executable, "-m", "coverage", "html", "-d", str(OUTPUT_DIR / "html")], env)
        if html.returncode != 0:
            print((html.stdout or "") + (html.stderr or ""))
            return html.returncode
        print(f"written: {(OUTPUT_DIR / 'html' / 'index.html').relative_to(REPO_ROOT)}")

    below_threshold = per_file_shortfalls(env, args.fail_under_file)
    for path, percent in below_threshold:
        print(f"FAIL: {path} at {percent:.1f}% (below --fail-under-file={args.fail_under_file})")

    if report.returncode != 0:
        print(f"FAIL: total coverage below --fail-under={args.fail_under}")
    if below_threshold:
        return 2
    return report.returncode


def per_file_shortfalls(env: dict[str, str], threshold: float) -> list[tuple[str, float]]:
    """Return (path, percent) for every measured file below the threshold."""
    if threshold <= 0.0:
        return []
    json_path = OUTPUT_DIR / "coverage.json"
    result = run([sys.executable, "-m", "coverage", "json", "-o", str(json_path)], env)
    if result.returncode != 0:
        print((result.stdout or "") + (result.stderr or ""))
        raise SystemExit("coverage json export failed; cannot apply --fail-under-file")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    shortfalls = [
        (path, summary)
        for path, entry in sorted(data.get("files", {}).items())
        if (summary := float(entry["summary"]["percent_covered"])) < threshold
    ]
    return shortfalls


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
