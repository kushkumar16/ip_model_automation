#!/usr/bin/env python3
"""The target profile: which codebase the IP-model tooling operates on.

Every path and naming convention the tools use to find an IP's artifacts —
where models, tests, templates, and DLDs live, and how a model file and class
are named — is declared here instead of hardcoded. `target_profile.yaml` at the
repo root supplies the values; anything it omits falls back to the defaults
below, which describe *this* repo. So with no config file present, the tooling
behaves exactly as before.

To drive a different SimPy framework, copy `target_profile.yaml` into that repo
(or edit it) and point its paths/patterns at that codebase's layout. This is the
seam that lets the same tooling amend models in another project without editing
tool code.

Usage from a tool::

    profile = target_profile.load_profile(REPO_ROOT)
    profile.model_file("mailbox_ip")     # -> <repo>/src/ip_model_automation/mailbox_ip.py
    profile.model_class("mailbox_ip")    # -> "MailboxIpModel"
"""

from __future__ import annotations

import re
from pathlib import Path

PROFILE_FILENAME = "target_profile.yaml"

DEFAULT_PATHS = {
    "model_dir": "src/ip_model_automation",
    "tests_dir": "tests",
    "templates_dir": "templates",
    "dlds_dir": "dlds",
    "reports_dir": "reports",
    "prompt_packs_dir": "prompt_packs",
}

DEFAULT_NAMING = {
    "model_file": "{ip}.py",
    "test_file": "test_{ip}.py",
    "template_file": "{ip}.template.yaml",
    "draft_file": "{ip}.template.draft.yaml",
    "dld_file": "{ip}_dld.md",
    "model_class": "{camel}Model",
}


def camelize(ip: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[_\-\s]+", ip) if part)


class Profile:
    """Resolved paths and naming conventions for one target codebase."""

    def __init__(self, repo_root: Path, paths: dict[str, str], naming: dict[str, str]):
        self.repo_root = repo_root
        self.paths = paths
        self.naming = naming

    # directories (absolute) --------------------------------------------- #
    def directory(self, key: str) -> Path:
        return self.repo_root / self.paths[key]

    @property
    def model_dir(self) -> Path:
        return self.directory("model_dir")

    @property
    def tests_dir(self) -> Path:
        return self.directory("tests_dir")

    @property
    def templates_dir(self) -> Path:
        return self.directory("templates_dir")

    @property
    def dlds_dir(self) -> Path:
        return self.directory("dlds_dir")

    @property
    def reports_dir(self) -> Path:
        return self.directory("reports_dir")

    @property
    def prompt_packs_dir(self) -> Path:
        return self.directory("prompt_packs_dir")

    # per-IP artifacts --------------------------------------------------- #
    def model_file(self, ip: str) -> Path:
        return self.model_dir / self.naming["model_file"].format(ip=ip)

    def test_file(self, ip: str) -> Path:
        return self.tests_dir / self.naming["test_file"].format(ip=ip)

    def template_file(self, ip: str) -> Path:
        return self.templates_dir / self.naming["template_file"].format(ip=ip)

    def draft_file(self, ip: str) -> Path:
        return self.templates_dir / self.naming["draft_file"].format(ip=ip)

    def dld_file(self, ip: str) -> Path:
        return self.dlds_dir / self.naming["dld_file"].format(ip=ip)

    def model_class(self, ip: str) -> str:
        return self.naming["model_class"].format(camel=camelize(ip), ip=ip)


def load_profile(repo_root: Path) -> Profile:
    paths = dict(DEFAULT_PATHS)
    naming = dict(DEFAULT_NAMING)
    config = Path(repo_root) / PROFILE_FILENAME
    if config.is_file():
        try:
            import yaml  # type: ignore
        except ModuleNotFoundError as exc:
            raise SystemExit("PyYAML is required to read target_profile.yaml.") from exc
        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        paths.update(data.get("paths") or {})
        naming.update(data.get("naming") or {})
    return Profile(Path(repo_root), paths, naming)
