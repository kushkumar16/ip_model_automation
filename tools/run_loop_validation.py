#!/usr/bin/env python3
"""Run deterministic validation gates for the IP generation loop."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]


def require_yaml():
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("PyYAML is required. Install project requirements before running this tool.") from exc
    return yaml


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str


def load_yaml(path: Path) -> dict[str, Any]:
    yaml = require_yaml()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"{path}: expected YAML mapping")
    return data


def run_command(repo_root: Path, args: list[str], env: dict[str, str] | None = None) -> GateResult:
    proc = subprocess.run(args, cwd=repo_root, env=env, text=True, capture_output=True)
    name = " ".join(Path(part).name if part.endswith(".py") else part for part in args[1:3])
    output = (proc.stdout + proc.stderr).strip()
    detail = output.splitlines()[-1] if output else "no output"
    return GateResult(name=name, passed=proc.returncode == 0, detail=detail)


def python_env(repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(repo_root / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src_path if not existing else f"{src_path}{os.pathsep}{existing}"
    return env


def run_loop_once(repo_root: Path, harness: dict[str, Any]) -> list[GateResult]:
    templates = sorted(repo_root.glob(harness.get("inputs", {}).get("template_glob", "templates/*.template.yaml")))
    model_dir = repo_root / harness.get("inputs", {}).get("model_dir", "src/ip_model_automation")
    results: list[GateResult] = []

    lint_cmd = [sys.executable, str(repo_root / "tools" / "template_lint.py"), *[str(t.relative_to(repo_root)) for t in templates]]
    results.append(run_command(repo_root, lint_cmd))

    with tempfile.TemporaryDirectory(prefix="ip_loop_scaffold_") as scaffold_tmp:
        for template in templates:
            results.append(
                run_command(
                    repo_root,
                    [
                        sys.executable,
                        str(repo_root / "tools" / "generate_model_scaffold.py"),
                        str(template.relative_to(repo_root)),
                        "--output-dir",
                        scaffold_tmp,
                    ],
                )
            )

    with tempfile.TemporaryDirectory(prefix="ip_loop_prompt_") as prompt_tmp:
        for template in templates:
            results.append(
                run_command(
                    repo_root,
                    [
                        sys.executable,
                        str(repo_root / "tools" / "generate_prompt_pack.py"),
                        str(template.relative_to(repo_root)),
                        "--output-dir",
                        prompt_tmp,
                    ],
                )
            )

    results.append(run_command(repo_root, [sys.executable, str(repo_root / "tools" / "report_model_coverage.py")]))
    results.append(run_command(repo_root, [sys.executable, "-m", "unittest", "discover", "-s", "tests"], env=python_env(repo_root)))
    results.append(run_command(repo_root, [sys.executable, str(repo_root / "tools" / "validate_ip_flow.py")], env=python_env(repo_root)))

    if not model_dir.is_dir():
        results.append(GateResult("model_dir_exists", False, f"missing {model_dir}"))
    return results


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--harness", type=Path, default=REPO_ROOT / "harness" / "ip_generation_loop.yaml")
    parser.add_argument("--iterations", type=int, default=1)
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    harness_path = args.harness if args.harness.is_absolute() else repo_root / args.harness
    harness = load_yaml(harness_path)
    max_iterations = max(1, min(args.iterations, int(harness.get("loop_policy", {}).get("max_iterations", 3))))

    final_results: list[GateResult] = []
    for iteration in range(1, max_iterations + 1):
        print(f"loop iteration {iteration}/{max_iterations}")
        final_results = run_loop_once(repo_root, harness)
        for result in final_results:
            status = "OK" if result.passed else "FAIL"
            print(f"  {status} {result.name}: {result.detail}")
        if all(result.passed for result in final_results):
            print("loop validation: OK")
            return 0
        if harness.get("loop_policy", {}).get("stop_on_first_pass", True):
            break

    print("loop validation: FAIL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
