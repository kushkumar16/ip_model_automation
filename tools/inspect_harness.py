#!/usr/bin/env python3
"""Inspect the IP generation harness configuration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]


def require_yaml():
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("PyYAML is required. Install project requirements before running this tool.") from exc
    return yaml


def load_harness(path: Path) -> dict[str, Any]:
    yaml = require_yaml()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"{path}: expected YAML mapping")
    return data


def inspect(repo_root: Path, harness_path: Path) -> list[str]:
    config = load_harness(harness_path)
    lines = [
        f"harness: {config.get('name')}",
        f"description: {config.get('description')}",
        f"agent_contract: {config.get('agent_contract')}",
        "stages:",
    ]
    for stage in config.get("stages", []):
        required = "required" if stage.get("required") else "optional"
        lines.append(f"  - {stage.get('name')} ({required}): {stage.get('command')}")
    template_glob = config.get("inputs", {}).get("template_glob", "templates/*.template.yaml")
    templates = sorted(repo_root.glob(template_glob))
    lines.append(f"templates: {len(templates)}")
    for template in templates:
        lines.append(f"  - {template.relative_to(repo_root)}")
    return lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--harness", type=Path, default=REPO_ROOT / "harness" / "ip_generation_loop.yaml")
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    harness_path = args.harness if args.harness.is_absolute() else repo_root / args.harness
    print("\n".join(inspect(repo_root, harness_path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
