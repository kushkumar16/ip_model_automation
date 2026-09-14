#!/usr/bin/env python3
"""Discover the target repository's root directory.

Every tool used to compute its root as ``Path(__file__).resolve().parents[1]``,
which only works when the tool script itself lives inside the target repo's
own ``tools/`` directory — i.e., when this framework is used from a clone of
itself. That breaks once the tooling is pip-installed and the script runs out
of site-packages instead.

Discovery instead walks upward from the current working directory, the same
way git/npm/eslint locate a project root: the first directory carrying
``target_profile.yaml`` (an explicit target-repo marker) or a ``.git`` entry
wins. This lets an installed console script operate on whatever repo the
caller's shell is currently in, while preserving today's behavior for the
``python tools/x.py`` workflow (run from the repo root, ``.git`` is found
immediately).
"""

from __future__ import annotations

import os
from pathlib import Path

_MARKERS = ("target_profile.yaml", ".git")


def find_repo_root(start: Path | str | None = None) -> Path:
    """Walk upward from *start* (default: cwd) for a target-repo marker."""
    current = Path(start if start is not None else os.getcwd()).resolve()
    for candidate in (current, *current.parents):
        if any((candidate / marker).exists() for marker in _MARKERS):
            return candidate
    return current
