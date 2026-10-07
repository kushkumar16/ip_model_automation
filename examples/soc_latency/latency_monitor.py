"""Latency checker shared by every approach. New file: nothing here exists in the baseline.

- LatencyMonitor       records latency per pipeline and logs an ERROR over budget (all approaches)
- LatencyMonitor.wrap  wraps a per-transaction generator function            (approach A)
- LatencyCheckedMixin  overrides an IP block's _process/_run_stage hooks     (approach B)
- LatencyProbe         Store pair that stamps on ingress, checks on egress   (approach C)
Approach D needs nothing extra: it calls LatencyMonitor.record() directly.

Budgets come from latency_budgets.yaml (see LatencyMonitor.from_yaml).
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import simpy
import yaml

log = logging.getLogger("latency")

DEFAULT_CONFIG = Path(__file__).with_name("latency_budgets.yaml")


@dataclass
class LatencyMonitor:
    env: simpy.Environment
    default_budget: float = math.inf
    budgets: dict[str, float] = field(default_factory=dict)  # "ip.pipeline" or "ip.pipeline.stage"
    samples: dict[str, list[float]] = field(default_factory=dict)
    violations: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, env: simpy.Environment, path: Path | str = DEFAULT_CONFIG) -> LatencyMonitor:
        cfg = yaml.safe_load(Path(path).read_text()) or {}
        return cls(
            env,
            default_budget=float(cfg.get("default_budget", math.inf)),
            budgets={k: float(v) for k, v in (cfg.get("budgets") or {}).items()},
        )

    def budget_for(self, name: str) -> float:
        return self.budgets.get(name, self.default_budget)

    def record(self, name: str, start: float, txn_id) -> float:
        latency = self.env.now - start
        self.samples.setdefault(name, []).append(latency)
        budget = self.budget_for(name)
        if latency > budget:
            self.violations[name] = self.violations.get(name, 0) + 1
            log.error(
                "[t=%.2f] LATENCY BUDGET EXCEEDED pipeline=%s txn=%s latency=%.2f budget=%.2f (+%.2f)",
                self.env.now,
                name,
                txn_id,
                latency,
                budget,
                latency - budget,
            )
        return latency

    def timed(self, name: str, gen, txn_id):
        """Run generator `gen` and record its latency, including when it raises or is interrupted."""
        start = self.env.now
        try:
            result = yield from gen
        except Exception:  # simpy.Interrupt included; GeneratorExit at teardown is not recorded
            self.record(name, start, txn_id)
            raise
        self.record(name, start, txn_id)
        return result

    # ---- approach A ----------------------------------------------------------------------
    def wrap(self, name: str, fn):
        """Return fn(txn) wrapped so every call is timed under `name`."""

        def wrapper(txn):
            return self.timed(name, fn(txn), txn.id)

        return wrapper

    def report(self, title: str = "") -> None:
        if title:
            print(f"\n== {title} ==")
        print(f"{'pipeline':<28}{'n':>5}{'avg':>8}{'p95':>8}{'max':>8}{'budget':>8}{'viol':>6}")
        for name, xs in sorted(self.samples.items()):
            p95 = sorted(xs)[max(0, math.ceil(len(xs) * 0.95) - 1)]
            print(
                f"{name:<28}{len(xs):>5}{statistics.mean(xs):>8.2f}{p95:>8.2f}"
                f"{max(xs):>8.2f}{self.budget_for(name):>8.1f}{self.violations.get(name, 0):>6}"
            )


# ---- approach B --------------------------------------------------------------------------
class LatencyCheckedMixin:
    """Put in front of an IP block class: class X(LatencyCheckedMixin, IPBlock).

    Overrides the IP block's per-transaction hook (_process) and per-stage hook (_run_stage).
    Stage latency is only checked for stages that have a budget ("ip.pipeline.stage").
    """

    def __init__(self, *args, monitor: LatencyMonitor, **kwargs):
        super().__init__(*args, **kwargs)
        self.monitor = monitor

    def _process(self, pname, txn):
        return self.monitor.timed(f"{self.name}.{pname}", super()._process(pname, txn), txn.id)

    def _run_stage(self, pname, stage, txn):
        gen = super()._run_stage(pname, stage, txn)
        key = f"{self.name}.{pname}.{stage.name}"
        return self.monitor.timed(key, gen, txn.id) if key in self.monitor.budgets else gen


# ---- approach C --------------------------------------------------------------------------
class _ProbedStore(simpy.Store):
    def __init__(self, env, on_put, **kwargs):
        super().__init__(env, **kwargs)
        self._on_put = on_put

    def put(self, item):
        self._on_put(item)
        return super().put(item)


class LatencyProbe:
    """Drop-in replacements for a pipeline's input and output simpy.Store."""

    def __init__(self, monitor: LatencyMonitor, name: str):
        self.monitor, self.name = monitor, name
        self.t_in: dict[int, float] = {}  # id(txn) -> ingress time; leftovers = txns still in flight

    def ingress(self, env, **kwargs) -> simpy.Store:
        return _ProbedStore(env, lambda txn: self.t_in.__setitem__(id(txn), env.now), **kwargs)

    def egress(self, env, **kwargs) -> simpy.Store:
        def check(txn):
            start = self.t_in.pop(id(txn), None)
            if start is not None:
                self.monitor.record(self.name, start, getattr(txn, "id", txn))

        return _ProbedStore(env, check, **kwargs)
