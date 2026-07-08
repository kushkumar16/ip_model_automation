#!/usr/bin/env python3
"""Check that a template captures the FSMs/interfaces declared by its DLD.

This is the gate for the ``dlds/<ip>_dld.md`` -> ``templates/<ip>.template.yaml``
step. It ensures the template did not silently drop behavior the DLD declares.

Hard failures (exit 1):
  * a DLD FSM has no matching template FSM, or
  * the DLD's stated ``Total FSM/processes`` count disagrees with the template's
    ``fsm_relationships.fsm_count``, or
  * (with ``--strict``) the template still contains ``TODO_REVIEW`` markers.

Interfaces are reported but not hard-failed: golden templates legitimately
consolidate DLD interface sections (e.g. a watchdog-heartbeat section folded into
another interface), so a 1:1 match is not expected.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent


def _load_sibling(module_name: str):
    path = TOOLS_DIR / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"cannot load tool: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dld = _load_sibling("dld_to_template")


def load_template(path: Path) -> dict[str, Any]:
    yaml = dld.require_yaml()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: template did not parse to a mapping")
    return data


def dld_fsm_names(dld_text: str) -> list[str]:
    return [f["name"] for f in dld.extract_fsms(dld_text.splitlines())]


def dld_interface_names(dld_text: str) -> list[str]:
    return [i["name"] for i in dld.extract_interfaces(dld_text.splitlines())]


def coverage(template_path: Path, dld_path: Path, strict: bool) -> tuple[list[str], list[str]]:
    template = load_template(template_path)
    dld_text = dld_path.read_text(encoding="utf-8")

    report: list[str] = []
    errors: list[str] = []

    tmpl_fsms = [str(f["name"]) for f in template.get("fsm_processes", [])]
    doc_fsms = dld_fsm_names(dld_text)
    tmpl_set, doc_set = set(tmpl_fsms), set(doc_fsms)

    report.append(f"FSMs: template={len(tmpl_fsms)} dld={len(doc_fsms)}")
    missing = sorted(doc_set - tmpl_set)
    extra = sorted(tmpl_set - doc_set)
    if missing:
        errors.append(f"template is missing DLD FSMs: {', '.join(missing)}")
    if extra:
        report.append(f"  note: template FSMs not named in DLD headings: {', '.join(extra)}")
    if not missing and not extra:
        report.append("  OK: FSM names match DLD exactly")

    declared = dld.extract_fsm_count(dld_text, len(doc_fsms))
    tmpl_count = template.get("fsm_relationships", {}).get("fsm_count")
    report.append(f"fsm_count: template={tmpl_count} dld_declared={declared}")
    if isinstance(tmpl_count, int) and tmpl_count != declared:
        errors.append(f"fsm_count mismatch: template={tmpl_count} but DLD declares {declared}")

    doc_ifaces = dld_interface_names(dld_text)
    tmpl_ifaces = [str(i.get("name")) for i in template.get("interfaces", [])]
    report.append(f"interfaces (report-only): template={tmpl_ifaces} dld_sections={doc_ifaces}")

    todo_count = template_path.read_text(encoding="utf-8").count(dld.TODO)
    if todo_count:
        message = f"{todo_count} unresolved {dld.TODO} marker(s)"
        if strict:
            errors.append(message)
        else:
            report.append(f"  warning: {message}")

    return report, errors


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path, help="templates/<ip>.template.yaml (or .draft.yaml)")
    parser.add_argument("dld", type=Path, help="dlds/<ip>_dld.md")
    parser.add_argument("--strict", action="store_true", help="also fail on TODO_REVIEW markers")
    args = parser.parse_args(argv)

    for path in (args.template, args.dld):
        if not path.is_file():
            raise SystemExit(f"not found: {path}")

    report, errors = coverage(args.template, args.dld, args.strict)
    print(f"{args.template.name} vs {args.dld.name}")
    for line in report:
        print(f"  {line}")
    if errors:
        print("FAIL", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("coverage: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
