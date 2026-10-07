"""Approach B: a Pipeline base class whose template run() does the timing.

Each pipeline subclasses Pipeline and implements stages(txn). The base class times the
whole transaction, and stage() optionally times individual stages too, so per-stage
budgets ("dma.write.s1") work with no extra code.

Run: python examples/latency_checker/approach_b_base_class.py
"""

from __future__ import annotations

import random

import simpy

from monitor import CAPACITY, SCENARIO, SEED, LatencyMonitor, arrivals, jitter, setup_logging


class Pipeline:
    """Base class: subclasses implement stages(txn); timing lives here only."""

    def __init__(self, env: simpy.Environment, ip: str, name: str, monitor: LatencyMonitor):
        self.env, self.monitor = env, monitor
        self.full_name = f"{ip}.{name}"

    def submit(self, txn):
        return self.env.process(self.run(txn))

    def run(self, txn):  # template method: do not override
        start = self.env.now
        try:
            yield from self.stages(txn)
        finally:
            self.monitor.record(self.full_name, start, txn.id)

    def stage(self, sname: str, gen, txn):
        """Optional: time one stage. Only checked against a budget if one is configured."""
        start = self.env.now
        yield from gen
        key = f"{self.full_name}.{sname}"
        if key in self.monitor.budgets:
            self.monitor.record(key, start, txn.id)

    def stages(self, txn):
        raise NotImplementedError


# ---- pipelines: rewritten once to subclass Pipeline ---------------------------------------
class StagePipeline(Pipeline):
    def __init__(self, env, ip, name, monitor, stage_delays, shared: simpy.Resource):
        super().__init__(env, ip, name, monitor)
        self.stage_delays, self.shared = stage_delays, shared

    def _one_stage(self, d):
        with self.shared.request() as req:
            yield req
            yield self.env.timeout(jitter(d))

    def stages(self, txn):
        for i, d in enumerate(self.stage_delays):
            yield from self.stage(f"s{i}", self._one_stage(d), txn)


def run() -> LatencyMonitor:
    random.seed(SEED)
    env = simpy.Environment()
    mon = LatencyMonitor(env)
    mon.budgets["dma.write.s0"] = 2.5  # per-stage budget, only possible with this approach
    for ip, pipes in SCENARIO.items():
        shared = simpy.Resource(env, capacity=CAPACITY[ip])
        for pname, (delays, period) in pipes.items():
            p = StagePipeline(env, ip, pname, mon, delays, shared)
            env.process(arrivals(env, p.submit, period))
    env.run()
    return mon


if __name__ == "__main__":
    setup_logging()
    run().report("Approach B: Pipeline base class")
