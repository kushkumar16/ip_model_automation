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
        weighted_order_rebuild_latency: int = 8,
        reset_latency: int = 1,
        log_level: str = "WARNING",
        log_file: str = "run.log",
    ):
        self.env = env
        self.logger = get_ip_logger("arbitration_ip", log_level, log_file)
        self.topology = topology or DEFAULT_TOPOLOGY
        self.port_mode = port_mode
        self.output_ready = True
        # qos_credit_if: a sampled `credit_status` input, declared
        # wait_for_response at arbiter_main.TENANT_SCAN, whose eligibility result
        # decides whether a candidate may be granted. There was no credit or
        # liveness state at all, so the sample had nothing to read and the grant
        # ignored it. arbitration.eligibility_owner is arbitration_ip_check_only,
        # so this IP CHECKS the credit and never debits it -- token_debit_owner
        # names completion_ip or the QoS service for that.
        self.tenant_credit: Dict[str, Dict[str, object]] = defaultdict(
            lambda: {
                "tenant_alive": True,
                "read_iops_credit": True,
                "write_iops_credit": True,
                "read_bw_credit": True,
                "write_bw_credit": True,
            }
        )
        self.device_burst_available = 1024
        self.tenant_burst_available = defaultdict(lambda: 1024)
        self.sq_burst_available = defaultdict(lambda: defaultdict(lambda: 1024))

        normalized_weights = self._normalize_weights(weights)
        if self.port_mode == "single":
            normalized_weights["ports"] = {"port0": normalized_weights["ports"].get("port0", 1)}

        self.port_policy = WeightedOrder(normalized_weights["ports"])
        self.tenant_policies = {
            port_id: WeightedOrder(
                {tenant: normalized_weights["tenants"].get(tenant, 1) for tenant in port_cfg["tenants"]}
            )
            for port_id, port_cfg in self.topology.items()
            if self.port_mode != "single" or port_id == "port0"
        }
        self.sq_policies = {
            tenant: WeightedOrder({sq: normalized_weights["sqs"].get(sq, 1) for sq in sqs})
            for port_cfg in self.topology.values()
            for tenant, sqs in port_cfg["tenants"].items()
        }

        if scan_latency is not None:
            # One value for the three scan stages, and only those. This used to
            # set port_scan to the value and then silently zero tenant_scan,
            # sq_scan and grant -- so a caller reaching for it as a convenience
            # opted out of three of the four declared arbiter_main latencies
            # without saying so, and every test in this IP does reach for it.
            # `grant` is not a scan and is left alone; a caller who wants it
            # short passes grant_latency.
            port_scan_latency = scan_latency
            tenant_scan_latency = scan_latency
            sq_scan_latency = scan_latency

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
            "backpressure_retry": 1,
            "pointer_update": 1,
            "age_update": 2,
            "weighted_order_rebuild": weighted_order_rebuild_latency,
            "reset": reset_latency,
        }

        self.selected_sq_q = simpy.Store(env, capacity=1)
        self.policy_update_q = simpy.Store(env)
        self.queues: Dict[str, Dict[str, Dict[str, Deque[Command]]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(deque))
        )
        self.port_pending_bitmap: Dict[str, bool] = {port: False for port in self.topology}
        self.tenant_pending_bitmap: Dict[str, Dict[str, bool]] = {
            port: {tenant: False for tenant in cfg["tenants"]} for port, cfg in self.topology.items()
        }
        self.sq_pending_bitmap: Dict[str, Dict[str, bool]] = {
            tenant: {sq: False for sq in sqs}
            for cfg in self.topology.values()
            for tenant, sqs in cfg["tenants"].items()
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

    def configure_burst(
        self,
        device: Optional[int] = None,
        tenants: Optional[Dict[str, int]] = None,
        sqs: Optional[Dict[str, Dict[str, int]]] = None,
    ) -> None:
        if device is not None:
            self.device_burst_available = int(device)
        for tenant_id, value in (tenants or {}).items():
            self.tenant_burst_available[tenant_id] = int(value)
        for tenant_id, sq_values in (sqs or {}).items():
            for sq_id, value in sq_values.items():
                self.sq_burst_available[tenant_id][sq_id] = int(value)
        self.logger.info("configured burst device=%s tenants=%s sqs=%s", device, tenants or {}, sqs or {})

    def set_tenant_credit(self, tenant_id: str, **fields: object) -> None:
        """Drive qos_credit_if's eligibility_check fields for one tenant.

        The five fields the transaction declares: tenant_alive, read_iops_credit,
        write_iops_credit, read_bw_credit, write_bw_credit. Unnamed fields keep
        their current value, so a caller can make a tenant ineligible on one axis
        without restating the rest.
        """
        unknown = set(fields) - set(self.tenant_credit[tenant_id])
        if unknown:
            raise ValueError(f"qos_credit_if declares no field(s) {sorted(unknown)}")
        self.tenant_credit[tenant_id].update(fields)
        self.logger.debug("credit status tenant=%s %s", tenant_id, dict(self.tenant_credit[tenant_id]))

    def sample_eligibility(self, tenant_id: str) -> bool:
        """The eligibility_check sample taken during candidate evaluation.

        A candidate is eligible only while its tenant is alive and holds credit
        on every declared axis; the sample is read, never debited.
        """
        status = self.tenant_credit[tenant_id]
        return all(bool(status[field]) for field in status)

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

    def _clear_state(self) -> None:
        """The clear_state action the template declares on RESET -> IDLE."""
        self.metrics["resets"] += 1
        self._update_pending_bitmaps()

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
            if not self.tenant_pending_bitmap[port_id].get(tenant_id, False):
                continue
            # The sample taken at TENANT_SCAN. An ineligible tenant is passed
            # over for this scan rather than granted; it becomes selectable again
            # as soon as the credit status says so.
            if not self.sample_eligibility(tenant_id):
                self.metrics["eligibility_rejections"] += 1
                self.logger.debug("tenant ineligible tenant=%s time=%s", tenant_id, self.env.now)
                continue
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
        # RESET -> IDLE is declared with reset_deasserted and a 1-cycle cost. It
        # used to be neither held nor charged: the constructor set RESET and this
        # line overwrote it before any yield, so neither state existed at any
        # simulated time and the declared reset behaviour was not modelled.
        self._set_fsm_state("arbiter_main", "RESET")
        yield self.env.timeout(self.latency["reset"])
        # RESET -> IDLE is declared with clear_state as its action, and IDLE is
        # the only declared way into a scan. The loop used to start at PORT_SCAN
        # and enter IDLE only when the bitmaps happened to be dirty -- which
        # _clear_state() has just made false -- so both declared entries into
        # IDLE (RESET -> IDLE and GRANT -> IDLE) were never taken and the model
        # took undeclared RESET -> PORT_SCAN and GRANT -> PORT_SCAN instead.
        self._set_fsm_state("arbiter_main", "IDLE")
        self._clear_state()
        while True:
            # IDLE is held on every pass, so it is occupied rather than merely
            # assigned, and the loop is re-entered here after a grant -- which is
            # the declared GRANT -> IDLE. bitmap_update is still charged only
            # when the bitmaps are actually dirty: charging it on every idle spin
            # would invent 3 cycles the timing model does not price for a refresh
            # with nothing to refresh. An idle pass instead costs the 1-cycle
            # retry the template prices for a poll that found nothing.
            self._set_fsm_state("arbiter_main", "IDLE")
            if self.bitmap_dirty:
                yield self.env.timeout(self.latency["bitmap"])
                self._update_pending_bitmaps()
            else:
                yield self.env.timeout(self.latency["backpressure_retry"])
            self._set_fsm_state("arbiter_main", "PORT_SCAN")
            yield self.env.timeout(self.latency["port_scan"])
            # PORT_SCAN -> STALL on no_eligible_port is declared, and was never
            # taken: the tenant and SQ scans ran unconditionally, so a stall was
            # always reached from SQ_SCAN having charged all three scans first.
            if not any(self.port_pending_bitmap.values()):
                self._set_fsm_state("arbiter_main", "STALL")
                self.metrics["stalls"] += 1
                # increment_no_eligible_stall is the declared action on this
                # transition, and no_port_pending_stalls is the metric the
                # template names for it. It used to be incremented only inside
                # select_sq(), which this branch returns before ever reaching --
                # so the counter recorded a mid-scan race and never the stall it
                # is named for.
                self.metrics["no_port_pending_stalls"] += 1
                self.logger.debug("arbiter stall no eligible port time=%s", self.env.now)
                yield self.env.timeout(self.latency["backpressure_retry"])
                continue
            self._set_fsm_state("arbiter_main", "TENANT_SCAN")
            yield self.env.timeout(self.latency["tenant_scan"])
            self._set_fsm_state("arbiter_main", "SQ_SCAN")
            yield self.env.timeout(self.latency["sq_scan"])
            selection = self.select_sq()
            if selection is None:
                self._set_fsm_state("arbiter_main", "STALL")
                self.metrics["stalls"] += 1
                self.logger.debug("arbiter stall time=%s", self.env.now)
                yield self.env.timeout(self.latency["backpressure_retry"])
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
            # issue_if is wait_for_ack_inline at issue_pipeline.ISSUE_REQUEST,
            # resuming on issue_ready, and the declared invariants are that
            # commands are popped only when issue_ready is true and that queue
            # entries remain pending during downstream backpressure. The check
            # used to run only *before* the request: once ISSUE_REQUEST was
            # entered, issue_ready dropping during those 3 cycles did nothing and
            # the commands were popped anyway, with output_backpressure_cycles
            # left at 0. The request is now re-driven until it completes with
            # issue_ready still high, so nothing leaves the queue during a stall.
            while True:
                while not self.output_ready:
                    self._set_fsm_state("issue_pipeline", "ISSUE_STALL")
                    self.metrics["output_stalls"] += 1
                    self.metrics["output_backpressure_cycles"] += 1
                    self.logger.warning("output backpressure selection=%s time=%s", selection, self.env.now)
                    yield self.env.timeout(1)

                self._set_fsm_state("issue_pipeline", "ISSUE_REQUEST")
                yield self.env.timeout(self.latency["issue"])
                if self.output_ready:
                    break
                self.logger.warning("issue_ready dropped during request selection=%s time=%s", selection, self.env.now)
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
            # Advancing all three weighted pointers is the weighted_order_rebuild
            # the timing model prices at 8 cycles; it was performed but never
            # charged, so policy_update spanned 3 cycles against a declared 11.
            self.port_policy.advance_to_after(selection["port_id"])
            self.tenant_policies[selection["port_id"]].advance_to_after(selection["tenant_id"])
            self.sq_policies[selection["tenant_id"]].advance_to_after(selection["sq_id"])
            yield self.env.timeout(self.latency["weighted_order_rebuild"])
            self.metrics["policy_updates"] += 1
            self.logger.debug("policy update selection=%s", selection)
            self._set_fsm_state("policy_update", "UPDATE_AGE")
            yield self.env.timeout(self.latency["age_update"])
            self.metrics["age_updates"] += 1
            self._set_fsm_state("policy_update", "WAIT_GRANT")
