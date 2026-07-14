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

Reads the template as parsed YAML (like ``template_lint.py``); only the model
files it cross-references are scanned as source text.

Usage::

    python tools/check_subsystem_wiring.py                 # all subsystem templates
    python tools/check_subsystem_wiring.py templates/dma_subsystem.template.yaml
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"


def _profile(repo_root: Path):
    """Load the target profile (paths + naming conventions)."""
    path = TOOLS_DIR / "target_profile.py"
    spec = importlib.util.spec_from_file_location("target_profile", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_profile(repo_root)


def require_yaml():
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("PyYAML is required. Install project requirements before running this tool.") from exc
    return yaml


def load_template(path: Path) -> dict[str, Any]:
    yaml = require_yaml()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: template did not parse to a mapping")
    return data


def discover_subsystem_templates(repo_root: Path) -> list[Path]:
    found = []
    for template in sorted(_profile(repo_root).templates_dir.glob("*.template.yaml")):
        data = load_template(template)
        if isinstance(data.get("subsystem"), dict):
            found.append(template)
    return found


def connection_endpoints(connections: list[dict[str, Any]]) -> list[str]:
    endpoints: list[str] = []
    for conn in connections:
        if isinstance(conn, dict):
            endpoints += [str(conn[key]) for key in ("from", "to") if key in conn]
    return endpoints


def _member_has_api(member_text: str, api: str) -> bool:
    return f"def {api}(" in member_text or f"self.{api}" in member_text


def check_file(repo_root: Path, template_path: Path) -> list[str]:
    errors: list[str] = []
    profile = _profile(repo_root)
    template = load_template(template_path)
    subsystem = template.get("subsystem")
    if not isinstance(subsystem, dict):
        return [f"{template_path.name}: no `subsystem:` section"]

    subsystem_name = template.get("ip", {}).get("name")
    if not subsystem_name:
        return [f"{template_path.name}: cannot find `ip.name`"]

    members = [
        (m["ip"], m["model"])
        for m in subsystem.get("members", [])
        if isinstance(m, dict) and "ip" in m and "model" in m
    ]
    if not members:
        errors.append("subsystem.members: no `{ip: ..., model: ...}` entries found")

    member_texts: dict[str, str] = {}
    for ip_name, model_class in members:
        if not profile.template_file(ip_name).is_file():
            errors.append(f"member `{ip_name}`: no promoted template templates/{ip_name}.template.yaml")
        member_model_path = profile.model_file(ip_name)
        if not member_model_path.is_file():
            errors.append(f"member `{ip_name}`: no model file for {ip_name}")
            continue
        member_text = member_model_path.read_text(encoding="utf-8")
        member_texts[ip_name] = member_text
        if f"class {model_class}" not in member_text:
            errors.append(f"member `{ip_name}`: class `{model_class}` not found in {ip_name}.py")

    subsystem_model_path = profile.model_file(subsystem_name)
    if not subsystem_model_path.is_file():
        errors.append(f"no subsystem model file for {subsystem_name}")
        subsystem_model_text = ""
    else:
        subsystem_model_text = subsystem_model_path.read_text(encoding="utf-8")
        expected_class = profile.model_class(subsystem_name)
        if f"class {expected_class}" not in subsystem_model_text:
            errors.append(f"{subsystem_name}.py: class `{expected_class}` not found")
        for ip_name, model_class in members:
            if f"{model_class}(" not in subsystem_model_text:
                errors.append(f"{subsystem_name}.py: does not instantiate member model `{model_class}`")

    member_ips = {ip_name for ip_name, _model in members}
    fsm_names = {str(fsm.get("name")) for fsm in template.get("fsm_processes", []) if isinstance(fsm, dict)}
    connections = subsystem.get("connections", [])
    endpoints = connection_endpoints(connections)
    if not endpoints:
        errors.append("subsystem.connections: no `{from: ..., to: ...}` entries found")
    for endpoint in endpoints:
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
