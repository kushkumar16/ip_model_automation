from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional

import simpy

from .common import get_ip_logger


class InterruptControllerIpModel:
    def __init__(
        self,
        env: simpy.Environment,
        sample_latency=5,
        pending_latency=3,
        filter_latency=0,
        priority_latency=20,
        delivery_latency=4,
        ack_latency=9,
        eoi_latency=8,
        register_latency=4,
        software_latency=4,
        log_level: str = "WARNING",
        log_file: str = "run.log",
    ):
        self.env = env
        self.logger = get_ip_logger("interrupt_controller_ip", log_level, log_file)
        self.source_q = simpy.Store(env)
        self.source_event_queue = simpy.Store(env)
        self.software_write_queue = simpy.Store(env)
        self.software_interrupt_event_queue = simpy.Store(env)
        self.eligible_set_queue = simpy.Store(env)
        self.priority_winner_queue = simpy.Store(env)
        self.delivery_queue = simpy.Store(env)
        self.ack_queue = simpy.Store(env)
        self.eoi_queue = simpy.Store(env)
        self.register_read_queue = simpy.Store(env)

        self.enabled = set()
        self.masked = set()
        self.priorities: Dict[int, int] = {}
        self.targets: Dict[int, str] = {}
        self.pending = set()
        self.active = set()
        self.level_asserted = set()
        self.trigger_type = defaultdict(lambda: "EDGE")
        self.delivered: List[tuple[float, int]] = []
        self.acknowledged: List[tuple[float, int]] = []
        self.metrics = defaultdict(int)
        self.lat = {
            "sample": sample_latency,
            "pending": pending_latency,
            "filter": filter_latency,
            "priority": priority_latency,
            "delivery": delivery_latency,
            "ack": ack_latency,
            "eoi": eoi_latency,
            "register": register_latency,
            "software": software_latency,
        }
        self.last_delivered: Dict[str, int] = {}
        # cpu_irq_if is wait_for_ack_before_next_request with outstanding_limit 1
        # per target: delivery completes on assertion, but the acknowledge must
        # arrive before the next interrupt is delivered to that target.
        self.delivery_ack_events: Dict[str, simpy.Event] = {}
        self.fsm_state = {
            "source_sampling": "SAMPLE_SOURCES",
            "pending_update": "WAIT_EVENT",
            "mask_enable_filter": "WAIT_PENDING_CHANGE",
            "priority_resolver": "IDLE",
            "cpu_delivery": "IDLE",
            "acknowledge": "WAIT_ACK",
            "end_of_interrupt": "WAIT_EOI",
            "register_access": "IDLE",
            "software_interrupt": "WAIT_SOFT_INT_WRITE",
        }

        self.env.process(self.source_sampling_process())
        self.env.process(self.pending_update_process())
        self.env.process(self.mask_enable_filter_process())
        self.env.process(self.priority_resolver_process())
        self.env.process(self.cpu_delivery_process())
        self.env.process(self.acknowledge_process())
        self.env.process(self.end_of_interrupt_process())
        self.env.process(self.register_access_process())
        self.env.process(self.software_interrupt_process())
        self.logger.info("initialized")

    def configure_source(
        self,
        src_id: int,
        priority: int,
        target_id: str = "CPU0",
        enabled: bool = True,
        trigger_type: str = "EDGE",
        masked: bool = False,
    ) -> None:
        self.priorities[src_id] = priority
        self.targets[src_id] = target_id
        self.trigger_type[src_id] = trigger_type.upper()
        if enabled:
            self.enabled.add(src_id)
        else:
            self.enabled.discard(src_id)
        if masked:
            self.masked.add(src_id)
        else:
            self.masked.discard(src_id)
        self.logger.info(
            "configured source=%s priority=%s target=%s enabled=%s trigger=%s masked=%s",
            src_id,
            priority,
            target_id,
            enabled,
            trigger_type,
            masked,
        )

    def set_mask(self, src_id: int, masked: bool) -> None:
        if masked:
            self.masked.add(src_id)
        else:
            self.masked.discard(src_id)
        self.metrics["register_writes"] += 1
        self.eligible_set_queue.put("mask_update")
        self.logger.info("mask source=%s masked=%s", src_id, masked)

    def assert_edge(self, src_id: int):
        self.logger.info("edge asserted source=%s", src_id)
        return self.source_q.put((self.env.now, src_id, "EDGE", True))

    def set_level(self, src_id: int, asserted: bool):
        if asserted:
            self.level_asserted.add(src_id)
        else:
            self.level_asserted.discard(src_id)
        self.logger.info("level source=%s asserted=%s", src_id, asserted)
        return self.source_q.put((self.env.now, src_id, "LEVEL", asserted))

    def inject_software_interrupt(self, src_id: int, target_id: str = "CPU0"):
        self.logger.info("software interrupt source=%s target=%s", src_id, target_id)
        return self.software_write_queue.put((src_id, target_id))

    def ack(self, target_id: str = "CPU0"):
        self.logger.info("ack requested target=%s", target_id)
        return self.ack_queue.put(target_id)

    def read_state(self) -> simpy.Event:
        """Register read: `state = yield model.read_state()` (register_if waits for the response)."""
        response = self.env.event()
        self.register_read_queue.put({"op": "read_state", "response": response})
        return response

    def eoi(self, src_id: int) -> None:
        self.eoi_queue.put(src_id)
        self.logger.info("eoi requested source=%s", src_id)

    def _assert_edge_functional(self, src_id: int) -> None:
        if src_id in self.enabled:
            self.pending.add(src_id)

    def ack_functional(self, target_id: str = "CPU0") -> Optional[int]:
        eligible = [
            src for src in self.pending if self.targets.get(src, "CPU0") == target_id and src not in self.masked
        ]
        if not eligible:
            return None
        selected = min(eligible, key=lambda src: self.priorities.get(src, 255))
        self.pending.remove(selected)
        self.active.add(selected)
        self.last_delivered[target_id] = selected
        return selected

    def source_sampling_process(self):
        while True:
            self.fsm_state["source_sampling"] = "SAMPLE_SOURCES"
            _, src_id, trigger, value = yield self.source_q.get()
            yield self.env.timeout(self.lat["sample"])
            self.fsm_state["source_sampling"] = "DETECT_LEVEL" if trigger == "LEVEL" else "DETECT_EDGE"
            if trigger == "LEVEL" and not value:
                continue
            self.fsm_state["source_sampling"] = "QUEUE_EVENT"
            yield self.source_event_queue.put(src_id)
            self.metrics["source_events"] += 1

    def pending_update_process(self):
        while True:
            self.fsm_state["pending_update"] = "WAIT_EVENT"
            source_request = self.source_event_queue.get()
            software_request = self.software_interrupt_event_queue.get()
            ready = yield source_request | software_request

            # SimPy leaves the branch that lost sitting in its store's get queue.
            # The next put to that store is handed to a request nobody is waiting
            # on any more, so the item is consumed and never seen: an interrupt
            # accepted by the controller and delivered to no one.
            for request in (source_request, software_request):
                if request not in ready:
                    request.cancel()

            # AnyOf reports every branch that fired, and both ingress paths
            # arriving in the same cycle is ordinary for a block with two of
            # them. Reading a single value off it would discard the other.
            for src_id in ready.values():
                yield from self._set_pending(src_id)

    def _set_pending(self, src_id: int):
        yield self.env.timeout(self.lat["pending"])
        self.fsm_state["pending_update"] = "SET_PENDING"
        if src_id in self.enabled:
            self.pending.add(src_id)
            self.metrics["pending_updates"] += 1
            self.logger.debug("pending set source=%s", src_id)
            yield self.eligible_set_queue.put("pending_changed")
        else:
            self.metrics["disabled_source_events"] += 1
            self.logger.warning("disabled source event source=%s", src_id)

    def mask_enable_filter_process(self):
        while True:
            self.fsm_state["mask_enable_filter"] = "WAIT_PENDING_CHANGE"
            yield self.eligible_set_queue.get()
            yield self.env.timeout(self.lat["filter"])
            yield self.env.timeout(1)
            self.fsm_state["mask_enable_filter"] = "PUBLISH_ELIGIBLE_SET"
            eligible = [src for src in self.pending if src in self.enabled and src not in self.masked]
            self.metrics["masked_irqs"] += len([src for src in self.pending if src in self.masked])
            if any(src in self.masked for src in self.pending):
                self.logger.warning(
                    "masked pending sources=%s", sorted(src for src in self.pending if src in self.masked)
                )
            if eligible:
                yield self.priority_winner_queue.put(eligible)

    def priority_resolver_process(self):
        while True:
            self.fsm_state["priority_resolver"] = "IDLE"
            eligible = yield self.priority_winner_queue.get()
            self.fsm_state["priority_resolver"] = "COMPARE_PRIORITY"
            yield self.env.timeout(self.lat["priority"])
            selected = min(eligible, key=lambda src: self.priorities.get(src, 255))
            self.fsm_state["priority_resolver"] = "SELECT_WINNER"
            yield self.delivery_queue.put(selected)
            self.metrics["priority_resolutions"] += 1
            self.logger.debug("priority selected source=%s eligible=%s", selected, eligible)

    def cpu_delivery_process(self):
        while True:
            self.fsm_state["cpu_delivery"] = "IDLE"
            selected = yield self.delivery_queue.get()
            if selected not in self.pending:
                # Resolved before this delivery got its turn (acknowledged or
                # cleared meanwhile): deliver the current winner, not a stale one.
                self.fsm_state["cpu_delivery"] = "DELIVERY_STALL"
                self.metrics["stale_delivery_skips"] += 1
                self.logger.debug("stale delivery skipped source=%s", selected)
                continue
            self.fsm_state["cpu_delivery"] = "ASSERT_IRQ"
            yield self.env.timeout(self.lat["delivery"])
            target_id = self.targets.get(selected, "CPU0")
            self.delivered.append((self.env.now, selected))
            self.last_delivered[target_id] = selected
            self.metrics["delivered_irqs"] += 1
            self.logger.info("delivered source=%s target=%s time=%s", selected, target_id, self.env.now)
            # One interrupt in flight per target: hold the level here until the
            # CPU acknowledges, then deliver the next winner.
            ack_event = self.env.event()
            self.delivery_ack_events[target_id] = ack_event
            self.fsm_state["cpu_delivery"] = "WAIT_ACK"
            self.metrics["delivery_ack_waits"] += 1
            yield ack_event
            self.fsm_state["cpu_delivery"] = "HOLD_LEVEL"
            self.logger.debug("delivery acknowledged source=%s target=%s", selected, target_id)

    def acknowledge_process(self):
        while True:
            self.fsm_state["acknowledge"] = "WAIT_ACK"
            target_id = yield self.ack_queue.get()
            self.fsm_state["acknowledge"] = "LOOKUP_SELECTED_IRQ"
            yield self.env.timeout(self.lat["ack"])
            selected = self.last_delivered.get(target_id)
            if selected is None or selected not in self.pending:
                selected = self.ack_functional(target_id)
            else:
                self.pending.remove(selected)
                self.active.add(selected)
            if selected is None:
                self.metrics["ack_errors"] += 1
                self.logger.error("ack error target=%s", target_id)
                continue
            self.fsm_state["acknowledge"] = "SET_ACTIVE"
            self.acknowledged.append((self.env.now, selected))
            self.metrics["ack_count"] += 1
            self.logger.info("acknowledged source=%s target=%s", selected, target_id)
            # Releases cpu_delivery from WAIT_ACK so the next winner for this
            # target can be delivered.
            ack_event = self.delivery_ack_events.pop(target_id, None)
            if ack_event is not None and not ack_event.triggered:
                ack_event.succeed()
            # The acknowledged source left the pending set, so re-resolve: a
            # lower-priority pending interrupt may now be the winner.
            yield self.eligible_set_queue.put("post_ack")

    def end_of_interrupt_process(self):
        while True:
            self.fsm_state["end_of_interrupt"] = "WAIT_EOI"
            src_id = yield self.eoi_queue.get()
            self.fsm_state["end_of_interrupt"] = "CLEAR_ACTIVE"
            yield self.env.timeout(self.lat["eoi"])
            self.active.discard(src_id)
            if src_id in self.level_asserted and src_id in self.enabled:
                self.fsm_state["end_of_interrupt"] = "CHECK_LEVEL_REASSERT"
                self.pending.add(src_id)
                self.metrics["reasserted_level_irqs"] += 1
                self.logger.warning("level reasserted source=%s", src_id)
                yield self.eligible_set_queue.put("level_reassert")
            self.metrics["eoi_count"] += 1
            self.logger.info("eoi completed source=%s", src_id)

    def register_access_process(self):
        while True:
            self.fsm_state["register_access"] = "IDLE"
            request = yield self.register_read_queue.get()
            self.fsm_state["register_access"] = "DECODE_ACCESS"
            yield self.env.timeout(self.lat["register"])
            # register_if is wait_for_response: software blocks on the read and
            # decides what to write next from what comes back.
            self.fsm_state["register_access"] = "READ_STATE"
            request["response"].succeed(
                {
                    "pending": sorted(self.pending),
                    "active": sorted(self.active),
                    "enabled": sorted(self.enabled),
                    "masked": sorted(self.masked),
                }
            )
            self.metrics["register_reads"] += 1

    def software_interrupt_process(self):
        while True:
            self.fsm_state["software_interrupt"] = "WAIT_SOFT_INT_WRITE"
            src_id, target_id = yield self.software_write_queue.get()
            self.fsm_state["software_interrupt"] = "VALIDATE_TARGET"
            yield self.env.timeout(self.lat["software"])
            self.targets[src_id] = target_id
            self.enabled.add(src_id)
            self.fsm_state["software_interrupt"] = "GENERATE_SOFT_EVENT"
            yield self.software_interrupt_event_queue.put(src_id)
            self.metrics["software_interrupts"] += 1
            self.logger.info("software interrupt generated source=%s target=%s", src_id, target_id)
