"""Shared pieces for every latency-checker approach: the monitor and the demo scenario.

Every approach feeds the same LatencyMonitor, so they all log the same ERROR line and
print the same report. The approaches differ only in *where* the start/end timestamps
are taken.
"""

from __future__ import annotations

import logging
import random
import statistics
from dataclasses import dataclass, field

import simpy

log = logging.getLogger("latency")

# Budgets are keyed "<ip>.<pipeline>" (or "<ip>.<pipeline>.<stage>" for approach B).
# Anything not listed falls back to DEFAULT_BUDGET.
DEFAULT_BUDGET = 10.0
BUDGETS = {"dma.write": 6.0, "crypto.encrypt": 12.0}

# The demo scenario every approach models: ip -> pipeline -> (stage delays, arrival period).
SCENARIO = {
    "dma": {"read": ([1, 2, 1], 8), "write": ([2, 2, 2], 10)},
    "crypto": {"encrypt": ([3, 4], 8), "hash": ([2, 2], 10)},
}
CAPACITY = {"dma": 2, "crypto": 3}  # shared resource per IP -> queueing delay
TXNS_PER_PIPELINE = 40
SEED = 1


@dataclass
class Txn:
    id: int
    t_in: float | None = None  # only used by approach D


@dataclass
class LatencyMonitor:
    """Records per-pipeline latency and logs an ERROR when a budget is exceeded."""

    env: simpy.Environment
    default_budget: float = DEFAULT_BUDGET
    budgets: dict[str, float] = field(default_factory=lambda: dict(BUDGETS))
    samples: dict[str, list[float]] = field(default_factory=dict)
    violations: dict[str, int] = field(default_factory=dict)

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

    def report(self, title: str = "") -> None:
        if title:
            print(f"\n== {title} ==")
        print(f"{'pipeline':<26}{'n':>5}{'avg':>8}{'p95':>8}{'max':>8}{'budget':>8}{'viol':>6}")
        for name, xs in sorted(self.samples.items()):
            p95 = sorted(xs)[max(0, int(len(xs) * 0.95) - 1)]
            print(
                f"{name:<26}{len(xs):>5}{statistics.mean(xs):>8.2f}{p95:>8.2f}"
                f"{max(xs):>8.2f}{self.budget_for(name):>8.1f}{self.violations.get(name, 0):>6}"
            )


def jitter(d: float) -> float:
    return d * random.uniform(0.8, 1.4)


def arrivals(env, submit, period: float, n: int = TXNS_PER_PIPELINE):
    """Traffic generator: calls submit(Txn) n times with exponential inter-arrival gaps."""
    for i in range(n):
        submit(Txn(i))
        yield env.timeout(random.expovariate(1 / period))


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
