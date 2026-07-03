from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

import simpy

from .common import Command, WeightedOrder, get_ip_logger


DEFAULT_TOPOLOGY = {
    "port0": {
        "tenants": {
            "T0": ["SQ0", "SQ1"],
            "T1": ["SQ0", "SQ1", "SQ2", "SQ3"],
        }
    },
    "port1": {
        "tenants": {
            "T2": ["SQ0", "SQ1"],
            "T3": ["SQ0"],
        }
    },
}

DEFAULT_WEIGHTS = {
    "ports": {"port0": 1, "port1": 1},
    "tenants": {"T0": 1, "T1": 4, "T2": 2, "T3": 1},
    "sqs": {"SQ0": 4, "SQ1": 2, "SQ2": 1, "SQ3": 1},
}


class ArbitrationIpModel:
    def __init__(
        self,
        env: simpy.Environment,
        weights: Optional[Dict[str, int] | Dict[str, Dict[str, int]]] = None,
        scan_latency: Optional[int] = None,
        issue_latency: int = 3,
        topology: Optional[Dict] = None,
        port_mode: str = "dual",
        bitmap_latency: int = 3,
        port_scan_latency: int = 4,
        tenant_scan_latency: int = 6,
        sq_scan_latency: int = 8,
        grant_latency: int = 2,
        selection_accept_latency: int = 1,
        pending_count_latency: int = 3,
        burst_read_latency: int = 4,
        burst_calc_latency: int = 2,
        burst_debit_latency: int = 2,
        log_level: str = "WARNING",
        log_file: str = "run.log",
    ):
        self.env = env
        self.logger = get_ip_logger("arbitration_ip", log_level, log_file)
        self.topology = topology or DEFAULT_TOPOLOGY
        self.port_mode = port_mode
        self.output_ready = True
        self.device_burst_available = 1024
        self.tenant_burst_available = defaultdict(lambda: 1024)
        self.sq_burst_available = defaultdict(lambda: defaultdict(lambda: 1024))

        normalized_weights = self._normalize_weights(weights)
        if self.port_mode == "single":
            normalized_weights["ports"] = {"port0": normalized_weights["ports"].get("port0", 1)}

        self.port_policy = WeightedOrder(normalized_weights["ports"])
        self.tenant_policies = {
            port_id: WeightedOrder({tenant: normalized_weights["tenants"].get(tenant, 1) for tenant in port_cfg["tenants"]})
            for port_id, port_cfg in self.topology.items()
            if self.port_mode != "single" or port_id == "port0"
        }
        self.sq_policies = {
            tenant: WeightedOrder({sq: normalized_weights["sqs"].get(sq, 1) for sq in sqs})
            for port_cfg in self.topology.values()
            for tenant, sqs in port_cfg["tenants"].items()
        }

        if scan_latency is not None:
            port_scan_latency = scan_latency
            tenant_scan_latency = 0
            sq_scan_latency = 0
            grant_latency = 0

        self.latency = {
            "bitmap": bitmap_latency,
            "port_scan": port_scan_latency,
            "tenant_scan": tenant_scan_latency,
            "sq_scan": sq_scan_latency,
            "grant": grant_latency,
            "issue": issue_latency,
            "selection_accept": selection_accept_latency,
            "pending_count": pending_count_latency,
            "burst_read": burst_read_latency,
            "burst_calc": burst_calc_latency,
            "burst_debit": burst_debit_latency,
            "pointer_update": 1,
            "age_update": 2,
        }

        self.selected_sq_q = simpy.Store(env, capacity=1)
        self.policy_update_q = simpy.Store(env)
        self.queues: Dict[str, Dict[str, Dict[str, Deque[Command]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(deque)))
        self.port_pending_bitmap: Dict[str, bool] = {port: False for port in self.topology}
        self.tenant_pending_bitmap: Dict[str, Dict[str, bool]] = {
            port: {tenant: False for tenant in cfg["tenants"]} for port, cfg in self.topology.items()
        }
        self.sq_pending_bitmap: Dict[str, Dict[str, bool]] = {
            tenant: {sq: False for sq in sqs} for cfg in self.topology.values() for tenant, sqs in cfg["tenants"].items()
        }
        self.bitmap_dirty = False
        self.inflight_sqs = set()
        self.issued: List[tuple[float, Command]] = []
        self.downstream_requests: List[Dict[str, object]] = []
        self.selection_trace: List[Dict[str, str]] = []
        self.fsm_state = {
            "arbiter_main": "RESET",
            "issue_pipeline": "RESET",
            "policy_update": "RESET",
        }
        self.transition_counts = defaultdict(int)
        self.metrics = defaultdict(int)
        self.env.process(self.arbiter_main())
        self.env.process(self.issue_pipeline())
        self.env.process(self.policy_update())
        self.logger.info("initialized port_mode=%s", self.port_mode)

    def _normalize_weights(self, weights):
        if weights is None:
            return {level: dict(values) for level, values in DEFAULT_WEIGHTS.items()}
        if weights and all(isinstance(value, int) for value in weights.values()):
            merged = {level: dict(values) for level, values in DEFAULT_WEIGHTS.items()}
            merged["tenants"].update(weights)
            return merged
        merged = {level: dict(values) for level, values in DEFAULT_WEIGHTS.items()}
        for level in ["ports", "tenants", "sqs"]:
            merged[level].update((weights or {}).get(level, {}))
        return merged

    def enqueue(self, command: Command) -> None:
        port_id = command.port_id
        tenant_id = command.tenant_id
        sq_id = command.sq_id
        if port_id not in self.topology:
            raise ValueError(f"unknown port_id: {port_id}")
        if tenant_id not in self.topology[port_id]["tenants"]:
            raise ValueError(f"tenant {tenant_id} is not linked to port {port_id}")
        if sq_id not in self.topology[port_id]["tenants"][tenant_id]:
            raise ValueError(f"SQ {sq_id} is not linked to tenant {tenant_id}")
        self.queues[port_id][tenant_id][sq_id].append(command)
        self.bitmap_dirty = True
        self.logger.info("enqueue cmd=%s port=%s tenant=%s sq=%s", command.cmd_id, port_id, tenant_id, sq_id)

    def configure_burst(self, device: Optional[int] = None, tenants: Optional[Dict[str, int]] = None, sqs: Optional[Dict[str, Dict[str, int]]] = None) -> None:
        if device is not None:
            self.device_burst_available = int(device)
        for tenant_id, value in (tenants or {}).items():
            self.tenant_burst_available[tenant_id] = int(value)
        for tenant_id, sq_values in (sqs or {}).items():
            for sq_id, value in sq_values.items():
                self.sq_burst_available[tenant_id][sq_id] = int(value)
        self.logger.info("configured burst device=%s tenants=%s sqs=%s", device, tenants or {}, sqs or {})

    def set_issue_ready(self, ready: bool) -> None:
        self.output_ready = ready
        self.logger.info("issue_ready=%s", ready)

    def query_pending_bitmaps(self) -> Dict[str, object]:
        if self.bitmap_dirty:
            self._update_pending_bitmaps()
        return {
            "port": dict(self.port_pending_bitmap),
            "tenant": {port: dict(values) for port, values in self.tenant_pending_bitmap.items()},
            "sq": {tenant: dict(values) for tenant, values in self.sq_pending_bitmap.items()},
        }

    def get_metrics(self) -> Dict[str, int]:
        return dict(self.metrics)

    def get_issued_trace(self) -> List[tuple[float, Command]]:
        return list(self.issued)

    def _set_fsm_state(self, fsm: str, state: str) -> None:
        self.fsm_state[fsm] = state
        self.transition_counts[f"{fsm}.{state}"] += 1
        self.logger.debug("fsm=%s state=%s time=%s", fsm, state, self.env.now)

    def _update_pending_bitmaps(self) -> None:
        for port_id, port_cfg in self.topology.items():
            if self.port_mode == "single" and port_id != "port0":
                self.port_pending_bitmap[port_id] = False
                continue
            port_pending = False
            for tenant_id, sqs in port_cfg["tenants"].items():
                tenant_pending = False
                for sq_id in sqs:
                    sq_pending = bool(self.queues[port_id][tenant_id][sq_id])
                    self.sq_pending_bitmap[tenant_id][sq_id] = sq_pending
                    tenant_pending = tenant_pending or sq_pending
                self.tenant_pending_bitmap[port_id][tenant_id] = tenant_pending
                port_pending = port_pending or tenant_pending
            self.port_pending_bitmap[port_id] = port_pending
        self.bitmap_dirty = False
        self.metrics["bitmap_updates"] += 1

    def _select_port(self) -> Optional[str]:
        for port_id in self.port_policy.scan():
            if self.port_pending_bitmap.get(port_id, False):
                return port_id
        return None

    def _select_tenant(self, port_id: str) -> Optional[str]:
        for tenant_id in self.tenant_policies[port_id].scan():
            if self.tenant_pending_bitmap[port_id].get(tenant_id, False):
                return tenant_id
        return None

    def _select_sq(self, tenant_id: str) -> Optional[str]:
        for sq_id in self.sq_policies[tenant_id].scan():
            if self.sq_pending_bitmap[tenant_id].get(sq_id, False) and (tenant_id, sq_id) not in self.inflight_sqs:
                return sq_id
        return None

    def select_sq(self) -> Optional[Dict[str, str]]:
        port_id = self._select_port()
        if port_id is None:
            self.metrics["no_port_pending_stalls"] += 1
            return None

        tenant_id = self._select_tenant(port_id)
        if tenant_id is None:
            self.metrics["no_tenant_pending_stalls"] += 1
            return None

        sq_id = self._select_sq(tenant_id)
        if sq_id is None:
            self.metrics["no_sq_pending_stalls"] += 1
            return None

        self.inflight_sqs.add((tenant_id, sq_id))
        selection = {"port_id": port_id, "tenant_id": tenant_id, "sq_id": sq_id}
        self.selection_trace.append(selection)
        self.metrics["selected_sqs"] += 1
        return selection

    def _pending_count(self, selection: Dict[str, str]) -> int:
        return len(self.queues[selection["port_id"]][selection["tenant_id"]][selection["sq_id"]])

    def _issue_count(self, selection: Dict[str, str]) -> int:
        tenant_id = selection["tenant_id"]
        sq_id = selection["sq_id"]
        return min(
            self._pending_count(selection),
            self.device_burst_available,
            self.tenant_burst_available[tenant_id],
            self.sq_burst_available[tenant_id][sq_id],
        )

    def arbiter_main(self):
        self._set_fsm_state("arbiter_main", "IDLE")
        while True:
            if self.bitmap_dirty:
                self._set_fsm_state("arbiter_main", "IDLE")
                yield self.env.timeout(self.latency["bitmap"])
                self._update_pending_bitmaps()
            self._set_fsm_state("arbiter_main", "PORT_SCAN")
            yield self.env.timeout(self.latency["port_scan"])
            self._set_fsm_state("arbiter_main", "TENANT_SCAN")
            yield self.env.timeout(self.latency["tenant_scan"])
            self._set_fsm_state("arbiter_main", "SQ_SCAN")
            yield self.env.timeout(self.latency["sq_scan"])
            selection = self.select_sq()
            if selection is None:
                self._set_fsm_state("arbiter_main", "STALL")
                self.metrics["stalls"] += 1
                self.logger.debug("arbiter stall time=%s", self.env.now)
                yield self.env.timeout(1)
                continue
            self._set_fsm_state("arbiter_main", "GRANT")
            yield self.env.timeout(self.latency["grant"])
            yield self.selected_sq_q.put(selection)
            yield self.policy_update_q.put(selection)
            self.metrics["grants"] += 1

    def issue_pipeline(self):
        self._set_fsm_state("issue_pipeline", "WAIT_SELECTION")
        while True:
            self._set_fsm_state("issue_pipeline", "WAIT_SELECTION")
            selection = yield self.selected_sq_q.get()
            self._set_fsm_state("issue_pipeline", "READ_PENDING_COUNT")
            yield self.env.timeout(self.latency["selection_accept"])
            yield self.env.timeout(self.latency["pending_count"])
            pending_count = self._pending_count(selection)
            self._set_fsm_state("issue_pipeline", "READ_BURST")
            yield self.env.timeout(self.latency["burst_read"])
            self._set_fsm_state("issue_pipeline", "CALC_ISSUE_COUNT")
            yield self.env.timeout(self.latency["burst_calc"])
            issue_count = self._issue_count(selection)
            if issue_count <= 0:
                self._set_fsm_state("issue_pipeline", "ISSUE_STALL")
                self.metrics["burst_stalls"] += 1
                self.logger.warning("burst stall selection=%s time=%s", selection, self.env.now)
                self.inflight_sqs.discard((selection["tenant_id"], selection["sq_id"]))
                yield self.env.timeout(1)
                continue
            while not self.output_ready:
                self._set_fsm_state("issue_pipeline", "ISSUE_STALL")
                self.metrics["output_stalls"] += 1
                self.metrics["output_backpressure_cycles"] += 1
                self.logger.warning("output backpressure selection=%s time=%s", selection, self.env.now)
                yield self.env.timeout(1)

            self._set_fsm_state("issue_pipeline", "ISSUE_REQUEST")
            yield self.env.timeout(self.latency["issue"])
            issued_cmds = []
            queue = self.queues[selection["port_id"]][selection["tenant_id"]][selection["sq_id"]]
            for _ in range(issue_count):
                command = queue.popleft()
                issued_cmds.append(command)
                self.issued.append((self.env.now, command))

            self.device_burst_available -= issue_count
            self.tenant_burst_available[selection["tenant_id"]] -= issue_count
            self.sq_burst_available[selection["tenant_id"]][selection["sq_id"]] -= issue_count
            self._set_fsm_state("issue_pipeline", "UPDATE_BURST")
            yield self.env.timeout(self.latency["burst_debit"])
            self.inflight_sqs.discard((selection["tenant_id"], selection["sq_id"]))
            self._update_pending_bitmaps()
            self.downstream_requests.append(
                {
                    "time": self.env.now,
                    "port_id": selection["port_id"],
                    "tenant_id": selection["tenant_id"],
                    "sq_id": selection["sq_id"],
                    "pending_cmd_count": pending_count,
                    "issue_count": issue_count,
                    "cmd_ids": [command.cmd_id for command in issued_cmds],
                }
            )
            self.metrics["downstream_requests"] += 1
            self.metrics["issued_commands"] += issue_count
            self.logger.info(
                "issued port=%s tenant=%s sq=%s issue_count=%s cmds=%s",
                selection["port_id"],
                selection["tenant_id"],
                selection["sq_id"],
                issue_count,
                [command.cmd_id for command in issued_cmds],
            )

    def policy_update(self):
        self._set_fsm_state("policy_update", "WAIT_GRANT")
        while True:
            selection = yield self.policy_update_q.get()
            self._set_fsm_state("policy_update", "UPDATE_POINTER")
            yield self.env.timeout(self.latency["pointer_update"])
            self.port_policy.advance_to_after(selection["port_id"])
            self.tenant_policies[selection["port_id"]].advance_to_after(selection["tenant_id"])
            self.sq_policies[selection["tenant_id"]].advance_to_after(selection["sq_id"])
            self.metrics["policy_updates"] += 1
            self.logger.debug("policy update selection=%s", selection)
            self._set_fsm_state("policy_update", "UPDATE_AGE")
            yield self.env.timeout(self.latency["age_update"])
            self.metrics["age_updates"] += 1
            self._set_fsm_state("policy_update", "WAIT_GRANT")
