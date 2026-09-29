#!/usr/bin/env python3
"""Backpressure study: a request/response pipeline with bounded FIFOs, in SimPy.

Topology (time unit = 1 cycle)::

    Requester --> [req FIFO] --> Responder x N --> [resp FIFO] --> Response handler x M
        ^                                                                    |
        +---------------- outstanding-credit return (optional) --------------+

Backpressure propagates right-to-left: a slow response handler fills the
response FIFO, which stalls the responder workers, which stops draining the
request FIFO, which stalls the requester. The model measures where time is
lost at each stage so you can see which knob actually limits throughput.

Knobs (see ``Config``):

* issue rate / pattern        -- ``issue_interval``, ``issue_pattern`` (const, poisson, burst)
* FIFO sizes                  -- ``req_fifo_depth``, ``resp_fifo_depth``
* responder service latency   -- ``service_latency``, ``service_dist``, ``responder_workers``
* response handler latency    -- ``handler_latency``, ``handler_dist``, ``handler_workers``
* flow control                -- ``flow_control`` = ready (instant full signal) or
                                 credit (credits return ``credit_return_latency`` cycles after a pop)
* end-to-end outstanding cap  -- ``max_outstanding`` (0 = unlimited)
* overflow policy             -- ``on_full`` = block (stall the source) or drop (count a loss)

Usage::

    python studies/backpressure/backpressure_sim.py                      # one run, default config
    python studies/backpressure/backpressure_sim.py --set handler_latency=12
    python studies/backpressure/backpressure_sim.py \\
        --sweep resp_fifo_depth=1,2,4,8,16 --sweep handler_latency=8,12 --csv out.csv
"""

from __future__ import annotations

import argparse
import csv
import itertools
import random
import sys
from dataclasses import asdict, dataclass, fields, replace
from typing import Dict, List, Optional

import simpy


@dataclass(frozen=True)
class Config:
    # Request side
    issue_interval: float = 10.0  # mean cycles between requests (1 / issue rate)
    issue_pattern: str = "const"  # const | poisson | burst
    burst_len: int = 8  # requests per burst (burst pattern), issued back-to-back
    burst_gap: float = 1.0  # cycles between requests inside a burst
    max_outstanding: int = 0  # requests issued but not yet handled; 0 = unlimited

    # FIFOs and flow control
    req_fifo_depth: int = 4
    resp_fifo_depth: int = 4
    flow_control: str = "ready"  # ready | credit (req FIFO only)
    credit_return_latency: float = 0.0  # cycles from req-FIFO pop to credit seen by requester
    on_full: str = "block"  # block | drop (requester side)

    # Responder (processes a request, produces a response)
    responder_workers: int = 1  # parallel/pipelined requests in service
    service_latency: float = 8.0
    service_dist: str = "const"  # const | exp | uniform | bimodal

    # Response handler (consumes responses)
    handler_workers: int = 1
    handler_latency: float = 6.0
    handler_dist: str = "const"

    # Run control
    sim_time: float = 200_000.0
    warmup: float = 5_000.0
    seed: int = 1


def sample(rng: random.Random, mean: float, dist: str) -> float:
    if mean <= 0:
        return 0.0
    if dist == "const":
        return mean
    if dist == "exp":
        return rng.expovariate(1.0 / mean)
    if dist == "uniform":  # +/-50% around the mean
        return rng.uniform(0.5 * mean, 1.5 * mean)
    if dist == "bimodal":  # 90% fast, 10% at 5.5x -> same mean, heavy tail
        return mean * (5.5 if rng.random() < 0.1 else 0.5)
    raise ValueError(f"unknown distribution {dist!r}")


class TrackedFifo:
    """A bounded simpy.Store that records time-weighted occupancy."""

    def __init__(self, env: simpy.Environment, depth: int, stats_start: float):
        self.env = env
        self.store = simpy.Store(env, capacity=max(1, depth))
        self.stats_start = stats_start
        self._last_t = 0.0
        self._area = 0.0
        self.max_occupancy = 0
        self.full_time = 0.0

    def _account(self) -> None:
        now = self.env.now
        start = max(self._last_t, self.stats_start)
        if now > start:
            occ = len(self.store.items)
            self._area += occ * (now - start)
            if occ >= self.store.capacity:
                self.full_time += now - start
        self._last_t = now

    def put(self, item):
        self._account()
        ev = self.store.put(item)
        ev.callbacks.append(self._after_change)
        return ev

    def get(self):
        self._account()
        ev = self.store.get()
        ev.callbacks.append(self._after_change)
        return ev

    def _after_change(self, _ev) -> None:
        self._account()
        if self.env.now >= self.stats_start:
            self.max_occupancy = max(self.max_occupancy, len(self.store.items))

    def is_full(self) -> bool:
        return len(self.store.items) >= self.store.capacity

    def finish(self) -> None:
        self._account()

    def avg_occupancy(self, window: float) -> float:
        return self._area / window if window > 0 else 0.0


@dataclass
class Request:
    rid: int
    t_issue: float
    t_resp_ready: float = 0.0


class BackpressureModel:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.env = simpy.Environment()
        self.rng = random.Random(cfg.seed)
        self.req_fifo = TrackedFifo(self.env, cfg.req_fifo_depth, cfg.warmup)
        self.resp_fifo = TrackedFifo(self.env, cfg.resp_fifo_depth, cfg.warmup)
        self.req_credits = simpy.Container(self.env, capacity=cfg.req_fifo_depth, init=cfg.req_fifo_depth)
        self.outstanding: Optional[simpy.Container] = (
            simpy.Container(self.env, capacity=cfg.max_outstanding, init=cfg.max_outstanding)
            if cfg.max_outstanding > 0
            else None
        )

        self.issued = 0
        self.dropped = 0
        self.completed = 0
        self.latencies: List[float] = []
        self.handler_wait: List[float] = []  # time a response sat in resp FIFO
        self.req_stall = 0.0  # requester blocked (FIFO full / no credit / outstanding cap)
        self.resp_stall = 0.0  # responder workers blocked on a full resp FIFO (summed over workers)
        self.service_busy = 0.0
        self.handler_busy = 0.0

    # -- helpers -----------------------------------------------------------
    def _in_window(self) -> bool:
        return self.env.now >= self.cfg.warmup

    def _count(self, start: float, attr: str) -> None:
        """Add the part of [start, now) that lies after warmup to counter ``attr``."""
        dur = self.env.now - max(start, self.cfg.warmup)
        if dur > 0:
            setattr(self, attr, getattr(self, attr) + dur)

    def _next_gap(self, idx: int) -> float:
        c = self.cfg
        if c.issue_pattern == "const":
            return c.issue_interval
        if c.issue_pattern == "poisson":
            return self.rng.expovariate(1.0 / c.issue_interval)
        if c.issue_pattern == "burst":
            # Same average rate as const: burst_len requests every burst_len * issue_interval cycles.
            if (idx + 1) % c.burst_len:
                return c.burst_gap
            return max(0.0, c.burst_len * c.issue_interval - (c.burst_len - 1) * c.burst_gap)
        raise ValueError(f"unknown issue_pattern {c.issue_pattern!r}")

    # -- processes ---------------------------------------------------------
    def requester(self):
        c = self.cfg
        idx = 0
        while True:
            t0 = self.env.now
            if self.outstanding is not None:
                yield self.outstanding.get(1)
            req = Request(idx, t0)  # latency is measured from when the request *wanted* to issue
            if c.on_full == "drop":
                full = self.req_credits.level == 0 if c.flow_control == "credit" else self.req_fifo.is_full()
                if full:
                    if self._in_window():
                        self.dropped += 1
                    if self.outstanding is not None:
                        yield self.outstanding.put(1)
                else:
                    yield from self._push(req)
            else:
                yield from self._push(req)
            self._count(t0, "req_stall")
            if self._in_window():
                self.issued += 1
            yield self.env.timeout(self._next_gap(idx))
            idx += 1

    def _push(self, req: Request):
        if self.cfg.flow_control == "credit":
            yield self.req_credits.get(1)
        yield self.req_fifo.put(req)

    def _return_credit(self):
        if self.cfg.credit_return_latency > 0:
            yield self.env.timeout(self.cfg.credit_return_latency)
        yield self.req_credits.put(1)

    def responder(self):
        c = self.cfg
        while True:
            req = yield self.req_fifo.get()
            if c.flow_control == "credit":
                self.env.process(self._return_credit())
            t0 = self.env.now
            yield self.env.timeout(sample(self.rng, c.service_latency, c.service_dist))
            self._count(t0, "service_busy")
            t1 = self.env.now
            req.t_resp_ready = t1
            yield self.resp_fifo.put(req)
            self._count(t1, "resp_stall")

    def handler(self):
        c = self.cfg
        while True:
            req = yield self.resp_fifo.get()
            t0 = self.env.now
            yield self.env.timeout(sample(self.rng, c.handler_latency, c.handler_dist))
            self._count(t0, "handler_busy")
            if self.outstanding is not None:
                yield self.outstanding.put(1)
            if req.t_issue >= c.warmup:
                self.completed += 1
                self.latencies.append(self.env.now - req.t_issue)
                self.handler_wait.append(t0 - req.t_resp_ready)

    # -- run ---------------------------------------------------------------
    def run(self) -> Dict[str, float]:
        c = self.cfg
        self.env.process(self.requester())
        for _ in range(c.responder_workers):
            self.env.process(self.responder())
        for _ in range(c.handler_workers):
            self.env.process(self.handler())
        self.env.run(until=c.sim_time)
        self.req_fifo.finish()
        self.resp_fifo.finish()
        return self._report()

    def _report(self) -> Dict[str, float]:
        c = self.cfg
        window = c.sim_time - c.warmup
        lat = sorted(self.latencies)

        def pct(p: float) -> float:
            return lat[min(len(lat) - 1, int(p * len(lat)))] if lat else float("nan")

        offered = window / c.issue_interval
        service_cap = c.responder_workers / c.service_latency if c.service_latency else float("inf")
        handler_cap = c.handler_workers / c.handler_latency if c.handler_latency else float("inf")
        caps = {"issue": 1.0 / c.issue_interval, "responder": service_cap, "handler": handler_cap}
        if c.flow_control == "credit" and c.credit_return_latency > 0:
            # Each credit makes one round trip: FIFO pop -> credit return -> next push.
            caps["credit_loop"] = c.req_fifo_depth / c.credit_return_latency
        if c.max_outstanding > 0:
            # Little's law with the unloaded round trip as the minimum hold time of a token.
            caps["outstanding_cap"] = c.max_outstanding / max(1e-9, c.service_latency + c.handler_latency)
        return {
            "offered_rate": 1.0 / c.issue_interval,
            "throughput": self.completed / window,
            "static_bottleneck": min(caps, key=caps.get),
            "achieved_vs_offered": self.completed / offered if offered else float("nan"),
            "lat_mean": sum(lat) / len(lat) if lat else float("nan"),
            "lat_p50": pct(0.50),
            "lat_p99": pct(0.99),
            "lat_max": lat[-1] if lat else float("nan"),
            "resp_fifo_wait_mean": sum(self.handler_wait) / len(self.handler_wait) if self.handler_wait else 0.0,
            "req_stall_frac": self.req_stall / window,
            "resp_stall_frac": self.resp_stall / (window * c.responder_workers),
            "responder_util": self.service_busy / (window * c.responder_workers),
            "handler_util": self.handler_busy / (window * c.handler_workers),
            "req_fifo_avg": self.req_fifo.avg_occupancy(window),
            "req_fifo_max": self.req_fifo.max_occupancy,
            "req_fifo_full_frac": self.req_fifo.full_time / window,
            "resp_fifo_avg": self.resp_fifo.avg_occupancy(window),
            "resp_fifo_max": self.resp_fifo.max_occupancy,
            "resp_fifo_full_frac": self.resp_fifo.full_time / window,
            "dropped": self.dropped,
            "drop_frac": self.dropped / self.issued if self.issued else 0.0,
        }


def simulate(cfg: Config) -> Dict[str, float]:
    return BackpressureModel(cfg).run()


# -- CLI -------------------------------------------------------------------
_FIELD_TYPES = {f.name: f.type for f in fields(Config)}


def _coerce(name: str, raw: str):
    if name not in _FIELD_TYPES:
        raise SystemExit(f"unknown parameter {name!r}; choose from: {', '.join(_FIELD_TYPES)}")
    kind = _FIELD_TYPES[name]
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    return raw


def _parse_assign(text: str):
    if "=" not in text:
        raise SystemExit(f"expected name=value, got {text!r}")
    name, value = text.split("=", 1)
    return name.strip(), value


REPORT_COLUMNS = [
    "throughput",
    "achieved_vs_offered",
    "static_bottleneck",
    "lat_mean",
    "lat_p99",
    "req_stall_frac",
    "resp_stall_frac",
    "req_fifo_avg",
    "resp_fifo_avg",
    "resp_fifo_full_frac",
    "drop_frac",
]


def _fmt(v) -> str:
    return f"{v:.4g}" if isinstance(v, float) else str(v)


def _print_table(rows: List[Dict[str, object]], cols: List[str]) -> None:
    widths = {c: max(len(c), *(len(_fmt(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.rjust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(_fmt(r[c]).rjust(widths[c]) for c in cols))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="override a Config field")
    parser.add_argument(
        "--sweep", action="append", default=[], metavar="NAME=V1,V2,...", help="sweep a field (repeat for a grid)"
    )
    parser.add_argument("--csv", help="write every run (config + metrics) to this CSV file")
    args = parser.parse_args(argv)

    base = Config()
    for item in args.set:
        name, value = _parse_assign(item)
        base = replace(base, **{name: _coerce(name, value)})

    sweep_names, sweep_values = [], []
    for item in args.sweep:
        name, values = _parse_assign(item)
        sweep_names.append(name)
        sweep_values.append([_coerce(name, v) for v in values.split(",")])

    rows: List[Dict[str, object]] = []
    for combo in itertools.product(*sweep_values) if sweep_names else [()]:
        cfg = replace(base, **dict(zip(sweep_names, combo)))
        rows.append({**asdict(cfg), **simulate(cfg)})

    if not sweep_names:
        print("config:", ", ".join(f"{k}={v}" for k, v in asdict(base).items()))
        for k, v in rows[0].items():
            if k not in _FIELD_TYPES:
                print(f"  {k:22s} {_fmt(v)}")
    else:
        _print_table(rows, sweep_names + REPORT_COLUMNS)

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {len(rows)} row(s) to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
