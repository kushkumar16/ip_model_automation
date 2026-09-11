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
        issue_slot_latency: int = 1,
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
            "weighted_order_rebuild": weighted_order_rebuild_latency,
            "reset": reset_latency,
        }

        self._arbiter_wake = None
        self.issue_slot_latency = issue_slot_latency
        # issue_slots: declared capacity 1 with latency_cycles 1. The capacity
        # was already modelled by the depth-1 handoff store below, which limits
        # the arbiter to one selection ahead; the port's own latency was charged
        # nowhere. It is charged in the background, as ruled for completion_ip's
        # output port: the pipeline holds the slot across the downstream request
        # and hands the release to a process that lets the port's latency elapse
        # first, so the declared 38-cycle path is unchanged while the port stays
        # busy a cycle longer -- which is where a capacity of 1 is felt.
        self.issue_slots = simpy.Resource(env, capacity=1)
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
        self._wake_arbiter()
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

    def _pending_ports(self) -> list:
        """Every pending port, in policy order.

        The scan needs all of them, not just the first: `eligible_port_found` is
        about a port that yields a grantable candidate, and that cannot be known
        until its tenants have been examined.
        """
        return [port_id for port_id in self.port_policy.scan() if self.port_pending_bitmap.get(port_id, False)]

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

    def _commit_selection(self, port_id: str, tenant_id: str, sq_id: str) -> Dict[str, str]:
        """Record a completed three-level selection.

        This used to be the tail of a `select_sq()` that ran all three levels
        back to back, after arbiter_main had already charged port_scan,
        tenant_scan and sq_scan. That put every decision -- including the
        qos_credit_if eligibility sample, whose declared wait point is
        arbiter_main.TENANT_SCAN -- in SQ_SCAN, eight cycles after TENANT_SCAN
        had been left, and made TENANT_SCAN -> SQ_SCAN a transition taken with
        its declared condition `eligible_tenant_found` not yet evaluated. Each
        level is now decided in the state declared to decide it; only the
        bookkeeping is shared.
        """
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

    def _release_issue_slot(self, slot):
        """Let the issue port's declared latency elapse, then release it.

        Runs off the pipeline's critical path deliberately: the cost belongs to
        the port, not to the command that just left it.
        """
        yield self.env.timeout(self.issue_slot_latency)
        self.issue_slots.release(slot)

    def _wake_arbiter(self) -> None:
        """Tell an arbiter blocked in IDLE that work may now exist."""
        if self._arbiter_wake is not None and not self._arbiter_wake.triggered:
            self._arbiter_wake.succeed()

    def _stall(self, reason: str, metric: str):
        """Hold STALL for the declared retry, recording the reason's own metric.

        Only PORT_SCAN -> STALL is declared, so the tenant- and SQ-level stalls
        leave their scan by an edge the template does not describe. That is a gap
        in the contract rather than a choice this model can make correctly: the
        template gives TENANT_SCAN and SQ_SCAN no exit but forward, and a
        candidate blocked on credit or already in flight has to go somewhere.
        Recorded in decisions/arbitration_ip.md.
        """
        self._set_fsm_state("arbiter_main", "STALL")
        self.metrics["stalls"] += 1
        self.metrics[metric] += 1
        self.logger.debug("arbiter stall %s time=%s", reason, self.env.now)
        yield self.env.timeout(self.latency["backpressure_retry"])

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
            # the declared GRANT -> IDLE.
            #
            # IDLE -> PORT_SCAN is declared on `any_port_pending`, and the
            # arbiter waits here until that is true rather than scanning to find
            # out. It used to spin IDLE -> PORT_SCAN -> STALL every 6 cycles with
            # nothing queued, taking the declared edge with its declared
            # condition false and paying a 4-cycle port scan to rediscover what
            # the bitmap it had just refreshed already said -- so `stalls` and
            # `no_port_pending_stalls` measured elapsed idle time rather than
            # arbitration events. An idle arbiter now costs nothing and records
            # nothing.
            #
            # The wait is armed before the condition is re-tested and no
            # simulated time passes between the two, so an enqueue cannot slip
            # into the gap and be missed.
            self._set_fsm_state("arbiter_main", "IDLE")
            while True:
                if self.bitmap_dirty:
                    yield self.env.timeout(self.latency["bitmap"])
                    self._update_pending_bitmaps()
                if any(self.port_pending_bitmap.values()):
                    break
                self._arbiter_wake = self.env.event()
                yield self._arbiter_wake
                self._arbiter_wake = None
            # Each scan decides its own level, in the state the template
            # declares that decision on: the port at the end of PORT_SCAN, the
            # tenant -- and with it the qos_credit_if eligibility sample, whose
            # declared wait point is TENANT_SCAN -- at the end of TENANT_SCAN,
            # the SQ at the end of SQ_SCAN. All three used to run together after
            # SQ_SCAN's timeout, which sampled credit eight cycles late and made
            # a credit-blocked candidate pay all three scans before stalling.
            self._set_fsm_state("arbiter_main", "PORT_SCAN")
            yield self.env.timeout(self.latency["port_scan"])
            candidates = self._pending_ports()
            # PORT_SCAN -> STALL on no_eligible_port, with its declared
            # increment_no_eligible_stall action.
            if not candidates:
                yield from self._stall("no eligible port", "no_port_pending_stalls")
                continue

            # The scan walks the ports in policy order rather than committing to
            # the first pending one. A port is `eligible` only if a grantable
            # candidate can be found beneath it, so a port whose tenants are all
            # credit-blocked must not consume the arbiter: it used to, and one
            # blocked tenant starved every other port indefinitely -- measured at
            # 400 cycles with zero grants while an eligible command sat on the
            # other port.
            port_id = tenant_id = sq_id = None
            for candidate_port in candidates:
                self._set_fsm_state("arbiter_main", "TENANT_SCAN")
                yield self.env.timeout(self.latency["tenant_scan"])
                tenant_id = self._select_tenant(candidate_port)
                if tenant_id is None:
                    continue

                self._set_fsm_state("arbiter_main", "SQ_SCAN")
                yield self.env.timeout(self.latency["sq_scan"])
                sq_id = self._select_sq(tenant_id)
                if sq_id is None:
                    continue
                port_id = candidate_port
                break

            if port_id is None:
                if tenant_id is None:
                    yield from self._stall("no eligible tenant", "no_tenant_pending_stalls")
                else:
                    yield from self._stall("no eligible sq", "no_sq_pending_stalls")
                continue

            selection = self._commit_selection(port_id, tenant_id, sq_id)
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
            # selection_accept is priced on the WAIT_SELECTION ->
            # READ_PENDING_COUNT transition (latency_cycles: 1), not inside
            # READ_PENDING_COUNT, whose own exit edge carries 3. Charging both
            # inside the state made it span 4 -- a figure the template states
            # nowhere -- and a test then pinned that 4 as if it were the
            # contract. The end-to-end total is the same either way; the
            # placement is what the template actually declares.
            yield self.env.timeout(self.latency["selection_accept"])
            # A request the downstream does not accept returns to
            # READ_PENDING_COUNT, which is the retry path the DLD declares for
            # ISSUE_STALL ("retry while selected SQ remains pending"). The
            # re-drive used to loop straight back to ISSUE_REQUEST, an edge no
            # document declares, and it re-issued against a pending count and
            # burst reading it had taken before the stall. Re-reading them is
            # both what the contract says and the safer of the two: the queue
            # and the burst state can move while the downstream is not
            # accepting.
            burst_stalled = False
            while True:
                # The hold comes before the re-read, not after it. issue_if
                # declares that while issue_ready is low the pipeline holds in
                # ISSUE_STALL; re-reading the pending count and burst on a loop
                # during backpressure would leave it cycling through
                # READ_PENDING_COUNT and READ_BURST instead of holding.
                while not self.output_ready:
                    self._set_fsm_state("issue_pipeline", "ISSUE_STALL")
                    self.metrics["output_stalls"] += 1
                    self.metrics["output_backpressure_cycles"] += 1
                    self.logger.warning("output backpressure selection=%s time=%s", selection, self.env.now)
                    yield self.env.timeout(1)

                self._set_fsm_state("issue_pipeline", "READ_PENDING_COUNT")
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
                    burst_stalled = True
                    break

                # issue_if is wait_for_ack_inline at issue_pipeline.ISSUE_REQUEST,
                # resuming on issue_ready: commands are popped only when
                # issue_ready is true, and queue entries remain pending during
                # downstream backpressure.
                slot = self.issue_slots.request()
                yield slot
                self._set_fsm_state("issue_pipeline", "ISSUE_REQUEST")
                yield self.env.timeout(self.latency["issue"])
                if self.output_ready:
                    break

                # ISSUE_REQUEST -> ISSUE_STALL: the downstream did not accept.
                # The slot is released at once rather than after the port's
                # latency -- nothing left through it, so there is nothing for it
                # to carry out.
                self.issue_slots.release(slot)
                self._set_fsm_state("issue_pipeline", "ISSUE_STALL")
                self.metrics["output_stalls"] += 1
                self.metrics["output_backpressure_cycles"] += 1
                self.logger.warning("issue_ready dropped during request selection=%s time=%s", selection, self.env.now)
                yield self.env.timeout(1)
                # back to the top: it holds here while issue_ready stays low,
                # and re-reads only once the downstream is ready again.

            if burst_stalled:
                continue

            issued_cmds = []
            queue = self.queues[selection["port_id"]][selection["tenant_id"]][selection["sq_id"]]
            for _ in range(issue_count):
                command = queue.popleft()
                issued_cmds.append(command)
                self.issued.append((self.env.now, command))

            self.env.process(self._release_issue_slot(slot))

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
            self._set_fsm_state("policy_update", "WAIT_GRANT")
