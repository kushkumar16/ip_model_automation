from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

import simpy

from .common import Command, WeightedOrder, get_ip_logger

TOKEN_NAMES = ("read", "write", "read_bw", "write_bw")


class CompletionIpModel:
    def __init__(
        self,
        env: simpy.Environment,
        service_latency=4,
        tenant_select_latency=8,
        token_check_latency=5,
        emit_latency=4,
        retry_latency=1,
        refill_window=5_000_000,
        usage_assessment_latency=20,
        base_refill_latency=10,
        metrics_publish_latency=10,
        pending_depth: Optional[int] = None,
        tenant_weights: Optional[Dict[str, int]] = None,
        start_refill_process=False,
        log_level: str = "WARNING",
        log_file: str = "run.log",
    ):
        self.env = env
        self.logger = get_ip_logger("completion_ip", log_level, log_file)
        self.service_latency = service_latency
        self.tenant_select_latency = tenant_select_latency
        self.token_check_latency = token_check_latency
        self.emit_latency = emit_latency
        self.retry_latency = retry_latency
        self.refill_window = refill_window
        self.usage_assessment_latency = usage_assessment_latency
        self.base_refill_latency = base_refill_latency
        self.metrics_publish_latency = metrics_publish_latency
        # accept declares READY -> BACKPRESSURE on incoming_valid_and_queue_full,
        # and the only declared queue is tenant_pending_queues at depth
        # unbounded -- which can never be full, so the declared transition was
        # unreachable by construction. The depth stays unset by default, keeping
        # the template's `unbounded` true, and becomes configuration a caller can
        # bound. Same shape as any other value the contract leaves open: make it
        # settable rather than invent a number.
        self.pending_depth = pending_depth

        # completion_output_port: declared capacity 1, one completion per
        # dispatch tick. There was no resource at all, so the declared
        # contention did not exist.
        self.completion_output_port = simpy.Resource(env, capacity=1)

        self.input_q = simpy.Store(env)
        self.pending: Dict[str, Deque[Command]] = defaultdict(deque)
        self.tenant_weights = dict(tenant_weights or {"T0": 1})
        self.tenant_order = WeightedOrder(self.tenant_weights)
        self.tokens = defaultdict(self._new_token_bucket)
        self.base_tokens = defaultdict(self._new_token_bucket)
        self.tenant_alive = defaultdict(lambda: True)
        self.output_ready = True
        self.completed: List[tuple[float, Command]] = []
        self.metrics = defaultdict(int)
        self.window_metrics: List[Dict[str, float]] = []
        # Published so the interface wait points are observable: each interface's
        # wait model names the <fsm>.<STATE> this model must be sitting in while
        # the requester waits on it.
        self.fsm_state = {
            "accept": "READY",
            "completion_scheduler": "IDLE",
            "refill": "WAIT_WINDOW",
        }

        self.env.process(self.accept_process())
        self.env.process(self.completion_scheduler())
        if start_refill_process:
            self.env.process(self.refill_process())
        self.logger.info("initialized refill_window=%s", self.refill_window)

    @staticmethod
    def _new_token_bucket() -> Dict[str, float]:
        return {name: 0.0 for name in TOKEN_NAMES}

    def configure_tenant(
        self,
        tenant_id: str,
        read: float,
        write: float,
        read_bw: float = 1000.0,
        write_bw: float = 1000.0,
        weight: int = 1,
        alive: bool = True,
    ) -> None:
        self._ensure_tenant(tenant_id, weight)
        base = {"read": read, "write": write, "read_bw": read_bw, "write_bw": write_bw}
        self.tokens[tenant_id].update(base)
        self.base_tokens[tenant_id].update(base)
        self.tenant_alive[tenant_id] = alive
        self.logger.info("configured tenant=%s alive=%s", tenant_id, alive)

    def _ensure_tenant(self, tenant_id: str, weight: int = 1) -> None:
        if tenant_id not in self.tenant_weights:
            self.tenant_weights[tenant_id] = max(1, int(weight))
            self.tenant_order = WeightedOrder(self.tenant_weights)

    def set_completion_ready(self, ready: bool) -> None:
        self.output_ready = ready
        self.logger.info("completion_ready=%s", ready)

    def submit(self, command: Command):
        self._ensure_tenant(command.tenant_id)
        self.logger.info("submit cmd=%s kind=%s tenant=%s", command.cmd_id, command.kind, command.tenant_id)
        return self.input_q.put(command)

    def accept_process(self):
        while True:
            self.fsm_state["accept"] = "READY"
            command = yield self.input_q.get()

            # BACKPRESSURE is declared for incoming_valid_and_queue_full, and
            # only that. It used to be set when the tenant was not alive, which
            # is a different condition the template does not name here -- and in
            # that branch the command was appended to pending anyway, so it was
            # an enqueue wearing the label of a stall.
            while self._pending_queue_full(command.tenant_id):
                self.fsm_state["accept"] = "BACKPRESSURE"
                self.metrics["queue_full_stalls"] += 1
                self.logger.warning("pending queue full tenant=%s cmd=%s", command.tenant_id, command.cmd_id)
                yield self.env.timeout(self.retry_latency)

            # accepted_cmd_if is wait_for_ack_inline: the producer's accept
            # completes here, as the command lands in the pending queue. The
            # state is entered before the enqueue is charged so that it is held
            # for the whole operation -- it used to be assigned after the cost
            # and overwritten by READY on the next pass with no yield between,
            # so the declared wait point existed at no simulated time.
            self.fsm_state["accept"] = "ENQUEUE"
            yield self.env.timeout(self.service_latency)
            self.pending[command.tenant_id].append(command)
            if not self.tenant_alive[command.tenant_id]:
                # The command is parked, not accepted: tenant_inactive is one of
                # this command's declared error_conditions, and accept declares
                # no state of its own for it.
                self.metrics["tenant_inactive_stalls"] += 1
                self.logger.warning("tenant inactive cmd=%s tenant=%s", command.cmd_id, command.tenant_id)
            else:
                self.metrics["accepted_commands"] += 1
                self.logger.debug("accepted cmd=%s tenant=%s time=%s", command.cmd_id, command.tenant_id, self.env.now)

    def _pending_queue_full(self, tenant_id: str) -> bool:
        """Whether this tenant's pending queue is at its configured bound.

        Unbounded unless a caller sets pending_depth, which is what the template
        declares.
        """
        return self.pending_depth is not None and len(self.pending[tenant_id]) >= self.pending_depth

    def _costs(self, command: Command) -> Dict[str, float]:
        if command.kind == "READ":
            return {"read": 1.0, "read_bw": float(command.size_kb)}
        if command.kind == "FLUSH":
            return {}
        return {"write": 1.0, "write_bw": float(command.size_kb)}

    def _select_tenant(self) -> Optional[str]:
        for tenant_id in self.tenant_order.scan():
            if self.pending[tenant_id]:
                self.tenant_order.advance_to_after(tenant_id)
                return tenant_id
        for tenant_id in sorted(self.pending):
            if self.pending[tenant_id]:
                return tenant_id
        return None

    def _can_pay(self, tenant_id: str, costs: Dict[str, float]) -> bool:
        return all(self.tokens[tenant_id][name] >= cost for name, cost in costs.items())

    def _debit(self, tenant_id: str, costs: Dict[str, float]) -> None:
        for name, cost in costs.items():
            self.tokens[tenant_id][name] -= cost

    def step_functional(self) -> Optional[Command]:
        tenant_id = self._select_tenant()
        if tenant_id is None:
            return None
        command = self.pending[tenant_id][0]
        if not self.tenant_alive[tenant_id]:
            self.metrics["tenant_inactive_stalls"] += 1
            self.logger.warning("tenant inactive stall tenant=%s", tenant_id)
            return None
        if not self.output_ready:
            self.metrics["output_stalls"] += 1
            self.logger.warning("output stall tenant=%s", tenant_id)
            return None
        costs = self._costs(command)
        if not self._can_pay(tenant_id, costs):
            self.metrics["token_stalls"] += 1
            self.logger.warning("token stall tenant=%s cmd=%s costs=%s", tenant_id, command.cmd_id, costs)
            return None
        self._debit(tenant_id, costs)
        return self.pending[tenant_id].popleft()

    def completion_scheduler(self):
        while True:
            self.fsm_state["completion_scheduler"] = "IDLE"
            # SELECT_TENANT is entered before its cost is charged, so it is held
            # for the whole selection. It used to be assigned after the timeout
            # and replaced by CHECK_TOKENS with no yield between, so it existed
            # at no simulated time on the completion path.
            self.fsm_state["completion_scheduler"] = "SELECT_TENANT"
            yield self.env.timeout(self.tenant_select_latency)
            tenant_id = self._select_tenant()
            if tenant_id is None:
                self.metrics["stalls"] += 1
                yield self.env.timeout(self.retry_latency)
                continue

            command = self.pending[tenant_id][0]
            self.fsm_state["completion_scheduler"] = "CHECK_TOKENS"
            yield self.env.timeout(self.token_check_latency)
            if not self.tenant_alive[tenant_id]:
                self.fsm_state["completion_scheduler"] = "WAIT_TOKENS"
                self.metrics["tenant_inactive_stalls"] += 1
                self.logger.warning("tenant inactive stall tenant=%s", tenant_id)
                yield self.env.timeout(self.retry_latency)
                continue
            costs = self._costs(command)
            if not self._can_pay(tenant_id, costs):
                self.fsm_state["completion_scheduler"] = "WAIT_TOKENS"
                self.metrics["token_stalls"] += 1
                self.logger.warning("token stall tenant=%s cmd=%s costs=%s", tenant_id, command.cmd_id, costs)
                yield self.env.timeout(self.retry_latency)
                continue

            # CHECK_TOKENS -> EMIT is declared on
            # required_tokens_available_and_output_ready, and EMIT -> STALL_OUTPUT
            # on output_not_ready. The output check used to sit before the token
            # check, so STALL_OUTPUT was entered from CHECK_TOKENS with the
            # tokens neither verified nor debited.
            self._debit(tenant_id, costs)
            self.fsm_state["completion_scheduler"] = "EMIT"
            while not self.output_ready:
                # completion_queue_if is wait_for_ack_inline: while cpl_ready is
                # low the completion is held here rather than emitted.
                self.fsm_state["completion_scheduler"] = "STALL_OUTPUT"
                self.metrics["output_stalls"] += 1
                self.logger.warning("output stall tenant=%s", tenant_id)
                yield self.env.timeout(self.retry_latency)
                self.fsm_state["completion_scheduler"] = "EMIT"
            self.pending[tenant_id].popleft()
            # completion_output_port: capacity 1, one completion per dispatch
            # tick. Held across the emit so the declared contention is real.
            with self.completion_output_port.request() as port:
                yield port
                yield self.env.timeout(self.emit_latency)
            self.completed.append((self.env.now, command))
            self.metrics[f"completed_{command.kind.lower()}"] += 1
            self.metrics["completed_commands"] += 1
            self.logger.info(
                "completed cmd=%s kind=%s tenant=%s time=%s", command.cmd_id, command.kind, tenant_id, self.env.now
            )

    def refill_once(self) -> None:
        """Restore base tokens and snapshot the window.

        The state sequence belongs to refill_process, which holds each declared
        state across its declared cost. This used to set all three here, back to
        back with no yield possible -- refill_once is not a generator -- so the
        costs were charged as three bare timeouts before it ran and none of the
        states existed at any simulated time.
        """
        snapshot = {"time": float(self.env.now), "completed": float(self.metrics["completed_commands"])}
        for tenant_id, base in self.base_tokens.items():
            self.tokens[tenant_id].update(base)
        self.window_metrics.append(snapshot)
        self.metrics["refill_windows"] += 1
        self.logger.info("refill window time=%s", self.env.now)

    def refill_process(self):
        while True:
            self.fsm_state["refill"] = "WAIT_WINDOW"
            yield self.env.timeout(self.refill_window)
            self.fsm_state["refill"] = "ASSESS_USAGE"
            yield self.env.timeout(self.usage_assessment_latency)
            # qos_config_if is wait_for_ack_inline: the configured budgets take
            # effect here, at the window boundary.
            self.fsm_state["refill"] = "REFILL_BASE"
            yield self.env.timeout(self.base_refill_latency)
            self.fsm_state["refill"] = "PUBLISH_METRICS"
            yield self.env.timeout(self.metrics_publish_latency)
            self.refill_once()
