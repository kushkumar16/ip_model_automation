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
        port_latency=1,
        reset_latency=1,
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
        self.port_latency = port_latency
        self.reset_latency = reset_latency
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
        self._pending_snapshot: Dict[str, float] = {"time": 0.0, "completed": 0.0}
        # The set of tenants with active traffic and available tokens. The
        # scheduler leaves IDLE only while it is non-empty, so it does not pay an
        # 8-cycle tenant select to discover that nothing can be served. Kept
        # incrementally -- each event that can change one tenant's eligibility
        # refreshes that tenant -- rather than rescanned.
        self.eligible_tenants: set = set()
        self._scheduler_wake = None
        # Published so the interface wait points are observable: each interface's
        # wait model names the <fsm>.<STATE> this model must be sitting in while
        # the requester waits on it.
        self.fsm_state = {
            "accept": "RESET",
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
        self._refresh_eligibility(tenant_id)
        self.logger.info("configured tenant=%s alive=%s", tenant_id, alive)

    def set_tenant_alive(self, tenant_id: str, alive: bool) -> None:
        """Toggle a tenant's liveness after its initial configure_tenant.

        A write straight to tenant_alive -- the only way to express this,
        since qos_config_if's transaction has no field for it on its own --
        leaves eligible_tenants stale in both directions: a tenant marked
        dead stays selectable, or one marked alive again is never noticed.
        _refresh_eligibility keeps the two in sync and wakes a scheduler
        that was waiting on an empty eligible set.
        """
        self.tenant_alive[tenant_id] = alive
        self._refresh_eligibility(tenant_id)
        self.logger.info("tenant_alive=%s tenant=%s", alive, tenant_id)

    def credit_tokens(self, tenant_id: str, **fields: float) -> None:
        """Restore or adjust specific token axes outside configure_tenant/refill.

        The five declared token axes are the only fields TOKEN_NAMES has;
        unnamed fields keep their current value. This is how a caller models
        a token grant that is not a full reconfiguration and not a refill
        window boundary -- an external credit, say. Refreshes eligibility so
        a tenant that regains tokens on every axis its pending work needs is
        selected again, rather than left permanently excluded the way a raw
        `model.tokens[tenant_id][...] = ...` write would.
        """
        unknown = set(fields) - set(TOKEN_NAMES)
        if unknown:
            raise ValueError(f"unknown token field(s) {sorted(unknown)}")
        self.tokens[tenant_id].update(fields)
        self._refresh_eligibility(tenant_id)
        self.logger.debug("tokens credited tenant=%s %s", tenant_id, dict(self.tokens[tenant_id]))

    def _ensure_tenant(self, tenant_id: str, weight: int = 1) -> None:
        if tenant_id not in self.tenant_weights:
            self.tenant_weights[tenant_id] = max(1, int(weight))
            self.tenant_order = WeightedOrder(self.tenant_weights)

    def set_completion_ready(self, ready: bool) -> None:
        self.output_ready = ready
        self.logger.info("completion_ready=%s", ready)

    def submit(self, command: Command):
        """Offer a command on accepted_cmd_if, returning the peer's accept.

        accepted_cmd_if is `wait_for_ack_inline` with its wait point at
        `accept.ENQUEUE`: the producer needs no result, only the accept. This
        used to return the `input_q.put` event, which an unbounded Store
        completes at the instant of submission -- so the peer was acked at t=0
        whether or not `accept` was sitting in BACKPRESSURE, and a full pending
        queue stalled nothing upstream. The returned event now completes where
        the template says it does: when the command lands in the pending queue,
        at the end of ENQUEUE.
        """
        self._ensure_tenant(command.tenant_id)
        self.logger.info("submit cmd=%s kind=%s tenant=%s", command.cmd_id, command.kind, command.tenant_id)
        accepted = self.env.event()
        self.input_q.put((command, accepted))
        return accepted

    def accept_process(self):
        # RESET -> READY is declared with reset_deasserted, and ip.reset.behavior
        # is clear_pending_queues_tokens_and_metrics. The constructor did that
        # work and the process opened in READY, so RESET was never entered and
        # the declared transition was never taken -- the reset was implemented
        # as construction rather than modelled. arbitration_ip holds its RESET;
        # this now does too.
        self.fsm_state["accept"] = "RESET"
        yield self.env.timeout(self.reset_latency)
        self._clear_state()
        while True:
            self.fsm_state["accept"] = "READY"
            command, accepted = yield self.input_q.get()

            # BACKPRESSURE is declared for incoming_valid_and_queue_full, and
            # only that. It used to be set when the tenant was not alive, which
            # is a different condition the template does not name here -- and in
            # that branch the command was appended to pending anyway, so it was
            # an enqueue wearing the label of a stall.
            backpressured = False
            while self._pending_queue_full(command.tenant_id):
                backpressured = True
                self.fsm_state["accept"] = "BACKPRESSURE"
                self.metrics["queue_full_stalls"] += 1
                self.logger.warning("pending queue full tenant=%s cmd=%s", command.tenant_id, command.cmd_id)
                yield self.env.timeout(self.retry_latency)

            if backpressured:
                # BACKPRESSURE -> READY on queue_space_available, then
                # READY -> ENQUEUE. The model used to fall straight from
                # BACKPRESSURE into ENQUEUE, so the declared resumption was
                # never taken and the state that says "ready to accept" was
                # skipped on exactly the pass where a producer is watching for
                # it. The test that claimed to check the resumption could not
                # tell the two apart.
                self.fsm_state["accept"] = "READY"
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
            # The wait point completes here, not at submission.
            accepted.succeed()
            self._refresh_eligibility(command.tenant_id)
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

    def _clear_state(self) -> None:
        """ip.reset.behavior: clear pending queues, tokens and metrics.

        Two of the three are cleared here. Token state is not, and that is a
        deliberate gap rather than an oversight: in this model `tokens` is
        written by `configure_tenant` over qos_config_if, which callers apply
        before the simulation starts. A runtime reset that zeroed it would
        discard the configuration the run was set up with, and one that restored
        it from base would undo a caller that had deliberately drained a tenant.
        Either way the reset would be overwriting configuration rather than
        modelling a reset. Recorded in decisions/completion_ip.md.
        """
        self.pending.clear()
        self.metrics.clear()
        self.window_metrics.clear()
        self.completed.clear()
        self.eligible_tenants.clear()
        self._refresh_all_eligibility()

    def _tenant_eligible(self, tenant_id: str) -> bool:
        """Active traffic and available tokens, per the DLD's section 7.

        Deliberately a tenant-level test, not a command-level one: it asks
        whether the tenant holds *any* tokens on the axes its head command
        consumes, not whether it can afford that command. A tenant can therefore
        be eligible while its head command still costs more than the tokens
        left, which is what keeps CHECK_TOKENS -> WAIT_TOKENS reachable --
        affordability is decided there, not here.
        """
        if not self.tenant_alive[tenant_id] or not self.pending[tenant_id]:
            return False
        costs = self._costs(self.pending[tenant_id][0])
        return all(self.tokens[tenant_id][name] > 0 for name in costs)

    def _refresh_eligibility(self, tenant_id: str) -> None:
        """Re-evaluate one tenant and wake a scheduler waiting in IDLE."""
        if self._tenant_eligible(tenant_id):
            self.eligible_tenants.add(tenant_id)
            if self._scheduler_wake is not None and not self._scheduler_wake.triggered:
                self._scheduler_wake.succeed()
        else:
            self.eligible_tenants.discard(tenant_id)

    def _refresh_all_eligibility(self) -> None:
        for tenant_id in list(self.pending) + list(self.tokens):
            self._refresh_eligibility(tenant_id)

    def _select_tenant(self, skip_inactive: bool = False) -> Optional[str]:
        """Pick the next tenant with work.

        `skip_inactive` passes over tenants that are not alive rather than
        selecting them. The scheduler sets it: `tenant_inactive` is declared as a
        *command* error condition, not as a condition on any completion_scheduler
        transition, and the only declared entry to WAIT_TOKENS is
        `required_tokens_unavailable`. The scheduler used to select a dead tenant
        and then sit in WAIT_TOKENS -- the state whose one declared meaning is
        "the tenant cannot pay" -- while the tenant could pay in full.

        step_functional leaves it False: that API's job is to name which stall
        kind it hit, so it needs the selection before the liveness test.
        """

        def usable(tenant_id: str) -> bool:
            if not self.pending[tenant_id]:
                return False
            if skip_inactive and not self.tenant_alive[tenant_id]:
                self.metrics["tenant_inactive_stalls"] += 1
                self.logger.warning("tenant inactive, passed over tenant=%s", tenant_id)
                return False
            if skip_inactive and tenant_id not in self.eligible_tenants:
                return False
            return True

        for tenant_id in self.tenant_order.scan():
            if usable(tenant_id):
                self.tenant_order.advance_to_after(tenant_id)
                return tenant_id
        for tenant_id in sorted(self.pending):
            if usable(tenant_id):
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
            # IDLE -> SELECT_TENANT on any_eligible_tenant. The scheduler waits
            # here until a tenant has both active traffic and available tokens,
            # rather than paying an 8-cycle select to find out there is nothing
            # to serve.
            #
            # This is what puts the declared no_stall_completion path at 21
            # rather than 17: a tenant cannot become eligible before its command
            # has been enqueued, so the 4-cycle enqueue precedes the select
            # instead of overlapping it. The DLD states both the 21 and the
            # ordering it comes from.
            #
            # The wait is armed before the set is re-tested, with no simulated
            # time between, so an enqueue cannot fall into the gap.
            #
            # It sits outside the inner loop deliberately: a token stall returns
            # to SELECT_TENANT and so does an emit with another eligible tenant
            # waiting, both of which the template declares. Putting the wait at
            # the top of the inner loop routed them through IDLE and made both
            # declared transitions dead.
            self.fsm_state["completion_scheduler"] = "IDLE"
            while not self.eligible_tenants:
                self._scheduler_wake = self.env.event()
                if self.eligible_tenants:
                    self._scheduler_wake = None
                    break
                yield self._scheduler_wake
                self._scheduler_wake = None

            while True:
                self.fsm_state["completion_scheduler"] = "SELECT_TENANT"
                yield self.env.timeout(self.tenant_select_latency)
                tenant_id = self._select_tenant(skip_inactive=True)
                if tenant_id is None:
                    # SELECT_TENANT -> IDLE on candidate_lost_eligibility:
                    # eligibility was true when IDLE was left and is not now --
                    # a configure_tenant call or a concurrent change moved it
                    # during the 8-cycle select. Back to the wait rather than
                    # spinning the select.
                    self.metrics["stalls"] += 1
                    yield self.env.timeout(self.retry_latency)
                    break

                command = self.pending[tenant_id][0]
                self.fsm_state["completion_scheduler"] = "CHECK_TOKENS"
                yield self.env.timeout(self.token_check_latency)
                costs = self._costs(command)
                if not self._can_pay(tenant_id, costs):
                    self.fsm_state["completion_scheduler"] = "WAIT_TOKENS"
                    self.metrics["token_stalls"] += 1
                    self.logger.warning("token stall tenant=%s cmd=%s costs=%s", tenant_id, command.cmd_id, costs)
                    yield self.env.timeout(self.retry_latency)
                    # WAIT_TOKENS -> SELECT_TENANT, which used to be unreachable:
                    # every stall re-entered the loop at IDLE instead.
                    continue

                self.fsm_state["completion_scheduler"] = "EMIT"
                while not self.output_ready:
                    # completion_queue_if is wait_for_ack_inline: while cpl_ready
                    # is low the completion is held here rather than emitted.
                    self.fsm_state["completion_scheduler"] = "STALL_OUTPUT"
                    self.metrics["output_stalls"] += 1
                    self.logger.warning("output stall tenant=%s", tenant_id)
                    yield self.env.timeout(self.retry_latency)
                    self.fsm_state["completion_scheduler"] = "EMIT"

                # The debit belongs immediately before the emit it pays for. It
                # used to run before the output test, which stranded it for the
                # whole stall -- a tenant could end a run drained to zero having
                # completed nothing -- and a refill arriving during that stall
                # credited it back, so the completion was finally emitted having
                # spent nothing. Re-checking here keeps the declared invariant
                # "a completion is emitted only after required tokens are
                # debited" true of the token accounting, not just of the source
                # order. No second affordability test is needed and none is
                # added: this process is the only writer that spends tokens, and
                # a refill only ever raises them, so what was affordable at
                # CHECK_TOKENS is still affordable here. Re-testing would add an
                # EMIT -> WAIT_TOKENS edge the template does not declare and
                # nothing could reach.
                self._debit(tenant_id, costs)
                self.pending[tenant_id].popleft()
                # completion_output_port: capacity 1, declared latency 1 cycle.
                # That cycle was charged nowhere, and charging it inline would
                # make the declared no_stall_completion path 18 rather than the
                # 17 the template states and the model matches. So the port pays
                # it in the background: the scheduler holds the port across the
                # emit and hands the release to a process that lets the port's
                # own latency elapse first. The completion is done at the end of
                # the emit -- the end-to-end path is unchanged -- while the port
                # stays busy a cycle longer, which is where a capacity of 1 is
                # supposed to be felt.
                port = self.completion_output_port.request()
                yield port
                yield self.env.timeout(self.emit_latency)
                self.env.process(self._release_output_port(port))
                self.completed.append((self.env.now, command))
                self.metrics[f"completed_{command.kind.lower()}"] += 1
                self.metrics["completed_commands"] += 1
                self._refresh_eligibility(tenant_id)
                self.logger.info(
                    "completed cmd=%s kind=%s tenant=%s time=%s",
                    command.cmd_id,
                    command.kind,
                    tenant_id,
                    self.env.now,
                )
                # EMIT -> SELECT_TENANT on more_candidates, or EMIT -> IDLE
                # when there are none. Both are declared; the DLD names the
                # second and the template dropped it.
                if not self.eligible_tenants:
                    break

    def _release_output_port(self, port):
        """Let the port's declared latency elapse, then release it.

        Runs off the scheduler's critical path deliberately: the cost belongs to
        the port, not to the completion that just left it.
        """
        yield self.env.timeout(self.port_latency)
        self.completion_output_port.release(port)

    def refill_once(self) -> None:
        """Restore base tokens and snapshot the window, all at once.

        The whole-window shortcut, for callers driving the model directly. The
        process form below splits these three into the transitions the template
        declares them on; this keeps them together for a single explicit call.
        """
        self._snapshot_window_usage()
        self._restore_base_tokens()
        self._write_window_metrics(self._pending_snapshot)

    def _snapshot_window_usage(self) -> Dict[str, float]:
        """Declared action on WAIT_WINDOW -> ASSESS_USAGE."""
        self._pending_snapshot = {
            "time": float(self.env.now),
            "completed": float(self.metrics["completed_commands"]),
        }
        return self._pending_snapshot

    def _restore_base_tokens(self) -> None:
        """Declared action on ASSESS_USAGE -> REFILL_BASE."""
        for tenant_id, base in self.base_tokens.items():
            self.tokens[tenant_id].update(base)
        self._refresh_all_eligibility()

    def _write_window_metrics(self, snapshot: Dict[str, float]) -> None:
        """Declared action on REFILL_BASE -> PUBLISH_METRICS."""
        self.window_metrics.append(snapshot)
        self.metrics["refill_windows"] += 1
        self.logger.info("refill window time=%s", self.env.now)

    def refill_process(self):
        """Run the refill FSM, performing each action on its declared transition.

        All three actions used to run together after PUBLISH_METRICS had already
        finished -- the states were held across their costs, but nothing they
        were declared to *do* happened inside them. Base tokens came back 20
        cycles after the model had left REFILL_BASE, so a starved scheduler
        stayed starved 20 cycles past the point the contract says it was funded,
        and the window snapshot was timestamped 40 cycles after the boundary it
        claims to describe, attributing the next window's completions to this
        one.
        """
        while True:
            self.fsm_state["refill"] = "WAIT_WINDOW"
            yield self.env.timeout(self.refill_window)
            snapshot = self._snapshot_window_usage()

            self.fsm_state["refill"] = "ASSESS_USAGE"
            yield self.env.timeout(self.usage_assessment_latency)
            self._restore_base_tokens()

            # _restore_base_tokens() (called above, in ASSESS_USAGE) is a
            # periodic top-up of already-configured tenants' tokens back to
            # base level -- it is not where a pending configure_tenant call
            # takes effect. configure_tenant applies its values immediately,
            # at call time; qos_config_if's declared wait_for_ack_inline gate
            # at this state is not modelled -- see M33 in
            # decisions/completion_ip.md.
            self.fsm_state["refill"] = "REFILL_BASE"
            yield self.env.timeout(self.base_refill_latency)
            self._write_window_metrics(snapshot)

            self.fsm_state["refill"] = "PUBLISH_METRICS"
            yield self.env.timeout(self.metrics_publish_latency)
