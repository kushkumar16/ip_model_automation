from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import simpy

from .arbitration_ip import ArbitrationIpModel
from .axi_interconnect_ip import AxiInterconnectIpModel
from .common import Command, Descriptor, get_ip_logger
from .completion_ip import CompletionIpModel
from .gdma_ip import GdmaIpModel


DEFAULT_SUBSYSTEM_TOPOLOGY = {
    "port0": {
        "tenants": {
            "T0": ["SQ0"],
            "T1": ["SQ0"],
        }
    }
}


class DmaSubsystemModel:
    """SimPy delay model of the connected DMA data-mover subsystem.

    Composes four member IP models — GDMA engine, AXI interconnect, arbitration
    IP, and completion IP — with glue processes that bridge their boundaries.
    Each descriptor runs an engine leg (inside GDMA) and a fabric leg (one READ
    and one WRITE command through interconnect -> arbitration -> completion QoS);
    the subsystem interrupt fires only when both legs are done. A backpressure
    monitor mirrors interconnect congestion into GDMA memory readiness and
    completion backlog into arbitration issue readiness.

    Glue timing comes from the subsystem template; member IP timing comes from
    each member's own reviewed template (overridable via *_kwargs).
    """

    def __init__(
        self,
        env: simpy.Environment,
        gdma_kwargs: Optional[Dict[str, Any]] = None,
        interconnect_kwargs: Optional[Dict[str, Any]] = None,
        arbitration_kwargs: Optional[Dict[str, Any]] = None,
        completion_kwargs: Optional[Dict[str, Any]] = None,
        completion_backlog_limit: int = 8,
        default_token_budget: float = 1_000_000.0,
        log_level: str = "WARNING",
        log_file: str = "run.log",
    ):
        self.env = env
        self.logger = get_ip_logger("dma_subsystem", log_level, log_file)
        self.completion_backlog_limit = completion_backlog_limit

        gdma_kwargs = dict(gdma_kwargs or {})
        interconnect_kwargs = dict(interconnect_kwargs or {})
        arbitration_kwargs = dict(arbitration_kwargs or {})
        completion_kwargs = dict(completion_kwargs or {})
        for kwargs in (gdma_kwargs, interconnect_kwargs, arbitration_kwargs, completion_kwargs):
            kwargs.setdefault("log_level", log_level)
            kwargs.setdefault("log_file", log_file)
        arbitration_kwargs.setdefault("topology", DEFAULT_SUBSYSTEM_TOPOLOGY)
        arbitration_kwargs.setdefault("port_mode", "single")
        arbitration_kwargs.setdefault("weights", {"T0": 1, "T1": 1})

        self.gdma = GdmaIpModel(env, **gdma_kwargs)
        self.interconnect = AxiInterconnectIpModel(env, **interconnect_kwargs)
        self.arbitration = ArbitrationIpModel(env, **arbitration_kwargs)
        self.completion = CompletionIpModel(env, **completion_kwargs)

        self.interconnect.add_route(0x0, 2**64, "mem0")
        for tenant_id in ("T0", "T1"):
            self.completion.configure_tenant(
                tenant_id,
                read=default_token_budget,
                write=default_token_budget,
                read_bw=default_token_budget,
                write_bw=default_token_budget,
            )

        self.descriptor_intake_queue = simpy.Store(env, capacity=16)
        # desc_id -> {"desc", "start", "engine_done", "fabric_done": {cmd_id: bool}}
        self.tracking: Dict[str, Dict[str, Any]] = {}
        self.cmd_to_desc: Dict[str, str] = {}

        self.completed: List[Tuple[float, Descriptor]] = []
        self.irqs: List[int] = []
        self.metrics = defaultdict(int)
        self.engine_throttled = False
        self.issue_throttled = False

        self._responses_seen = 0
        self._issued_seen = 0
        self._completions_seen = 0
        self._engine_seen = 0

        self.fsm_state = {
            "host_command": "IDLE",
            "fabric_bridge": "IDLE",
            "downstream_dispatch": "IDLE",
            "completion_collector": "IDLE",
            "backpressure_monitor": "IDLE",
        }

        self.env.process(self.host_command_process())
        self.env.process(self.fabric_bridge_process())
        self.env.process(self.downstream_dispatch_process())
        self.env.process(self.completion_collector_process())
        self.env.process(self.backpressure_monitor_process())
        self.logger.info("initialized backlog_limit=%s", completion_backlog_limit)

    # ------------------------------------------------------------------ #
    # Public API (functionality_model.apis)
    # ------------------------------------------------------------------ #
    def submit(self, desc: Descriptor):
        self.logger.info("submit descriptor=%s channel=%s", desc.desc_id, desc.channel_id)
        return self.descriptor_intake_queue.put(desc)

    def configure_channel(self, channel_id: int, enabled: bool = True, priority: int = 0) -> None:
        self.gdma.configure_channel(channel_id, enabled=enabled, priority=priority)

    def configure_qos(self, tenant_id: str, token_budget: float, limit_type: str = "SOFT") -> None:
        self.completion.configure_tenant(
            tenant_id,
            read=token_budget,
            write=token_budget,
            read_bw=max(token_budget, 1.0) * 1000.0,
            write_bw=max(token_budget, 1.0) * 1000.0,
            limit_type=limit_type,
        )
        self.logger.info("qos configured tenant=%s budget=%s", tenant_id, token_budget)

    def refill_qos(self) -> None:
        self.completion.refill_once()

    def set_route(self, base: int, limit: int, slave_id: str) -> None:
        self.interconnect.add_route(base, limit, slave_id)

    def get_metrics(self) -> Dict[str, int]:
        return dict(self.metrics)

    def _tenant_for_channel(self, channel_id: int) -> str:
        return f"T{channel_id % 2}"

    # ------------------------------------------------------------------ #
    # FSM processes (glue)
    # ------------------------------------------------------------------ #
    def host_command_process(self):
        while True:
            self.fsm_state["host_command"] = "IDLE"
            desc = yield self.descriptor_intake_queue.get()
            self.fsm_state["host_command"] = "ACCEPT_DESCRIPTOR"
            yield self.env.timeout(1)
            self.tracking[desc.desc_id] = {
                "desc": desc,
                "start": self.env.now,
                "engine_done": False,
                "fabric_done": {f"{desc.desc_id}:rd": False, f"{desc.desc_id}:wr": False},
            }
            self.metrics["descriptors_submitted"] += 1
            self.fsm_state["host_command"] = "FORWARD_ENGINE"
            yield self.env.timeout(1)
            yield self.gdma.submit(desc)
            self.fsm_state["host_command"] = "BUILD_FABRIC_COMMANDS"
            tenant_id = self._tenant_for_channel(desc.channel_id)
            size_kb = max(1, desc.length_bytes // 1024)
            read_cmd = Command(
                f"{desc.desc_id}:rd", "READ",
                tenant_id=tenant_id, sq_id="SQ0",
                addr=desc.src_addr, size_kb=size_kb,
            )
            write_cmd = Command(
                f"{desc.desc_id}:wr", "WRITE",
                tenant_id=tenant_id, sq_id="SQ0",
                addr=desc.dst_addr, size_kb=size_kb,
            )
            self.cmd_to_desc[read_cmd.cmd_id] = desc.desc_id
            self.cmd_to_desc[write_cmd.cmd_id] = desc.desc_id
            self.fsm_state["host_command"] = "SUBMIT_FABRIC"
            yield self.env.timeout(1)
            yield self.interconnect.submit(read_cmd)
            yield self.interconnect.submit(write_cmd)
            self.metrics["fabric_commands_issued"] += 2
            self.logger.debug("fabric commands issued descriptor=%s tenant=%s", desc.desc_id, tenant_id)

    def fabric_bridge_process(self):
        while True:
            self.fsm_state["fabric_bridge"] = "POLL_RESPONSES"
            yield self.env.timeout(1)
            new_responses = self.interconnect.responses[self._responses_seen:]
            self._responses_seen += len(new_responses)
            for _time, response in new_responses:
                desc_id = self.cmd_to_desc.get(response.cmd_id)
                entry = self.tracking.get(desc_id) if desc_id else None
                if entry is None:
                    continue
                if response.status == "DECERR":
                    self.metrics["fabric_decode_errors"] += 1
                    self.logger.error("fabric decode error cmd=%s", response.cmd_id)
                    continue
                # The interconnect response drops tenant/size context, so the
                # bridge rebuilds the arbitration command from the tracked
                # descriptor (this is the MAP_TENANT step).
                self.fsm_state["fabric_bridge"] = "MAP_TENANT"
                yield self.env.timeout(1)
                desc = entry["desc"]
                arb_cmd = Command(
                    response.cmd_id, response.kind,
                    tenant_id=self._tenant_for_channel(desc.channel_id), sq_id="SQ0",
                    addr=response.addr, size_kb=max(1, desc.length_bytes // 1024),
                )
                self.fsm_state["fabric_bridge"] = "ENQUEUE_ARBITRATION"
                yield self.env.timeout(1)
                self.arbitration.enqueue(arb_cmd)
                self.metrics["arb_enqueued"] += 1

    def downstream_dispatch_process(self):
        while True:
            self.fsm_state["downstream_dispatch"] = "POLL_ISSUED"
            yield self.env.timeout(1)
            new_issued = self.arbitration.issued[self._issued_seen:]
            self._issued_seen += len(new_issued)
            for _time, command in new_issued:
                self.fsm_state["downstream_dispatch"] = "SUBMIT_COMPLETION"
                yield self.env.timeout(1)
                yield self.completion.submit(command)
                self.metrics["completion_submitted"] += 1

    def completion_collector_process(self):
        while True:
            self.fsm_state["completion_collector"] = "POLL_COMPLETIONS"
            yield self.env.timeout(1)
            new_completions = self.completion.completed[self._completions_seen:]
            self._completions_seen += len(new_completions)
            new_engine = self.gdma.completed_ids[self._engine_seen:]
            self._engine_seen += len(new_engine)

            touched = []
            for _time, command in new_completions:
                desc_id = self.cmd_to_desc.get(command.cmd_id)
                if desc_id is None or desc_id not in self.tracking:
                    continue
                self.fsm_state["completion_collector"] = "MATCH_DESCRIPTOR"
                self.tracking[desc_id]["fabric_done"][command.cmd_id] = True
                self.metrics["completions_collected"] += 1
                touched.append(desc_id)
            for desc_id in new_engine:
                if desc_id not in self.tracking:
                    continue
                self.fsm_state["completion_collector"] = "MATCH_DESCRIPTOR"
                self.tracking[desc_id]["engine_done"] = True
                touched.append(desc_id)
            if touched:
                yield self.env.timeout(1)

            for desc_id in dict.fromkeys(touched):
                entry = self.tracking[desc_id]
                if entry["engine_done"] and all(entry["fabric_done"].values()):
                    self.fsm_state["completion_collector"] = "MARK_COMPLETE"
                    desc = entry["desc"]
                    self.tracking.pop(desc_id)
                    self.completed.append((self.env.now, desc))
                    self.metrics["descriptors_completed"] += 1
                    self.metrics["end_to_end_latency"] = self.env.now - entry["start"]
                    self.fsm_state["completion_collector"] = "ASSERT_IRQ"
                    yield self.env.timeout(1)
                    self.irqs.append(desc.channel_id)
                    self.metrics["subsystem_irqs"] += 1
                    self.logger.info(
                        "descriptor complete descriptor=%s channel=%s latency=%s",
                        desc_id, desc.channel_id, self.metrics["end_to_end_latency"],
                    )

    def backpressure_monitor_process(self):
        while True:
            self.fsm_state["backpressure_monitor"] = "SAMPLE_FABRIC"
            yield self.env.timeout(2)
            fabric_congested = len(self.interconnect.outstanding) >= self.interconnect.outstanding_limit
            if fabric_congested != self.engine_throttled:
                self.fsm_state["backpressure_monitor"] = "APPLY_ENGINE_THROTTLE"
                yield self.env.timeout(1)
                self.engine_throttled = fabric_congested
                self.gdma.set_memory_ready(source=not fabric_congested, destination=not fabric_congested)
                if fabric_congested:
                    self.metrics["fabric_backpressure_events"] += 1
                    self.logger.warning("fabric congested, engine throttled time=%s", self.env.now)

            self.fsm_state["backpressure_monitor"] = "SAMPLE_COMPLETION_BACKLOG"
            backlog = sum(len(queue) for queue in self.completion.pending.values())
            backlogged = backlog >= self.completion_backlog_limit
            if backlogged != self.issue_throttled:
                self.fsm_state["backpressure_monitor"] = "APPLY_ISSUE_THROTTLE"
                yield self.env.timeout(1)
                self.issue_throttled = backlogged
                self.arbitration.set_issue_ready(not backlogged)
                if backlogged:
                    self.metrics["completion_backlog_events"] += 1
                    self.logger.warning("completion backlog, issue throttled backlog=%s time=%s", backlog, self.env.now)
