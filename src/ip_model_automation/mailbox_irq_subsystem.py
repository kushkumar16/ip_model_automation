from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Tuple

import simpy

from .common import get_ip_logger
from .interrupt_controller_ip import InterruptControllerIpModel
from .mailbox_ip import MailboxIpModel


class MailboxIrqSubsystemModel:
    """SimPy delay model of the mailbox interrupt-delivery subsystem.

    Composes the Mailbox IP (doorbell interrupt source) and the Interrupt
    Controller IP (delivery fabric) with glue processes that close the full
    message-to-EOI round trip: host message intake, doorbell-to-level bridging
    (which also acknowledges the mailbox doorbell, since the mailbox allows one
    outstanding interrupt), CPU acknowledge, and a software handler that reads
    the message, deasserts the level once the channel drains, and issues EOI.
    Level semantics are real:
    the level stays asserted while unserviced messages remain, so the
    controller's EOI reassert re-pends the source. A storm monitor masks a
    flooding source at the controller until its outstanding count drains.

    Channel N maps to controller source N. Glue timing comes from the subsystem
    template; member IP timing comes from each member's own reviewed template
    (overridable via *_kwargs).
    """

    def __init__(
        self,
        env: simpy.Environment,
        mailbox_kwargs: Optional[Dict[str, Any]] = None,
        controller_kwargs: Optional[Dict[str, Any]] = None,
        storm_limit: int = 8,
        storm_window: int = 40,
        target_id: str = "CPU0",
        log_level: str = "WARNING",
        log_file: str = "run.log",
    ):
        self.env = env
        self.logger = get_ip_logger("mailbox_irq_subsystem", log_level, log_file)
        self.storm_limit = storm_limit
        self.storm_window = storm_window
        self.target_id = target_id

        mailbox_kwargs = dict(mailbox_kwargs or {})
        controller_kwargs = dict(controller_kwargs or {})
        for kwargs in (mailbox_kwargs, controller_kwargs):
            kwargs.setdefault("log_level", log_level)
            kwargs.setdefault("log_file", log_file)

        self.mailbox = MailboxIpModel(env, **mailbox_kwargs)
        self.controller = InterruptControllerIpModel(env, **controller_kwargs)

        self.message_intake_queue = simpy.Store(env, capacity=16)
        self.send_timestamps: Dict[int, deque] = defaultdict(deque)
        self.outstanding: Dict[int, int] = defaultdict(int)
        self.storm_masked: Dict[int, float] = {}

        self.serviced: List[Tuple[float, int, Any]] = []
        self.metrics = defaultdict(int)

        self._delivered_seen = 0
        self._acked_seen = 0

        self.fsm_state = {
            "host_message": "IDLE",
            "irq_source_bridge": "IDLE",
            "cpu_service": "IDLE",
            "software_handler": "IDLE",
            "storm_monitor": "IDLE",
        }

        self.env.process(self.host_message_process())
        self.env.process(self.irq_source_bridge_process())
        self.env.process(self.cpu_service_process())
        self.env.process(self.software_handler_process())
        self.env.process(self.storm_monitor_process())
        self.logger.info("initialized storm_limit=%s target=%s", storm_limit, target_id)

    # ------------------------------------------------------------------ #
    # Public API (functionality_model.apis)
    # ------------------------------------------------------------------ #
    def send(self, channel_id: int, message: Any):
        self.logger.info("send channel=%s", channel_id)
        return self.message_intake_queue.put((channel_id, message))

    def configure_channel(self, channel_id: int, priority: int = 10, masked: bool = False) -> None:
        self.mailbox.configure_channel(channel_id, enable=True, masked=masked)
        self.controller.configure_source(channel_id, priority=priority, target_id=self.target_id, trigger_type="LEVEL")

    def set_storm_limit(self, storm_limit: int) -> None:
        self.storm_limit = storm_limit
        self.logger.info("storm_limit=%s", storm_limit)

    def get_metrics(self) -> Dict[str, int]:
        return dict(self.metrics)

    def _source_for_channel(self, channel_id: int) -> int:
        return channel_id

    # ------------------------------------------------------------------ #
    # FSM processes (glue)
    # ------------------------------------------------------------------ #
    def host_message_process(self):
        while True:
            self.fsm_state["host_message"] = "IDLE"
            channel_id, message = yield self.message_intake_queue.get()
            self.fsm_state["host_message"] = "ACCEPT_MESSAGE"
            yield self.env.timeout(1)
            self.send_timestamps[channel_id].append(self.env.now)
            self.metrics["messages_sent"] += 1
            self.fsm_state["host_message"] = "FORWARD_MAILBOX"
            yield self.env.timeout(1)
            yield self.mailbox.send_message(channel_id, message)

    def irq_source_bridge_process(self):
        # Pops (consumes) mailbox interrupt entries rather than indexing with a
        # seen-counter: mailbox.clear_interrupt() rebuilds the interrupts list,
        # which would invalidate saved indices.
        while True:
            self.fsm_state["irq_source_bridge"] = "IDLE"
            yield self.env.timeout(1)
            while self.mailbox.interrupts:
                self.fsm_state["irq_source_bridge"] = "CONSUME_MAILBOX_IRQ"
                _time, channel_id = self.mailbox.interrupts.pop(0)
                yield self.env.timeout(1)
                self.fsm_state["irq_source_bridge"] = "MAP_SOURCE"
                src_id = self._source_for_channel(channel_id)
                yield self.env.timeout(1)
                self.fsm_state["irq_source_bridge"] = "ASSERT_LEVEL"
                self.outstanding[channel_id] += 1
                yield self.controller.set_level(src_id, True)
                # The mailbox doorbell is wait_for_ack_before_next_request with
                # outstanding_limit 1: it stays asserted until cleared, and the
                # mailbox raises no further doorbell until then. Once the level
                # is latched at the controller the doorbell has done its job, so
                # the bridge acknowledges it here; from this point the *level*
                # (managed by the software handler until the channel drains) is
                # what keeps the source pending.
                self.mailbox.clear_interrupt(channel_id)
                self.metrics["doorbells_acked"] += 1
                self.metrics["irqs_bridged"] += 1
                self.logger.debug(
                    "bridged channel=%s source=%s outstanding=%s", channel_id, src_id, self.outstanding[channel_id]
                )

    def cpu_service_process(self):
        while True:
            self.fsm_state["cpu_service"] = "POLL_DELIVERED"
            yield self.env.timeout(1)
            new_deliveries = self.controller.delivered[self._delivered_seen :]
            self._delivered_seen += len(new_deliveries)
            for _time, src_id in new_deliveries:
                self.metrics["deliveries_observed"] += 1
                self.fsm_state["cpu_service"] = "ISSUE_ACK"
                yield self.env.timeout(1)
                yield self.controller.ack(self.target_id)
                self.metrics["acks_issued"] += 1

    def software_handler_process(self):
        while True:
            self.fsm_state["software_handler"] = "POLL_ACKED"
            yield self.env.timeout(1)
            new_acked = self.controller.acknowledged[self._acked_seen :]
            self._acked_seen += len(new_acked)
            for _time, src_id in new_acked:
                channel_id = src_id
                self.fsm_state["software_handler"] = "READ_MESSAGE"
                yield self.env.timeout(2)
                message = self.mailbox.read_message(channel_id)
                while message is None:
                    # The mailbox pop process may not have delivered the
                    # message yet; retry on the next tick.
                    yield self.env.timeout(1)
                    message = self.mailbox.read_message(channel_id)
                self.outstanding[channel_id] = max(0, self.outstanding[channel_id] - 1)
                self.serviced.append((self.env.now, channel_id, message))
                self.metrics["messages_serviced"] += 1
                if self.send_timestamps[channel_id]:
                    sent = self.send_timestamps[channel_id].popleft()
                    self.metrics["round_trip_latency"] = self.env.now - sent
                if self.outstanding[channel_id] == 0:
                    self.fsm_state["software_handler"] = "CLEAR_DOORBELL"
                    yield self.env.timeout(1)
                    self.mailbox.clear_interrupt(channel_id)
                    yield self.controller.set_level(src_id, False)
                else:
                    self.metrics["level_reasserts_used"] += 1
                self.fsm_state["software_handler"] = "ISSUE_EOI"
                yield self.env.timeout(1)
                self.controller.eoi(src_id)
                self.metrics["eois_issued"] += 1
                self.logger.info(
                    "serviced channel=%s remaining=%s time=%s",
                    channel_id,
                    self.outstanding[channel_id],
                    self.env.now,
                )

    def storm_monitor_process(self):
        # Release is time-windowed rather than drain-based: the controller's
        # pending set collapses a flooding source to one in-flight delivery at
        # a time, so a masked source cannot drain below the limit on its own.
        # After the window the source is unmasked and re-masked if still
        # flooding (duty-cycle throttling).
        while True:
            self.fsm_state["storm_monitor"] = "SAMPLE_OUTSTANDING"
            yield self.env.timeout(2)
            for channel_id, count in list(self.outstanding.items()):
                src_id = self._source_for_channel(channel_id)
                if src_id in self.storm_masked:
                    if self.env.now - self.storm_masked[src_id] >= self.storm_window:
                        self.fsm_state["storm_monitor"] = "RELEASE_MASK"
                        yield self.env.timeout(1)
                        self.storm_masked.pop(src_id)
                        self.controller.set_mask(src_id, False)
                        self.metrics["storm_mask_releases"] += 1
                        self.logger.info("storm mask released source=%s time=%s", src_id, self.env.now)
                elif count >= self.storm_limit:
                    self.fsm_state["storm_monitor"] = "APPLY_MASK"
                    yield self.env.timeout(1)
                    self.storm_masked[src_id] = self.env.now
                    self.controller.set_mask(src_id, True)
                    self.metrics["storm_throttle_events"] += 1
                    self.logger.warning(
                        "storm mask applied source=%s outstanding=%s time=%s", src_id, count, self.env.now
                    )
