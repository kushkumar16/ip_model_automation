"""Approach A: wrap each pipeline's per-transaction generator when it is registered.

Pipeline bodies are not edited. The only change is that IPBlock.add_pipeline() routes the
pipeline through wrap_with_latency_check().

Run: python examples/latency_checker/approach_a_wrapper.py
"""

from __future__ import annotations

import functools
import random

import simpy

from monitor import CAPACITY, SCENARIO, SEED, LatencyMonitor, arrivals, jitter, setup_logging


def wrap_with_latency_check(monitor: LatencyMonitor, name: str, fn):
    """Wrap a generator function fn(txn, ...) so its end-to-end latency is checked."""

    @functools.wraps(fn)
    def wrapper(txn, *args, **kwargs):
        start = monitor.env.now
        try:
            result = yield from fn(txn, *args, **kwargs)
        finally:  # also records txns that raise or get interrupted
            monitor.record(name, start, getattr(txn, "id", txn))
        return result

    return wrapper


class IPBlock:
    """An IP block owning several pipelines. add_pipeline() is the single hook point."""

    def __init__(self, env, name, monitor: LatencyMonitor):
        self.env, self.name, self.monitor = env, name, monitor
        self.pipelines = {}

    def add_pipeline(self, pname: str, fn) -> None:
        # CHANGE (the only one): was `self.pipelines[pname] = fn`
        self.pipelines[pname] = wrap_with_latency_check(self.monitor, f"{self.name}.{pname}", fn)

    def submit(self, pname: str, txn):
        return self.env.process(self.pipelines[pname](txn))


# ---- existing pipeline code: untouched ----------------------------------------------------
def make_stage_pipeline(env, stage_delays, shared: simpy.Resource):
    def pipeline(txn):
        for d in stage_delays:
            with shared.request() as req:
                yield req
                yield env.timeout(jitter(d))

    return pipeline


def run() -> LatencyMonitor:
    random.seed(SEED)
    env = simpy.Environment()
    mon = LatencyMonitor(env)
    for ip, pipes in SCENARIO.items():
        block = IPBlock(env, ip, mon)
        shared = simpy.Resource(env, capacity=CAPACITY[ip])
        for pname, (delays, period) in pipes.items():
            block.add_pipeline(pname, make_stage_pipeline(env, delays, shared))
            env.process(arrivals(env, functools.partial(block.submit, pname), period))
    env.run()
    return mon


if __name__ == "__main__":
    setup_logging()
    run().report("Approach A: wrapper at registration")
