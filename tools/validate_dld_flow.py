#!/usr/bin/env python3
"""End-to-end validation for the DLD -> template -> model -> test flow.

Extends ``validate_ip_flow.py`` with a front-end gate: every ``dlds/<ip>_dld.md``
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
subsystem_wiring = _load("check_subsystem_wiring")
doc_renderer = _load("render_template_doc")


def discover_dlds(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "dlds").glob("*_dld.md"))


def validate_front_end(repo_root: Path) -> list[str]:
    """Return a list of failure messages for the DLD->template gate (empty if OK)."""
    failures: list[str] = []
    dlds = discover_dlds(repo_root)
    if not dlds:
        return ["no dlds/*_dld.md files found"]

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

    subsystem_templates = subsystem_wiring.discover_subsystem_templates(repo_root)
    if subsystem_templates:
        print(f"checking subsystem wiring for {len(subsystem_templates)} subsystem templates")
        for template_path in subsystem_templates:
            wiring_errors = subsystem_wiring.check_file(repo_root, template_path)
            if wiring_errors:
                for error in wiring_errors:
                    failures.append(f"{template_path.name}: {error}")
                print(f"  {template_path.stem.removesuffix('.template')}: WIRING FAIL")
            else:
                print(f"  {template_path.stem.removesuffix('.template')}: wiring OK")
    return failures


def render_template_docs(repo_root: Path) -> int:
    """Regenerate readable Markdown + HTML docs for every promoted template."""
    output_dir = repo_root / "reports" / "template_docs"
    template_paths = doc_renderer.discover_templates()
    for template_path in template_paths:
        template = doc_renderer.load_template(template_path)
        markdown = doc_renderer.render_document(template, template_path)
        ip_name = template.get("ip", {}).get("name", template_path.stem)
        output_dir.mkdir(parents=True, exist_ok=True)
        md_path = output_dir / template_path.name.replace(".template.yaml", ".template.md")
        html_path = output_dir / template_path.name.replace(".template.yaml", ".template.html")
        md_path.write_text(markdown, encoding="utf-8")
        html_path.write_text(
            doc_renderer.markdown_to_html(markdown, f"{ip_name} — Template Overview"), encoding="utf-8"
        )
    return len(template_paths)


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

    rendered = render_template_docs(repo_root)
    print(f"rendered readable template docs (md + html) for {rendered} templates -> reports/template_docs\n")

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
