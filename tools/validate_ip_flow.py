#!/usr/bin/env python3
"""Run the full IP model template-to-test validation flow."""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_tool(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load tool: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snake_to_camel(name: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[_\-\s]+", name) if part)


def sanitize_identifier(name: str) -> str:
    cleaned = re.sub(r"\W+", "_", name.strip().lower())
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned


def discover_templates(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "templates").glob("*.template.yaml"))


def run_template_lint(repo_root: Path, templates: list[Path]) -> None:
    linter = repo_root / "tools" / "template_lint.py"
    cmd = [sys.executable, str(linter), *[str(path.relative_to(repo_root)) for path in templates]]
    subprocess.run(cmd, cwd=repo_root, check=True)


def scaffold_expectations(template: dict[str, Any]) -> tuple[str, list[str]]:
    ip_name = template["ip"]["name"]
    class_name = f"{snake_to_camel(ip_name)}Model"
    process_names = [
        f"def {sanitize_identifier(str(fsm['name']))}_process"
        for fsm in template.get("fsm_processes", [])
        if fsm.get("simpy_process") is True
    ]
    return class_name, process_names


def validate_scaffolds(repo_root: Path, templates: list[Path]) -> None:
    generator = load_tool("generate_model_scaffold", repo_root / "tools" / "generate_model_scaffold.py")
    with tempfile.TemporaryDirectory(prefix="ip_scaffold_") as tmpdir:
        output_dir = Path(tmpdir)
        for template_path in templates:
            template = generator.load_template(template_path)
            target = generator.write_scaffold(template_path, output_dir, stdout=False)
            if target is None or not target.is_file():
                raise RuntimeError(f"{template_path}: scaffold file was not generated")
            text = target.read_text(encoding="utf-8")
            class_name, process_names = scaffold_expectations(template)
            if f"class {class_name}" not in text:
                raise RuntimeError(f"{template_path}: generated scaffold missing class {class_name}")
            for process_name in process_names:
                if process_name not in text:
                    raise RuntimeError(f"{template_path}: generated scaffold missing `{process_name}`")
            print(f"{template_path.relative_to(repo_root)}: scaffold OK")


def run_unit_tests(repo_root: Path) -> None:
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
    env = os.environ.copy()
    src_path = str(repo_root / "src")
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src_path if not existing_pythonpath else f"{src_path}{os.pathsep}{existing_pythonpath}"
    subprocess.run(cmd, cwd=repo_root, env=env, check=True)


def run_coverage_report(repo_root: Path) -> None:
    reporter = repo_root / "tools" / "report_model_coverage.py"
    subprocess.run([sys.executable, str(reporter)], cwd=repo_root, check=True)


def validate_prompt_packs(repo_root: Path, templates: list[Path]) -> None:
    generator = load_tool("generate_prompt_pack", repo_root / "tools" / "generate_prompt_pack.py")
    with tempfile.TemporaryDirectory(prefix="ip_prompt_pack_") as tmpdir:
        output_dir = Path(tmpdir)
        for template_path in templates:
            template = generator.load_template(template_path)
            target = generator.write_prompt_pack(template_path, output_dir, stdout=False)
            if target is None or not target.is_file():
                raise RuntimeError(f"{template_path}: prompt pack was not generated")
            text = target.read_text(encoding="utf-8")
            ip_name = template["ip"]["name"]
            if f"# Prompt Pack: {ip_name}" not in text:
                raise RuntimeError(f"{template_path}: prompt pack missing title")
            if f"src/ip_model_automation/{ip_name}.py" not in text:
                raise RuntimeError(f"{template_path}: prompt pack missing target model path")
            print(f"{template_path.relative_to(repo_root)}: prompt pack OK")


def validate(repo_root: Path, skip_tests: bool = False) -> None:
    templates = discover_templates(repo_root)
    if not templates:
        raise RuntimeError("no templates found")
    print(f"linting {len(templates)} templates")
    run_template_lint(repo_root, templates)
    print(f"generating {len(templates)} scaffolds")
    validate_scaffolds(repo_root, templates)
    print("checking template FSM coverage")
    run_coverage_report(repo_root)
    print("generating prompt packs")
    validate_prompt_packs(repo_root, templates)
    if not skip_tests:
        print("running unit tests")
        run_unit_tests(repo_root)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args(argv)

    validate(args.repo_root.resolve(), skip_tests=args.skip_tests)
    print("IP validation flow: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
