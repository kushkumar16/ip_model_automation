#!/usr/bin/env python3
"""Wiring checks for subsystem templates.

Subsystems are templates with a top-level ``subsystem:`` section declaring
member IPs and the connections between member APIs and glue FSM processes
(see ``templates/dma_subsystem.template.yaml``). The generic lint cannot see
whether those declarations point at anything real, so this tool verifies:

- every member ``ip:`` has a promoted template and a flat model file;
- every member ``model:`` class is defined in that member's model file;
- the subsystem's own model file exists, defines ``<CamelName>Model``, and
  instantiates every declared member model class;
- every ``connections:`` endpoint is either ``<member_ip>.<api>`` where the
  member is declared and the API exists in the member's model file (method or
  attribute), or a bare name matching one of the subsystem's glue FSMs.

Dependency-free, in the style of ``template_lint.py``.

Usage::

    python tools/check_subsystem_wiring.py                 # all subsystem templates
    python tools/check_subsystem_wiring.py templates/dma_subsystem.template.yaml
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"

MEMBER_RE = re.compile(r"\{\s*ip:\s*(\w+)\s*,\s*model:\s*(\w+)")
CONNECTION_RE = re.compile(r"\{\s*from:\s*([\w.]+)\s*,\s*to:\s*([\w.]+)")
IP_NAME_RE = re.compile(r"^\s*name:\s*(\w+)\s*$", re.MULTILINE)


def _load_template_lint():
    path = TOOLS_DIR / "template_lint.py"
    spec = importlib.util.spec_from_file_location("template_lint", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"cannot load tool: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


template_lint = _load_template_lint()


def camel_model_name(ip_name: str) -> str:
    return "".join(part.capitalize() for part in ip_name.split("_")) + "Model"


def discover_subsystem_templates(repo_root: Path) -> list[Path]:
    found = []
    for template in sorted((repo_root / "templates").glob("*.template.yaml")):
        text = template.read_text(encoding="utf-8")
        if any(line == "subsystem:" for line in text.splitlines()):
            found.append(template)
    return found


def parse_members(subsystem_text: str) -> list[tuple[str, str]]:
    return MEMBER_RE.findall(subsystem_text)


def parse_connections(subsystem_text: str) -> list[tuple[str, str]]:
    return CONNECTION_RE.findall(subsystem_text)


def _member_has_api(member_text: str, api: str) -> bool:
    return f"def {api}(" in member_text or f"self.{api}" in member_text


def check_file(repo_root: Path, template_path: Path) -> list[str]:
    errors: list[str] = []
    text = template_path.read_text(encoding="utf-8")
    subsystem_text = "\n".join(template_lint.section_lines(text, "subsystem"))
    if not subsystem_text:
        return [f"{template_path.name}: no `subsystem:` section"]

    name_match = IP_NAME_RE.search(text)
    if name_match is None:
        return [f"{template_path.name}: cannot find `ip.name`"]
    subsystem_name = name_match.group(1)

    members = parse_members(subsystem_text)
    if not members:
        errors.append("subsystem.members: no `{ip: ..., model: ...}` entries found")

    package_dir = repo_root / "src" / "ip_model_automation"
    member_texts: dict[str, str] = {}
    for ip_name, model_class in members:
        member_template = repo_root / "templates" / f"{ip_name}.template.yaml"
        if not member_template.is_file():
            errors.append(f"member `{ip_name}`: no promoted template templates/{ip_name}.template.yaml")
        member_model_path = package_dir / f"{ip_name}.py"
        if not member_model_path.is_file():
            errors.append(f"member `{ip_name}`: no model file src/ip_model_automation/{ip_name}.py")
            continue
        member_text = member_model_path.read_text(encoding="utf-8")
        member_texts[ip_name] = member_text
        if f"class {model_class}" not in member_text:
            errors.append(f"member `{ip_name}`: class `{model_class}` not found in {ip_name}.py")

    subsystem_model_path = package_dir / f"{subsystem_name}.py"
    if not subsystem_model_path.is_file():
        errors.append(f"no subsystem model file src/ip_model_automation/{subsystem_name}.py")
        subsystem_model_text = ""
    else:
        subsystem_model_text = subsystem_model_path.read_text(encoding="utf-8")
        expected_class = camel_model_name(subsystem_name)
        if f"class {expected_class}" not in subsystem_model_text:
            errors.append(f"{subsystem_name}.py: class `{expected_class}` not found")
        for ip_name, model_class in members:
            if f"{model_class}(" not in subsystem_model_text:
                errors.append(f"{subsystem_name}.py: does not instantiate member model `{model_class}`")

    member_ips = {ip_name for ip_name, _model in members}
    fsm_names = set(template_lint.collect_fsm_names(text))
    connections = parse_connections(subsystem_text)
    if not connections:
        errors.append("subsystem.connections: no `{from: ..., to: ...}` entries found")
    for endpoint in [ep for pair in connections for ep in pair]:
        if "." in endpoint:
            member, api = endpoint.split(".", 1)
            if member not in member_ips:
                errors.append(f"connection endpoint `{endpoint}`: `{member}` is not a declared member")
                continue
            member_text = member_texts.get(member)
            if member_text is not None and not _member_has_api(member_text, api):
                errors.append(f"connection endpoint `{endpoint}`: `{api}` not found in {member}.py")
        elif endpoint not in fsm_names:
            errors.append(f"connection endpoint `{endpoint}`: not a glue FSM of {subsystem_name}")

    return errors


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("templates", nargs="*", type=Path)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    templates = args.templates or discover_subsystem_templates(repo_root)
    if not templates:
        print("no subsystem templates found")
        return 0

    failed = False
    for template in templates:
        errors = check_file(repo_root, Path(template))
        if errors:
            failed = True
            print(f"{template}: FAIL")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"{template}: wiring OK")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
