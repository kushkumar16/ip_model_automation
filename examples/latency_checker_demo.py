"""SimPy demo: IP blocks, each with several pipelines, and a per-pipeline latency checker.

Approach used here (least intrusive): the checker wraps a pipeline's *per-transaction
generator* at registration time. Pipeline bodies are not edited at all; the only change
is that IPBlock.add_pipeline() routes through LatencyMonitor.wrap().

Run: python examples/latency_checker_demo.py
"""

from __future__ import annotations

import functools
import logging
import random
import statistics
from dataclasses import dataclass, field

import simpy

log = logging.getLogger("latency")


@dataclass
class LatencyMonitor:
    """Records per-pipeline latency and logs an ERROR when a budget is exceeded."""

    env: simpy.Environment
    default_budget: float = float("inf")
    budgets: dict[str, float] = field(default_factory=dict)  # "ip.pipeline" -> budget
    samples: dict[str, list[float]] = field(default_factory=dict)
    violations: dict[str, int] = field(default_factory=dict)

    def budget_for(self, name: str) -> float:
        return self.budgets.get(name, self.default_budget)

    def record(self, name: str, start: float, txn_id) -> None:
        latency = self.env.now - start
        self.samples.setdefault(name, []).append(latency)
        budget = self.budget_for(name)
        if latency > budget:
            self.violations[name] = self.violations.get(name, 0) + 1
            log.error(
                "[t=%.2f] LATENCY BUDGET EXCEEDED pipeline=%s txn=%s latency=%.2f budget=%.2f (+%.2f)",
                self.env.now, name, txn_id, latency, budget, latency - budget,
            )

    def wrap(self, name: str, fn):
        """Wrap a generator function fn(txn, ...) so its end-to-end latency is checked."""

        @functools.wraps(fn)
        def wrapper(txn, *args, **kwargs):
            start = self.env.now
            try:
                result = yield from fn(txn, *args, **kwargs)
            finally:  # also record txns that raise / get interrupted
                self.record(name, start, getattr(txn, "id", txn))
            return result

        return wrapper

    def report(self) -> None:
        print(f"{'pipeline':<22}{'n':>5}{'avg':>8}{'p95':>8}{'max':>8}{'budget':>8}{'viol':>6}")
        for name, xs in sorted(self.samples.items()):
            p95 = sorted(xs)[max(0, int(len(xs) * 0.95) - 1)]
            b = self.budget_for(name)
            print(f"{name:<22}{len(xs):>5}{statistics.mean(xs):>8.2f}{p95:>8.2f}"
                  f"{max(xs):>8.2f}{b:>8.1f}{self.violations.get(name, 0):>6}")


class IPBlock:
    """An IP block owning several pipelines. add_pipeline() is the single hook point."""

    def __init__(self, env, name, monitor: LatencyMonitor):
        self.env, self.name, self.monitor = env, name, monitor
        self.pipelines: dict[str, callable] = {}

    def add_pipeline(self, pname: str, fn) -> None:
        self.pipelines[pname] = self.monitor.wrap(f"{self.name}.{pname}", fn)  # <- only change

    def submit(self, pname: str, txn):
        return self.env.process(self.pipelines[pname](txn))


# ---- existing pipelines: untouched by the latency feature -------------------------------
def make_stage_pipeline(env, stage_delays, contention: simpy.Resource):
    def pipeline(txn):
        for d in stage_delays:
            with contention.request() as req:  # shared resource -> queueing delay
                yield req
                yield env.timeout(d * random.uniform(0.8, 1.4))

    return pipeline


@dataclass
class Txn:
    id: int


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    random.seed(1)
    env = simpy.Environment()
    mon = LatencyMonitor(env, default_budget=10.0, budgets={"dma.write": 6.0, "crypto.encrypt": 12.0})

    dma = IPBlock(env, "dma", mon)
    crypto = IPBlock(env, "crypto", mon)
    dma_bus = simpy.Resource(env, capacity=2)
    cry_core = simpy.Resource(env, capacity=3)

    dma.add_pipeline("read", make_stage_pipeline(env, [1, 2, 1], dma_bus))
    dma.add_pipeline("write", make_stage_pipeline(env, [2, 2, 2], dma_bus))
    crypto.add_pipeline("encrypt", make_stage_pipeline(env, [3, 4], cry_core))
    crypto.add_pipeline("hash", make_stage_pipeline(env, [2, 2], cry_core))

    def traffic(block, pname, period, n):
        for i in range(n):
            block.submit(pname, Txn(i))
            yield env.timeout(random.expovariate(1 / period))

    for blk, p, period in [(dma, "read", 8), (dma, "write", 10), (crypto, "encrypt", 8), (crypto, "hash", 10)]:
        env.process(traffic(blk, p, period, 40))
    env.run()
    mon.report()


if __name__ == "__main__":
    main()
