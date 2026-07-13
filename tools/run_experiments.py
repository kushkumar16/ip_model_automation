#!/usr/bin/env python3
"""Performance experiments: sweep model configurations and report the metrics.

Each experiment sweeps one configuration parameter across a set of values,
runs a fixed deterministic workload against a model (IP or subsystem) per
value, and collects rows from the metrics dictionaries the models already
expose. Results are written as one CSV per experiment plus a combined
markdown summary — latency/throughput vs. configuration tables such as
"descriptor end-to-end latency vs. interconnect outstanding limit".

Outputs land in reports/experiments/ (gitignored; results are regenerable).

Usage::

    python tools/run_experiments.py                 # run all experiments
    python tools/run_experiments.py --list          # list experiments
    python tools/run_experiments.py --only dma_outstanding_limit
    python tools/run_experiments.py --output-dir reports/experiments
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import simpy  # noqa: E402

from ip_model_automation.common import Command, Descriptor  # noqa: E402
from ip_model_automation.ip import (  # noqa: E402
    ArbitrationIpModel,
    AxiInterconnectIpModel,
    DmaSubsystemModel,
    MailboxIrqSubsystemModel,
)

# Fast member latencies so sweeps finish quickly; relative behavior is what
# the experiments measure, matching the approach used by the unit tests.
FAST_GDMA = {
    "fetch_latency": 2,
    "read_latency": 2,
    "write_latency": 2,
    "completion_latency": 2,
    "channel_scan_latency": 1,
    "irq_latency": 1,
}
FAST_INTERCONNECT = {
    "decode_latency": 1,
    "arbitration_latency": 1,
    "slave_latency": 1,
    "response_latency": 1,
    "write_join_latency": 1,
    "error_latency": 1,
}
FAST_ARBITRATION = {
    "bitmap_latency": 1,
    "port_scan_latency": 1,
    "tenant_scan_latency": 1,
    "sq_scan_latency": 1,
    "grant_latency": 1,
    "selection_accept_latency": 1,
    "pending_count_latency": 1,
    "burst_read_latency": 1,
    "burst_calc_latency": 1,
    "burst_debit_latency": 1,
    "issue_latency": 1,
}
FAST_COMPLETION = {
    "service_latency": 1,
    "tenant_select_latency": 1,
    "token_check_latency": 1,
    "emit_latency": 1,
    "retry_latency": 1,
}
FAST_CONTROLLER = {
    "sample_latency": 1,
    "pending_latency": 1,
    "filter_latency": 1,
    "priority_latency": 1,
    "delivery_latency": 1,
    "ack_latency": 1,
    "eoi_latency": 1,
    "register_latency": 1,
    "software_latency": 1,
}


@dataclass
class Experiment:
    name: str
    description: str
    param: str
    values: List
    runner: Callable[[object], Dict[str, object]]
    metrics: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Runners: one deterministic workload per experiment, returning a metrics row.
# --------------------------------------------------------------------------- #
def run_dma_outstanding_limit(limit: int) -> Dict[str, object]:
    """8 descriptors through the DMA subsystem vs. fabric outstanding credit."""
    env = simpy.Environment()
    subsystem = DmaSubsystemModel(
        env,
        gdma_kwargs=dict(FAST_GDMA),
        interconnect_kwargs={
            **FAST_INTERCONNECT,
            "outstanding_limit": limit,
            "slave_latency": 10,
            "response_latency": 10,
        },
        arbitration_kwargs=dict(FAST_ARBITRATION),
        completion_kwargs=dict(FAST_COMPLETION),
    )
    subsystem.configure_channel(0)
    for index in range(8):
        subsystem.submit(
            Descriptor(
                f"d{index}",
                channel_id=0,
                src_addr=0x1000 * (index + 1),
                dst_addr=0x2000 * (index + 1),
                length_bytes=4096,
            )
        )
    env.run(until=6000)
    return {
        "descriptors_completed": subsystem.metrics["descriptors_completed"],
        "last_end_to_end_latency": subsystem.metrics["end_to_end_latency"],
        "fabric_backpressure_events": subsystem.metrics["fabric_backpressure_events"],
        "interconnect_outstanding_stalls": subsystem.interconnect.metrics["outstanding_stalls"],
        "gdma_read_stalls": subsystem.gdma.metrics["read_stalls"],
    }


def run_dma_qos_budget(budget: float) -> Dict[str, object]:
    """6 descriptors vs. per-tenant completion token budget, refilled every 400 ticks."""
    env = simpy.Environment()
    subsystem = DmaSubsystemModel(
        env,
        gdma_kwargs=dict(FAST_GDMA),
        interconnect_kwargs=dict(FAST_INTERCONNECT),
        arbitration_kwargs=dict(FAST_ARBITRATION),
        completion_kwargs=dict(FAST_COMPLETION),
    )
    subsystem.configure_channel(0)
    subsystem.configure_qos("T0", token_budget=budget)

    def refill_driver():
        while True:
            yield env.timeout(400)
            subsystem.refill_qos()

    env.process(refill_driver())
    for index in range(6):
        subsystem.submit(
            Descriptor(
                f"d{index}",
                channel_id=0,
                src_addr=0x1000 * (index + 1),
                dst_addr=0x2000 * (index + 1),
                length_bytes=4096,
            )
        )
    env.run(until=6000)
    return {
        "descriptors_completed": subsystem.metrics["descriptors_completed"],
        "last_end_to_end_latency": subsystem.metrics["end_to_end_latency"],
        "completion_token_stalls": subsystem.completion.metrics["token_stalls"],
        "completion_backlog_events": subsystem.metrics["completion_backlog_events"],
    }


def run_mailbox_storm_window(window: int) -> Dict[str, object]:
    """6-message flood on one channel vs. storm throttle window (storm_limit=2)."""
    env = simpy.Environment()
    subsystem = MailboxIrqSubsystemModel(
        env,
        controller_kwargs=dict(FAST_CONTROLLER),
        storm_limit=2,
        storm_window=window,
    )
    subsystem.configure_channel(0)
    for index in range(6):
        subsystem.send(0, f"m{index}")
    env.run(until=6000)
    return {
        "messages_serviced": subsystem.metrics["messages_serviced"],
        "last_round_trip_latency": subsystem.metrics["round_trip_latency"],
        "storm_throttle_events": subsystem.metrics["storm_throttle_events"],
        "storm_mask_releases": subsystem.metrics["storm_mask_releases"],
        "level_reasserts_used": subsystem.metrics["level_reasserts_used"],
    }


def run_mailbox_storm_limit(limit: int) -> Dict[str, object]:
    """6-message flood on one channel vs. storm limit (storm_window=40)."""
    env = simpy.Environment()
    subsystem = MailboxIrqSubsystemModel(
        env,
        controller_kwargs=dict(FAST_CONTROLLER),
        storm_limit=limit,
        storm_window=40,
    )
    subsystem.configure_channel(0)
    for index in range(6):
        subsystem.send(0, f"m{index}")
    env.run(until=6000)
    return {
        "messages_serviced": subsystem.metrics["messages_serviced"],
        "last_round_trip_latency": subsystem.metrics["round_trip_latency"],
        "storm_throttle_events": subsystem.metrics["storm_throttle_events"],
        "delivered_irqs": subsystem.controller.metrics["delivered_irqs"],
    }


def run_arbitration_burst_credit(credit: int) -> Dict[str, object]:
    """12 commands across two tenants vs. device/tenant burst credit."""
    env = simpy.Environment()
    model = ArbitrationIpModel(env, **FAST_ARBITRATION)
    model.configure_burst(device=credit, tenants={"T0": credit, "T1": credit})
    for index in range(6):
        model.enqueue(Command(f"a{index}", "READ", port_id="port0", tenant_id="T0", sq_id="SQ0"))
        model.enqueue(Command(f"b{index}", "READ", port_id="port0", tenant_id="T1", sq_id="SQ0"))
    env.run(until=2000)
    return {
        "issued_commands": model.metrics["issued_commands"],
        "downstream_requests": model.metrics["downstream_requests"],
        "burst_stalls": model.metrics["burst_stalls"],
        "grants": model.metrics["grants"],
    }


def run_axi_outstanding_limit(limit: int) -> Dict[str, object]:
    """8 reads through the interconnect vs. outstanding-transaction credit."""
    env = simpy.Environment()
    model = AxiInterconnectIpModel(
        env,
        **{**FAST_INTERCONNECT, "outstanding_limit": limit, "slave_latency": 8, "response_latency": 8},
    )
    model.add_route(0x0, 0x10000, "mem0")
    for index in range(8):
        model.submit(Command(f"r{index}", "READ", addr=0x100 * (index + 1)))
    env.run(until=2000)
    return {
        "responses": len(model.responses),
        "last_read_latency": model.metrics["read_latency"],
        "outstanding_stalls": model.metrics["outstanding_stalls"],
        "read_grants": model.metrics["read_grants"],
    }


EXPERIMENTS: Dict[str, Experiment] = {
    exp.name: exp
    for exp in [
        Experiment(
            name="dma_outstanding_limit",
            description="DMA subsystem: descriptor end-to-end latency and engine throttling"
            " vs. interconnect outstanding limit.",
            param="outstanding_limit",
            values=[1, 2, 4, 8],
            runner=run_dma_outstanding_limit,
        ),
        Experiment(
            name="dma_qos_budget",
            description="DMA subsystem: completion delay and token stalls vs. per-tenant QoS token budget"
            " (refill every 400 ticks).",
            param="token_budget",
            values=[2, 4, 8, 1_000_000],
            runner=run_dma_qos_budget,
        ),
        Experiment(
            name="mailbox_storm_window",
            description="Mailbox IRQ subsystem: message service and round-trip latency vs. storm throttle window"
            " (flooded channel, storm_limit=2).",
            param="storm_window",
            values=[10, 20, 40, 80],
            runner=run_mailbox_storm_window,
        ),
        Experiment(
            name="mailbox_storm_limit",
            description="Mailbox IRQ subsystem: throttle behavior vs. storm limit (storm_window=40).",
            param="storm_limit",
            values=[1, 2, 4, 8],
            runner=run_mailbox_storm_limit,
        ),
        Experiment(
            name="arbitration_burst_credit",
            description="Arbitration IP: issued commands and burst stalls vs. device/tenant burst credit"
            " (12 queued commands, no refill).",
            param="burst_credit",
            values=[1, 2, 4, 16],
            runner=run_arbitration_burst_credit,
        ),
        Experiment(
            name="axi_outstanding_limit",
            description="AXI interconnect: read latency and stalls vs. outstanding-transaction credit"
            " (8 reads, slow slave).",
            param="outstanding_limit",
            values=[1, 2, 4, 8],
            runner=run_axi_outstanding_limit,
        ),
    ]
}


def run_experiment(experiment: Experiment, output_dir: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for value in experiment.values:
        metrics = experiment.runner(value)
        rows.append({experiment.param: value, **metrics})
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{experiment.name}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return rows


def markdown_table(param: str, rows: List[Dict[str, object]]) -> List[str]:
    headers = list(rows[0].keys())
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(row[h]) for h in headers) + " |")
    return lines


def write_summary(results: Dict[str, List[Dict[str, object]]], output_dir: Path) -> Path:
    lines = [
        "# Performance Experiment Summary",
        "",
        "Generated by `tools/run_experiments.py`. Each table sweeps one configuration",
        "parameter against a fixed deterministic workload; values come from the models'",
        "metrics dictionaries. Member latencies are the fast test settings, so absolute",
        "numbers are model ticks — relative trends across the sweep are the result.",
        "",
    ]
    for name, rows in results.items():
        experiment = EXPERIMENTS[name]
        lines.append(f"## {name}")
        lines.append("")
        lines.append(experiment.description)
        lines.append("")
        lines.extend(markdown_table(experiment.param, rows))
        lines.append("")
    summary_path = output_dir / "summary.md"
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list experiments and exit")
    parser.add_argument("--only", action="append", help="run only the named experiment (repeatable)")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "reports" / "experiments")
    args = parser.parse_args(argv)

    if args.list:
        for experiment in EXPERIMENTS.values():
            print(f"{experiment.name}: {experiment.description}")
        return 0

    names = args.only or list(EXPERIMENTS)
    unknown = [name for name in names if name not in EXPERIMENTS]
    if unknown:
        print(f"unknown experiments: {', '.join(unknown)}", file=sys.stderr)
        return 1

    results: Dict[str, List[Dict[str, object]]] = {}
    for name in names:
        experiment = EXPERIMENTS[name]
        print(f"running {name} ({len(experiment.values)} points)...")
        results[name] = run_experiment(experiment, args.output_dir)
        print(f"  wrote {args.output_dir / (name + '.csv')}")
    summary = write_summary(results, args.output_dir)
    print(f"wrote {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
