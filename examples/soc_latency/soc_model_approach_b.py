"""SoC model with latency checking, approach B: a mixin overrides the IP block's _process/_run_stage hooks.

Each IP block owns shared resources (bus, cores, ...) and a set of pipelines. A pipeline is
an ordered list of stages; each stage holds one of the IP's resources for a jittered delay,
so pipelines in the same IP contend with each other.

Per pipeline:  submit() -> in_q -> _dispatcher -> _process (stages) -> out_q -> _drain

The soc_model_approach_*.py files are copies of this file with the latency checker
applied; LATENCY_GUIDE.md shows the exact diff for each.

Run: python examples/soc_latency/soc_model_approach_b.py
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

import simpy

from latency_monitor import LatencyCheckedMixin, LatencyMonitor

SEED = 7
SIM_TIME = 2000
TXNS_PER_PIPELINE = 50


@dataclass
class Txn:
    id: int


@dataclass
class Stage:
    name: str
    delay: float
    resource: str  # key into the owning IP block's resources


IP_SPECS = {
    "dma": {
        "resources": {"bus": 2},
        "pipelines": {
            "read": [Stage("addr", 1, "bus"), Stage("fetch", 2, "bus"), Stage("resp", 1, "bus")],
            "write": [Stage("addr", 1, "bus"), Stage("data", 3, "bus"), Stage("ack", 1, "bus")],
        },
    },
    "crypto": {
        "resources": {"aes": 2, "sha": 1},
        "pipelines": {
            "encrypt": [Stage("key", 1, "aes"), Stage("rounds", 4, "aes")],
            "decrypt": [Stage("key", 1, "aes"), Stage("rounds", 4, "aes")],
            "hash": [Stage("pad", 1, "sha"), Stage("digest", 3, "sha")],
        },
    },
    "codec": {
        "resources": {"dsp": 1, "mem": 2},
        "pipelines": {
            "encode": [Stage("load", 1, "mem"), Stage("transform", 3, "dsp"), Stage("store", 1, "mem")],
            "decode": [Stage("load", 1, "mem"), Stage("inverse", 2, "dsp"), Stage("store", 1, "mem")],
        },
    },
}

# Mean inter-arrival time per pipeline.
TRAFFIC = {
    "dma.read": 8,
    "dma.write": 10,
    "crypto.encrypt": 9,
    "crypto.decrypt": 12,
    "crypto.hash": 8,
    "codec.encode": 10,
    "codec.decode": 9,
}


class IPBlock:
    def __init__(self, env: simpy.Environment, name: str, resources: dict[str, int]):
        self.env, self.name = env, name
        self.resources = {k: simpy.Resource(env, capacity=c) for k, c in resources.items()}
        self.pipelines: dict[str, list[Stage]] = {}
        self.in_q: dict[str, simpy.Store] = {}
        self.out_q: dict[str, simpy.Store] = {}
        self.completed: dict[str, int] = {}

    def add_pipeline(self, pname: str, stages: list[Stage]) -> None:
        self.pipelines[pname] = stages
        self.in_q[pname] = simpy.Store(self.env)
        self.out_q[pname] = simpy.Store(self.env)
        self.env.process(self._dispatcher(pname))
        self.env.process(self._drain(pname))

    def submit(self, pname: str, txn: Txn):
        return self.in_q[pname].put(txn)

    def _dispatcher(self, pname: str):
        while True:
            txn = yield self.in_q[pname].get()
            self.env.process(self._process(pname, txn))

    def _process(self, pname: str, txn: Txn):
        for stage in self.pipelines[pname]:
            yield from self._run_stage(pname, stage, txn)
        yield self.out_q[pname].put(txn)

    def _run_stage(self, pname: str, stage: Stage, txn: Txn):
        with self.resources[stage.resource].request() as req:
            yield req
            yield self.env.timeout(stage.delay * random.uniform(0.8, 1.4))

    def _drain(self, pname: str):
        while True:
            yield self.out_q[pname].get()
            self.completed[pname] = self.completed.get(pname, 0) + 1


class LatencyCheckedIPBlock(LatencyCheckedMixin, IPBlock):
    pass


def traffic(env: simpy.Environment, block: IPBlock, pname: str, period: float):
    for i in range(TXNS_PER_PIPELINE):
        block.submit(pname, Txn(i))
        yield env.timeout(random.expovariate(1 / period))


def build(env: simpy.Environment, monitor: LatencyMonitor) -> dict[str, IPBlock]:
    blocks = {}
    for ip, spec in IP_SPECS.items():
        block = LatencyCheckedIPBlock(env, ip, spec["resources"], monitor=monitor)
        for pname, stages in spec["pipelines"].items():
            block.add_pipeline(pname, stages)
        blocks[ip] = block
    for full_name, period in TRAFFIC.items():
        ip, pname = full_name.split(".")
        env.process(traffic(env, blocks[ip], pname, period))
    return blocks


def run():
    random.seed(SEED)
    env = simpy.Environment()
    monitor = LatencyMonitor.from_yaml(env)
    blocks = build(env, monitor)
    env.run(until=SIM_TIME)
    return blocks, monitor


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    blocks, monitor = run()
    for ip, block in blocks.items():
        print(ip, block.completed)
    monitor.report()
