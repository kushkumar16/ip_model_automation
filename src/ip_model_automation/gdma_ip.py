from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional

import simpy

from .common import Descriptor, get_ip_logger


class GdmaIpModel:
    def __init__(
        self,
        env: simpy.Environment,
        fetch_latency=53,
        read_latency=22,
        write_latency=20,
        completion_latency=32,
        channel_scan_latency=12,
        credit_check_latency=4,
        irq_latency=8,
        outstanding_read_depth=None,
        buffer_depth=16,
        descriptor_queue_depth=4,
        completion_queue_depth=16,
        coalesce_threshold=1,
        log_level: str = "WARNING",
        log_file: str = "run.log",
    ):
        self.env = env
        self.logger = get_ip_logger("gdma_ip", log_level, log_file)
        self.lat = {
            "fetch": fetch_latency,
            "read": read_latency,
            "write": write_latency,
            "completion": completion_latency,
            "channel_scan": channel_scan_latency,
            "credit_check": credit_check_latency,
            "irq": irq_latency,
        }
        # §8 Resources names "per-channel outstanding read credits", §6.4 says read
        # issue "can run ahead of write issue until internal buffer or outstanding
        # read limit is full", and §3's CH_CFG carries the per-channel outstanding
        # depth. The *value* of that depth is configuration, so it is set per
        # channel and left unbounded until it is, rather than inventing a number
        # the DLD does not state.
        self.outstanding_read_depth = outstanding_read_depth
        self.channel_read_depth: Dict[int, Optional[int]] = {}
        self.outstanding_reads: Dict[int, int] = defaultdict(int)
        self.read_credit_available: Dict[int, simpy.Event] = {}
        self.coalesce_threshold = coalesce_threshold

        self.desc_q = simpy.Store(env)
        self.active_descriptor_queue = simpy.Store(env, capacity=descriptor_queue_depth)
        self.read_issue_queue = simpy.Store(env, capacity=descriptor_queue_depth)
        self.read_response_queue = simpy.Store(env)
        self.internal_data_buffer = simpy.Store(env, capacity=buffer_depth)
        self.write_response_queue = simpy.Store(env)
        self.descriptor_done_queue = simpy.Store(env)
        self.completion_pending_queue = simpy.Store(env, capacity=completion_queue_depth)

        self.descriptor_read_port = simpy.Resource(env, capacity=1)
        self.source_read_port = simpy.Resource(env, capacity=1)
        self.destination_write_port = simpy.Resource(env, capacity=1)
        self.completion_write_port = simpy.Resource(env, capacity=1)

        self.enabled = set()
        self.channel_state: Dict[int, str] = defaultdict(lambda: "DISABLED")
        self.channel_priority: Dict[int, int] = defaultdict(lambda: 0)
        self.source_ready = True
        self.destination_ready = True
        self.completion_ready = True

        self.completed_ids: List[str] = []
        self.irqs: List[int] = []
        # interrupt_if is wait_for_ack_before_next_request with outstanding_limit 1:
        # one coalesced IRQ is outstanding until software clears it.
        self.irq_clear_event: simpy.Event | None = None
        self.errors: List[str] = []
        self.completed: List[tuple[float, Descriptor]] = []
        self.metrics = defaultdict(int)
        self.descriptor_start: Dict[str, float] = {}
        self.fsm_state = {
            "channel_control": "DISABLED",
            "descriptor_fetch": "WAIT_DESC",
            "channel_scheduler": "SCAN_CHANNELS",
            "read_issue": "WAIT_DESCRIPTOR",
            "read_response": "WAIT_READ_RESP",
            "write_issue": "WAIT_BUFFER_DATA",
            "write_response": "WAIT_WRITE_RESP",
            "completion_update": "WAIT_DESCRIPTOR_DONE",
            "interrupt_coalescing": "IDLE",
        }

        self.env.process(self.channel_control_process())
        self.env.process(self.descriptor_fetch_process())
        self.env.process(self.channel_scheduler_process())
        self.env.process(self.read_issue_process())
        self.env.process(self.read_response_process())
        self.env.process(self.write_issue_process())
        self.env.process(self.write_response_process())
        self.env.process(self.completion_update_process())
        self.env.process(self.interrupt_coalescing_process())
        self.logger.info("initialized coalesce_threshold=%s", self.coalesce_threshold)

    def configure_channel(
        self,
        channel_id: int,
        enabled: bool = True,
        priority: int = 0,
        outstanding_read_depth: Optional[int] = None,
    ) -> None:
        """CH_CFG: per-channel priority, outstanding depth, burst size (DLD §3).

        `priority` is stored and **not acted on** -- see `channel_scheduler_process`.
        """
        self.channel_priority[channel_id] = priority
        if outstanding_read_depth is not None:
            self.channel_read_depth[channel_id] = outstanding_read_depth
        if enabled:
            self.enable_channel(channel_id)
        else:
            self.enabled.discard(channel_id)
            self.channel_state[channel_id] = "DISABLED"

    def enable_channel(self, channel_id: int) -> None:
        self.enabled.add(channel_id)
        self.channel_state[channel_id] = "IDLE"
        self.logger.info("channel enabled channel=%s", channel_id)

    def halt_channel(self, channel_id: int) -> None:
        self.enabled.discard(channel_id)
        self.channel_state[channel_id] = "HALTED"
        self.logger.warning("channel halted channel=%s", channel_id)

    def set_memory_ready(
        self, source: bool | None = None, destination: bool | None = None, completion: bool | None = None
    ) -> None:
        if source is not None:
            self.source_ready = source
        if destination is not None:
            self.destination_ready = destination
        if completion is not None:
            self.completion_ready = completion
        self.logger.info(
            "memory_ready source=%s destination=%s completion=%s",
            self.source_ready,
            self.destination_ready,
            self.completion_ready,
        )

    def clear_interrupt(self) -> None:
        """Software INT_STATUS clear: releases the coalescing process from WAIT_CLEAR."""
        self.irqs.clear()
        self.metrics["irq_clears"] += 1
        if self.irq_clear_event is not None and not self.irq_clear_event.triggered:
            self.irq_clear_event.succeed()
            self.irq_clear_event = None
        self.logger.info("irq cleared time=%s", self.env.now)

    def submit(self, desc: Descriptor):
        self.descriptor_start[desc.desc_id] = self.env.now
        self.logger.info("submit descriptor=%s channel=%s length=%s", desc.desc_id, desc.channel_id, desc.length_bytes)
        return self.desc_q.put(desc)

    def validate_descriptor(self, desc: Descriptor) -> bool:
        if desc.channel_id not in self.enabled:
            self.errors.append("channel_disabled")
            self.channel_state[desc.channel_id] = "ERROR"
            self.logger.error(
                "descriptor rejected disabled channel descriptor=%s channel=%s", desc.desc_id, desc.channel_id
            )
            return False
        if desc.length_bytes <= 0:
            self.errors.append("bad_descriptor")
            self.channel_state[desc.channel_id] = "ERROR"
            self.logger.error("descriptor rejected bad length descriptor=%s length=%s", desc.desc_id, desc.length_bytes)
            return False
        return True

    def channel_control_process(self):
        while True:
            self.fsm_state["channel_control"] = "IDLE" if self.enabled else "DISABLED"
            for channel_id in list(self.enabled):
                if self.channel_state[channel_id] == "IDLE":
                    self.metrics["channel_control_idle_ticks"] += 1
                elif self.channel_state[channel_id] == "ACTIVE":
                    self.metrics["channel_active_ticks"] += 1
            yield self.env.timeout(4)

    def descriptor_fetch_process(self):
        while True:
            self.fsm_state["descriptor_fetch"] = "WAIT_DESC"
            desc = yield self.desc_q.get()
            self.fsm_state["descriptor_fetch"] = "ISSUE_FETCH"
            with self.descriptor_read_port.request() as req:
                yield req
                # descriptor_read_if is wait_for_response: the fetch blocks here
                # until the descriptor comes back, because its fields decide what
                # is validated and enqueued next.
                self.fsm_state["descriptor_fetch"] = "WAIT_FETCH_RESP"
                yield self.env.timeout(self.lat["fetch"])
            self.fsm_state["descriptor_fetch"] = "VALIDATE"
            if not self.validate_descriptor(desc):
                self.metrics["errors"] += 1
                continue
            if len(self.active_descriptor_queue.items) >= self.active_descriptor_queue.capacity:
                self.fsm_state["descriptor_fetch"] = "FETCH_STALL"
                self.metrics["descriptor_queue_full_stalls"] += 1
            self.channel_state[desc.channel_id] = "ACTIVE"
            self.fsm_state["descriptor_fetch"] = "ENQUEUE_DESC"
            yield self.active_descriptor_queue.put(desc)
            self.metrics["descriptors_fetched"] += 1

    def channel_scheduler_process(self):
        # NOT CLEAR -- CHECK_PRIORITY and NO_ELIGIBLE are not modelled, and this
        # process grants in arrival order instead.
        #
        # The DLD names both states (§6.3) and CH_CFG carries a per-channel
        # priority field (§3), but it never says how that field is used: whether a
        # higher number means more urgent or less, how ties break, whether
        # arbitration is strict-priority, weighted or round-robin among equals, or
        # what makes a channel ineligible and sends it to NO_ELIGIBLE. §6.3 says
        # only that the scheduler "may arbitrate across channels every dispatch
        # tick".
        #
        # Any of those choices would be an invention that changes which descriptor
        # runs first, so none is made here. `configure_channel` still records
        # `priority` so a future implementation has the input; nothing reads it.
        # See decisions/gdma_ip.md.
        while True:
            self.fsm_state["channel_scheduler"] = "SCAN_CHANNELS"
            desc = yield self.active_descriptor_queue.get()
            yield self.env.timeout(self.lat["channel_scan"])
            self.fsm_state["channel_scheduler"] = "GRANT_CHANNEL"
            self.metrics["channel_grants"] += 1
            yield self.read_issue_queue.put(desc)

    def _read_credit_limit(self, channel_id: int) -> Optional[int]:
        limit = self.channel_read_depth.get(channel_id, self.outstanding_read_depth)
        return limit

    def _release_read_credit(self, channel_id: int) -> None:
        if self.outstanding_reads[channel_id] > 0:
            self.outstanding_reads[channel_id] -= 1
        waiter = self.read_credit_available.pop(channel_id, None)
        if waiter is not None and not waiter.triggered:
            waiter.succeed()

    def read_issue_process(self):
        while True:
            self.fsm_state["read_issue"] = "WAIT_DESCRIPTOR"
            desc = yield self.read_issue_queue.get()

            # CHECK_READ_CREDITS. Declared as a state in §6.4, as a resource in §8
            # ("per-channel outstanding read credits"), and priced in §11's timing
            # table at 4 cycles. §6.4 states the behaviour it gates: read issue
            # "can run ahead of write issue until internal buffer or outstanding
            # read limit is full". The model previously went straight from
            # WAIT_DESCRIPTOR to ISSUE_READ, so the limit did not exist and reads
            # ran ahead without bound.
            self.fsm_state["read_issue"] = "CHECK_READ_CREDITS"
            yield self.env.timeout(self.lat["credit_check"])
            limit = self._read_credit_limit(desc.channel_id)
            while limit is not None and self.outstanding_reads[desc.channel_id] >= limit:
                self.metrics["read_credit_stalls"] += 1
                self.logger.warning(
                    "read credit exhausted channel=%s outstanding=%s limit=%s",
                    desc.channel_id,
                    self.outstanding_reads[desc.channel_id],
                    limit,
                )
                waiter = self.read_credit_available.setdefault(desc.channel_id, self.env.event())
                yield waiter
            self.outstanding_reads[desc.channel_id] += 1
            self.metrics["read_credits_taken"] += 1

            while not self.source_ready:
                self.fsm_state["read_issue"] = "READ_STALL"
                self.metrics["read_stalls"] += 1
                self.logger.warning("read stall descriptor=%s time=%s", desc.desc_id, self.env.now)
                yield self.env.timeout(1)
            self.fsm_state["read_issue"] = "ISSUE_READ"
            with self.source_read_port.request() as req:
                yield req
                yield self.env.timeout(self.lat["read"])
            self.fsm_state["read_issue"] = "READ_DONE"
            self.metrics["read_requests"] += 1
            yield self.read_response_queue.put(desc)

    def read_response_process(self):
        while True:
            self.fsm_state["read_response"] = "WAIT_READ_RESP"
            desc = yield self.read_response_queue.get()
            self.fsm_state["read_response"] = "CHECK_RESP_STATUS"
            if len(self.internal_data_buffer.items) >= self.internal_data_buffer.capacity:
                self.fsm_state["read_response"] = "RESP_ERROR"
                self.metrics["buffer_full_stalls"] += 1
            self.fsm_state["read_response"] = "WRITE_BUFFER"
            # The read is no longer outstanding once its response has landed, so
            # the channel's credit returns here.
            self._release_read_credit(desc.channel_id)
            yield self.internal_data_buffer.put(desc)
            self.metrics["buffer_writes"] += 1
            self.metrics["buffer_occupancy"] = max(
                self.metrics["buffer_occupancy"], len(self.internal_data_buffer.items)
            )

    def write_issue_process(self):
        while True:
            self.fsm_state["write_issue"] = "WAIT_BUFFER_DATA"
            desc = yield self.internal_data_buffer.get()
            while not self.destination_ready:
                self.fsm_state["write_issue"] = "WRITE_STALL"
                self.metrics["write_stalls"] += 1
                self.logger.warning("write stall descriptor=%s time=%s", desc.desc_id, self.env.now)
                yield self.env.timeout(1)
            self.fsm_state["write_issue"] = "ISSUE_WRITE"
            with self.destination_write_port.request() as req:
                yield req
                yield self.env.timeout(self.lat["write"])
            self.fsm_state["write_issue"] = "WRITE_DONE"
            self.metrics["write_requests"] += 1
            yield self.write_response_queue.put(desc)

    def write_response_process(self):
        while True:
            self.fsm_state["write_response"] = "WAIT_WRITE_RESP"
            desc = yield self.write_response_queue.get()
            self.fsm_state["write_response"] = "CHECK_RESP_STATUS"
            self.fsm_state["write_response"] = "MARK_BYTES_COMPLETE"
            self.metrics["write_responses"] += 1
            yield self.descriptor_done_queue.put(desc)

    def completion_update_process(self):
        while True:
            self.fsm_state["completion_update"] = "WAIT_DESCRIPTOR_DONE"
            desc = yield self.descriptor_done_queue.get()
            while not self.completion_ready:
                self.metrics["completion_stalls"] += 1
                self.logger.warning("completion stall descriptor=%s time=%s", desc.desc_id, self.env.now)
                yield self.env.timeout(1)
            self.fsm_state["completion_update"] = "ISSUE_COMPLETION_WRITE"
            with self.completion_write_port.request() as req:
                yield req
                yield self.env.timeout(self.lat["completion"])
            self.fsm_state["completion_update"] = "UPDATE_STATUS"
            self.completed_ids.append(desc.desc_id)
            self.completed.append((self.env.now, desc))
            self.metrics["completed_descriptors"] += 1
            self.metrics["last_latency"] = self.env.now - self.descriptor_start.get(desc.desc_id, self.env.now)
            self.logger.info("completed descriptor=%s channel=%s time=%s", desc.desc_id, desc.channel_id, self.env.now)
            if desc.interrupt:
                yield self.completion_pending_queue.put(desc)

    def interrupt_coalescing_process(self):
        pending_count = 0
        while True:
            self.fsm_state["interrupt_coalescing"] = "IDLE"
            desc = yield self.completion_pending_queue.get()
            self.fsm_state["interrupt_coalescing"] = "COUNT_COMPLETIONS"
            pending_count += 1
            yield self.env.timeout(self.lat["irq"])
            if pending_count >= self.coalesce_threshold:
                self.fsm_state["interrupt_coalescing"] = "ASSERT_IRQ"
                self.irqs.append(desc.channel_id)
                self.metrics["irq_count"] += 1
                self.logger.info("irq asserted channel=%s time=%s", desc.channel_id, self.env.now)
                pending_count = 0
                # interrupt_if is wait_for_ack_before_next_request: the data path
                # keeps running, but the next coalescing window only starts once
                # software clears this interrupt.
                self.irq_clear_event = self.env.event()
                self.fsm_state["interrupt_coalescing"] = "WAIT_CLEAR"
                self.metrics["irq_clear_waits"] += 1
                yield self.irq_clear_event
                self.fsm_state["interrupt_coalescing"] = "IDLE"
                self.logger.debug("irq cleared channel=%s time=%s", desc.channel_id, self.env.now)
