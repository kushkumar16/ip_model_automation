"""Packet model with latency checking: approach D, timestamp carried on the packet.

  ParallelIP  shape 1, a SimPy process per packet: a dispatcher starts handle_packet(pkt)
              for every packet, so many packets are in flight and share `engines`.
  SerialIP    shape 2, a `while True` loop: rx_loop() handles one packet at a time;
              packets queue in rx_q while it is busy.

Both IPs: receive() -> rx_q -> (handling) -> tx_q -> drain() -> sent.
The packet_model_*.py files are copies with latency checking applied; PACKET_LATENCY.md
shows the exact diff for each approach.

Run: python examples/soc_latency/packet_model_approach_d.py
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

import simpy

from latency_trace import MonitoredEnvironment

SEED = 5
SIM_TIME = 5000
PACKETS = 80
SIZES = (64, 512, 1500)  # bytes


@dataclass
class Packet:
    id: int
    size: int
    t_in: float = 0.0


def payload_time(pkt: Packet) -> float:
    return pkt.size / 512 * random.uniform(0.9, 1.3)


class ParallelIP:
    """Shape 1: one process per packet."""

    def __init__(self, env: simpy.Environment, name: str, engines: int = 2):
        self.env, self.name = env, name
        self.engines = simpy.Resource(env, capacity=engines)
        self.rx_q = simpy.Store(env)
        self.tx_q = simpy.Store(env)
        self.sent: list[int] = []
        env.process(self.dispatcher())
        env.process(self.drain())

    def receive(self, pkt: Packet):
        pkt.t_in = self.env.now
        return self.rx_q.put(pkt)

    def dispatcher(self):
        while True:
            pkt = yield self.rx_q.get()
            self.env.process(self.handle_packet(pkt))

    def handle_packet(self, pkt: Packet):
        with self.engines.request() as req:
            yield req
            yield self.env.timeout(1)  # parse header
            yield self.env.timeout(payload_time(pkt))
        yield self.tx_q.put(pkt)

    def drain(self):
        while True:
            pkt = yield self.tx_q.get()
            self.sent.append(pkt.id)
            self.env.monitor.record(f"{self.name}.e2e", pkt.t_in, pkt.id)


class SerialIP:
    """Shape 2: one long-lived `while True` loop."""

    def __init__(self, env: simpy.Environment, name: str):
        self.env, self.name = env, name
        self.fsm_state: dict[str, str] = {}
        self.rx_q = simpy.Store(env)
        self.tx_q = simpy.Store(env)
        self.sent: list[int] = []
        env.process(self.rx_loop())
        env.process(self.drain())

    def receive(self, pkt: Packet):
        pkt.t_in = self.env.now
        return self.rx_q.put(pkt)

    def rx_loop(self):
        while True:
            self.fsm_state["rx"] = "IDLE"
            pkt = yield self.rx_q.get()
            self.fsm_state["rx"] = "PARSE"
            yield self.env.timeout(1)
            self.fsm_state["rx"] = "PAYLOAD"
            yield self.env.timeout(payload_time(pkt))
            yield self.tx_q.put(pkt)

    def drain(self):
        while True:
            pkt = yield self.tx_q.get()
            self.sent.append(pkt.id)
            self.env.monitor.record(f"{self.name}.e2e", pkt.t_in, pkt.id)


def traffic(env: simpy.Environment, ip, period: float):
    for i in range(PACKETS):
        ip.receive(Packet(i, random.choice(SIZES)))
        yield env.timeout(random.expovariate(1 / period))


def build(env: simpy.Environment):
    par = ParallelIP(env, "par")
    ser = SerialIP(env, "ser")
    env.process(traffic(env, par, 2))
    env.process(traffic(env, ser, 4))
    return {"par": par, "ser": ser}


def run():
    random.seed(SEED)
    env = MonitoredEnvironment()
    ips = build(env)
    env.run(until=SIM_TIME)
    return ips, env.monitor


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ips, monitor = run()
    print({name: len(ip.sent) for name, ip in ips.items()})
    monitor.report()
