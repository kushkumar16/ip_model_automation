"""Approach D: the transaction carries its own ingress timestamp; a common sink checks it.

Ingress stamps txn.t_in once. Whatever stage finishes the transaction hands it to
CompletionSink.complete(), which computes and checks the latency. If your design already
has one completion IP / egress point, that is the only place the check is added.

Run: python examples/latency_checker/approach_d_txn_timestamp.py
"""

from __future__ import annotations

import functools
import random

import simpy

from monitor import CAPACITY, SCENARIO, SEED, LatencyMonitor, Txn, arrivals, jitter, setup_logging


class CompletionSink:
    """The common egress point: checks latency from txn.t_in."""

    def __init__(self, monitor: LatencyMonitor):
        self.monitor = monitor

    def complete(self, pipeline_name: str, txn: Txn) -> None:
        if txn.t_in is not None:
            self.monitor.record(pipeline_name, txn.t_in, txn.id)


def submit(env, pipeline, txn: Txn):
    txn.t_in = env.now  # CHANGE 1: stamp at ingress
    return env.process(pipeline(txn))


def make_stage_pipeline(env, name, stage_delays, shared: simpy.Resource, sink: CompletionSink):
    def pipeline(txn):
        for d in stage_delays:
            with shared.request() as req:
                yield req
                yield env.timeout(jitter(d))
        sink.complete(name, txn)  # CHANGE 2: hand to the common completion point

    return pipeline


def run() -> LatencyMonitor:
    random.seed(SEED)
    env = simpy.Environment()
    mon = LatencyMonitor(env)
    sink = CompletionSink(mon)
    for ip, pipes in SCENARIO.items():
        shared = simpy.Resource(env, capacity=CAPACITY[ip])
        for pname, (delays, period) in pipes.items():
            p = make_stage_pipeline(env, f"{ip}.{pname}", delays, shared, sink)
            env.process(arrivals(env, functools.partial(submit, env, p), period))
    env.run()
    return mon


if __name__ == "__main__":
    setup_logging()
    run().report("Approach D: timestamp on the transaction")
