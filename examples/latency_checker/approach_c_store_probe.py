"""Approach C: probe the pipeline's input and output Stores.

Here the pipelines are long-running worker loops (`while True: txn = yield in_q.get()`),
so there is no per-transaction generator to wrap. Instead, the Stores at the pipeline's
edges are swapped for ProbedStore: the ingress store stamps each txn on put, the egress
store checks the latency on put. The worker code is untouched.

Run: python examples/latency_checker/approach_c_store_probe.py
"""

from __future__ import annotations

import random

import simpy

from monitor import CAPACITY, SCENARIO, SEED, LatencyMonitor, arrivals, jitter, setup_logging


class LatencyProbe:
    """Pairs an ingress and egress store for one pipeline."""

    def __init__(self, monitor: LatencyMonitor, name: str):
        self.monitor, self.name = monitor, name
        self.t_in: dict[int, float] = {}  # id(txn) -> ingress time

    def ingress(self, env, **kw) -> simpy.Store:
        return ProbedStore(env, on_put=lambda item: self.t_in.__setitem__(id(item), env.now), **kw)

    def egress(self, env, **kw) -> simpy.Store:
        def check(item):
            start = self.t_in.pop(id(item), None)
            if start is not None:
                self.monitor.record(self.name, start, getattr(item, "id", item))

        return ProbedStore(env, on_put=check, **kw)


class ProbedStore(simpy.Store):
    """A simpy.Store that calls on_put(item) whenever a put request is issued."""

    def __init__(self, env, on_put, **kw):
        super().__init__(env, **kw)
        self._on_put = on_put

    def put(self, item):
        self._on_put(item)
        return super().put(item)


# ---- existing pipeline code: untouched ----------------------------------------------------
def worker(env, stage_delays, shared: simpy.Resource, in_q: simpy.Store, out_q: simpy.Store):
    while True:
        txn = yield in_q.get()
        env.process(_handle(env, stage_delays, shared, txn, out_q))


def _handle(env, stage_delays, shared, txn, out_q):
    for d in stage_delays:
        with shared.request() as req:
            yield req
            yield env.timeout(jitter(d))
    yield out_q.put(txn)


def drain(out_q):
    while True:
        yield out_q.get()


def run() -> LatencyMonitor:
    random.seed(SEED)
    env = simpy.Environment()
    mon = LatencyMonitor(env)
    for ip, pipes in SCENARIO.items():
        shared = simpy.Resource(env, capacity=CAPACITY[ip])
        for pname, (delays, period) in pipes.items():
            probe = LatencyProbe(mon, f"{ip}.{pname}")
            # CHANGE: was `in_q, out_q = simpy.Store(env), simpy.Store(env)`
            in_q, out_q = probe.ingress(env), probe.egress(env)
            env.process(worker(env, delays, shared, in_q, out_q))
            env.process(drain(out_q))
            env.process(arrivals(env, in_q.put, period))
    env.run(until=2000)  # worker loops never finish, so bound the run
    return mon


if __name__ == "__main__":
    setup_logging()
    run().report("Approach C: Store probes")
