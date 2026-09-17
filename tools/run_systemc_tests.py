#!/usr/bin/env python3
"""Compile and run the SystemC testbench for each opted-in IP, for real.

Sibling to `python -m unittest tests.test_<ip>` (SimPy): every IP whose
template declares `ip.modeling_backends` including `systemc` must carry
`systemc/models/<ip>.h` + `.cpp` and `systemc/tests/test_<ip>.cpp`. This tool
compiles the pair with g++ against the system SystemC library and runs the
resulting binary -- a real compile and a real simulated execution, exit code
0 only if every scenario the testbench itself reports passed.

An IP that omits `modeling_backends`, or names only `simpy`, is skipped
entirely and this tool has no opinion about it -- the SystemC backend is
opt-in per IP, not a repo-wide requirement.

Usage::

    python tools/run_systemc_tests.py            # every opted-in IP
    python tools/run_systemc_tests.py watchdog_ip
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from ._repo_root import find_repo_root
except ImportError:  # not running as part of the `ip_model_automation.tools`
    # package (e.g. `python tools/x.py`, or a test loading this file
    # directly via importlib) -- fall back to a sibling top-level import.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _repo_root import find_repo_root

REPO_ROOT = find_repo_root()
TEMPLATES_DIR = REPO_ROOT / "templates"
MODELS_DIR = REPO_ROOT / "systemc" / "models"
TESTS_DIR = REPO_ROOT / "systemc" / "tests"


def require_yaml():
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("PyYAML is required. Install project requirements before running this tool.") from exc
    return yaml


def systemc_ips() -> list[str]:
    """IPs whose template opts into the systemc backend, sorted for stable output."""
    yaml = require_yaml()
    ips = []
    for template_path in sorted(TEMPLATES_DIR.glob("*.template.yaml")):
        data = yaml.safe_load(template_path.read_text(encoding="utf-8"))
        backends = (data.get("ip", {}) or {}).get("modeling_backends") or ["simpy"]
        if "systemc" in backends:
            ips.append(str(data["ip"]["name"]))
    return ips


def require_pkg_config_systemc() -> list[str]:
    """The compiler flags pkg-config reports for the installed SystemC library."""
    if shutil.which("pkg-config") is None:
        raise SystemExit("pkg-config is required to locate the SystemC library. Install it before running this tool.")
    result = subprocess.run(["pkg-config", "--cflags", "--libs", "systemc"], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise SystemExit(
            "SystemC development library not found (pkg-config --cflags --libs systemc failed). "
            "Install it, e.g. `apt-get install libsystemc-dev`, before running SystemC tests.\n"
            f"{result.stderr}"
        )
    return result.stdout.split()


def compile_and_run(ip_name: str, pkg_config_flags: list[str]) -> tuple[bool, str]:
    """Compile ip_name's SystemC model + testbench and run it. Returns (passed, output)."""
    header = MODELS_DIR / f"{ip_name}.h"
    source = MODELS_DIR / f"{ip_name}.cpp"
    test_source = TESTS_DIR / f"test_{ip_name}.cpp"
    missing = [p for p in (header, source, test_source) if not p.is_file()]
    if missing:
        rel = ", ".join(str(p.relative_to(REPO_ROOT)) for p in missing)
        return False, f"missing SystemC source file(s): {rel}"

    # A composing model (e.g. storage_pipeline_subsystem, which embeds
    # ArbitrationIpModel/CompletionIpModel as sub-modules) only #includes its
    # member IPs' headers, not their .cpp -- so every model source under
    # systemc/models/ is linked in, not just this IP's own. Cheap: a handful
    # of small translation units, and it can never drift out of sync with
    # whatever models/*.cpp actually exist.
    other_sources = sorted(p for p in MODELS_DIR.glob("*.cpp") if p != source)

    with tempfile.TemporaryDirectory(prefix=f"systemc-{ip_name}-") as tmp:
        binary = Path(tmp) / ip_name
        compile_cmd = [
            "g++",
            "-std=c++17",
            "-Wall",
            "-I",
            str(MODELS_DIR),
            str(test_source),
            str(source),
            *(str(p) for p in other_sources),
            "-o",
            str(binary),
            *pkg_config_flags,
        ]
        compiled = subprocess.run(compile_cmd, capture_output=True, text=True, timeout=120)
        if compiled.returncode != 0:
            return False, f"compile failed:\n{compiled.stdout}{compiled.stderr}"

        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
        output = ran.stdout + ran.stderr
        return ran.returncode == 0, output


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ips", nargs="*", help="IP names to test (default: every IP opted into systemc)")
    args = parser.parse_args(argv)

    ips = args.ips or systemc_ips()
    if not ips:
        print("no IP declares ip.modeling_backends including 'systemc' -- nothing to do")
        return 0

    pkg_config_flags = require_pkg_config_systemc()
    failed = []
    for ip_name in ips:
        passed, output = compile_and_run(ip_name, pkg_config_flags)
        print(f"=== {ip_name} ===")
        print(output.rstrip())
        if passed:
            print(f"{ip_name}: PASS")
        else:
            print(f"{ip_name}: FAIL")
            failed.append(ip_name)
        print()

    if failed:
        print(f"FAIL: {len(failed)}/{len(ips)} SystemC IP(s) failed: {', '.join(failed)}")
        return 1
    print(f"OK: {len(ips)}/{len(ips)} SystemC IP(s) passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
