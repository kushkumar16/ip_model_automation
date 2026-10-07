"""Loop-style model with latency checking, approach E: a decorator on each loop method.

This mirrors the three loop shapes in src/ip_model_automation:

  WorkerIP     one loop per pipeline, blocks on Store.get(), handles one item at a time
               (like ArbitrationIP.issue_pipeline / policy_update, CompletionIP accept)
  SchedulerIP  waits on a wake-up event only when nothing is pending, otherwise falls
               straight through (like ArbitrationIP.arbiter_main, completion_scheduler)
  BridgeIP     polls every cycle with timeout(1) (like StoragePipelineSubsystem.dispatch_bridge)

All three keep their FSM state in self.fsm_state[fsm], like the real IPs.
Traffic: dma.read / dma.write -> WorkerIP; sched -> SchedulerIP -> BridgeIP.

Run: python examples/soc_latency/loop_model_approach_e.py
"""

from __future__ import annotations

import logging
import random
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import simpy

from latency_trace import MonitoredEnvironment, fsm_idle, latency_traced

SEED = 11
SIM_TIME = 3000
TXNS = 60


@dataclass
class Txn:
    id: int


def jitter(d: float) -> float:
    return d * random.uniform(0.8, 1.4)


class WorkerIP:
    def __init__(self, env: simpy.Environment, name: str, pipelines: dict[str, list[float]]):
        self.env, self.name = env, name
        self.fsm_state: dict[str, str] = {}
        self.in_q = {p: simpy.Store(env) for p in pipelines}
        self.done: dict[str, list[int]] = {p: [] for p in pipelines}  # ids of completed txns
        for pname, stages in pipelines.items():
            env.process(self.worker(pname, stages))

    def submit(self, pname: str, txn: Txn):
        return self.in_q[pname].put(txn)

    @latency_traced("{self.name}.{pname}")
    def worker(self, pname: str, stages: list[float]):
        while True:
            self.fsm_state[pname] = "IDLE"
            txn = yield self.in_q[pname].get()
            self.fsm_state[pname] = "BUSY"
            for d in stages:
                yield self.env.timeout(jitter(d))
            self.done[pname].append(txn.id)


class SchedulerIP:
    def __init__(self, env: simpy.Environment, name: str, downstream: BridgeIP):
        self.env, self.name, self.downstream = env, name, downstream
        self.fsm_state: dict[str, str] = {}
        self.pending: deque[Txn] = deque()
        self._wake = None
        env.process(self.scheduler())

    def submit(self, txn: Txn) -> None:
        self.pending.append(txn)
        if self._wake is not None and not self._wake.triggered:
            self._wake.succeed()

    @latency_traced("{self.name}.scheduler", boundary=fsm_idle("scheduler", "IDLE"))
    def scheduler(self):
        while True:
            self.fsm_state["scheduler"] = "IDLE"
            if not self.pending:  # only waits when idle; falls through when work is queued
                self._wake = self.env.event()
                yield self._wake
                self._wake = None
            txn = self.pending.popleft()
            self.fsm_state["scheduler"] = "SELECT"
            yield self.env.timeout(jitter(2))
            self.fsm_state["scheduler"] = "SERVE"
            yield self.env.timeout(jitter(4))
            self.downstream.mailbox.append(txn)


class BridgeIP:
    def __init__(self, env: simpy.Environment, name: str):
        self.env, self.name = env, name
        self.fsm_state: dict[str, str] = {}
        self.mailbox: deque[Txn] = deque()
        self.done: list[int] = []
        env.process(self.bridge())

    @latency_traced("{self.name}.bridge", boundary=fsm_idle("bridge", "POLL"))
    def bridge(self):
        while True:
            self.fsm_state["bridge"] = "POLL"
            yield self.env.timeout(1)
            if not self.mailbox:
                continue
            txn = self.mailbox.popleft()
            self.fsm_state["bridge"] = "FORWARD"
            yield self.env.timeout(jitter(3))
            self.done.append(txn.id)


def traffic(env: simpy.Environment, submit, period: float):
    for i in range(TXNS):
        submit(Txn(i))
        yield env.timeout(random.expovariate(1 / period))


def build(env: simpy.Environment):
    dma = WorkerIP(env, "dma", {"read": [1, 2, 1], "write": [2, 3, 1]})
    bridge = BridgeIP(env, "bridge")
    sched = SchedulerIP(env, "sched", bridge)
    env.process(traffic(env, lambda t: dma.submit("read", t), 6))
    env.process(traffic(env, lambda t: dma.submit("write", t), 8))
    env.process(traffic(env, sched.submit, 7))
    return {"dma": dma, "sched": sched, "bridge": bridge}


def completed(blocks) -> dict:
    return {"dma": dict(blocks["dma"].done), "bridge": blocks["bridge"].done}


def run():
    random.seed(SEED)
    env = MonitoredEnvironment(budgets=Path(__file__).with_name("loop_budgets.yaml"))
    blocks = build(env)
    env.run(until=SIM_TIME)
    return blocks, env.monitor


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    blocks, monitor = run()
    print({ip: len(ids) for ip, ids in blocks["dma"].done.items()} | {"bridge": len(blocks["bridge"].done)})
    monitor.report()
