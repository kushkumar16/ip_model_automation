from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Dict, List, Tuple

import simpy

from .common import get_ip_logger


class MailboxIpModel:
    """SimPy transaction-level delay model for the Mailbox IP.

    Concurrent processes: register access, message push, message pop, doorbell,
    and interrupt notify. Timing comes from the reviewed template.
    """

    def __init__(
        self,
        env: simpy.Environment,
        num_channels: int = 4,
        fifo_depth: int = 8,
        register_latency: int = 0,
        push_latency: int = 0,
        pop_latency: int = 0,
        doorbell_latency: int = 0,
        interrupt_latency: int = 1,
        log_level: str = "WARNING",
        log_file: str = "run.log",
    ):
        self.env = env
        self.logger = get_ip_logger("mailbox_ip", log_level, log_file)
        self.num_channels = num_channels
        self.fifo_depth = fifo_depth
        self.lat = {
            "register": register_latency,
            "push": push_latency,
            "pop": pop_latency,
            "doorbell": doorbell_latency,
            "interrupt": interrupt_latency,
        }

        self.register_if = simpy.Store(env)
        self.sender_if = simpy.Store(env)
        self.doorbell_queue = simpy.Store(env)
        self.interrupt_pending_queue = simpy.Store(env)

        self.channels: Dict[int, Dict[str, Any]] = {}
        self.message_fifos: Dict[int, deque] = defaultdict(deque)
        self.delivered: List[Tuple[int, Any]] = []
        self.doorbell_status: set[int] = set()
        self.interrupts: List[Tuple[float, int]] = []
        # interrupt_if is wait_for_ack_before_next_request with outstanding_limit 1:
        # the asserted doorbell parks the notify process until software clears it.
        self.irq_clear_events: Dict[int, simpy.Event] = {}
        self.metrics = defaultdict(int)
        self.receiver_enabled = True

        self.fsm_state = {
            "register_access": "IDLE",
            "message_push": "IDLE",
            "message_pop": "IDLE",
            "doorbell": "IDLE",
            "interrupt_notify": "IDLE",
        }

        self.env.process(self.register_access_process())
        self.env.process(self.message_push_process())
        self.env.process(self.message_pop_process())
        self.env.process(self.doorbell_process())
        self.env.process(self.interrupt_notify_process())
        self.logger.info("initialized channels=%s fifo_depth=%s", num_channels, fifo_depth)

    # ------------------------------------------------------------------ #
    # Public API (functionality_model.apis)
    # ------------------------------------------------------------------ #
    def configure_channel(self, channel_id: int, enable: bool = True, masked: bool = False):
        return self.register_if.put({"op": "config", "channel_id": channel_id, "enable": enable, "masked": masked})

    def send_message(self, channel_id: int, message: Any):
        self.logger.info("send channel=%s", channel_id)
        return self.sender_if.put((channel_id, message))

    def read_message(self, channel_id: int):
        for index, (chan, message) in enumerate(self.delivered):
            if chan == channel_id:
                return self.delivered.pop(index)[1]
        return None

    def clear_interrupt(self, channel_id: int) -> None:
        self.doorbell_status.discard(channel_id)
        self.interrupts = [(t, c) for t, c in self.interrupts if c != channel_id]
        self.metrics["clear_latency"] = self.lat["register"]
        # Releases the notify process parked in WAIT_SW_CLEAR for this channel.
        clear_event = self.irq_clear_events.pop(channel_id, None)
        if clear_event is not None and not clear_event.triggered:
            clear_event.succeed()
        self.logger.info("interrupt cleared channel=%s", channel_id)

    def read_status(self, channel_id: int) -> simpy.Event:
        """Register read: the caller blocks until the access returns a value.

        register_if is `wait_for_response`, so a read is a request the requester
        waits on -- `value = yield model.read_status(channel)` -- not a plain
        Python getter that would hide the access latency.
        """
        response = self.env.event()
        self.register_if.put({"op": "read_status", "channel_id": channel_id, "response": response})
        return response

    def set_receiver_enabled(self, enabled: bool) -> None:
        self.receiver_enabled = enabled

    def get_metrics(self) -> Dict[str, int]:
        return dict(self.metrics)

    # ------------------------------------------------------------------ #
    # FSM processes
    # ------------------------------------------------------------------ #
    def _apply_config(self, update: Dict[str, Any]) -> None:
        channel_id = update["channel_id"]
        self.channels[channel_id] = {
            "enabled": update.get("enable", True),
            "masked": update.get("masked", False),
        }
        self.message_fifos.setdefault(channel_id, deque())
        self.metrics["config_applied"] += 1
        self.logger.info("config applied channel=%s masked=%s", channel_id, update.get("masked", False))

    def register_access_process(self):
        while True:
            self.fsm_state["register_access"] = "IDLE"
            update = yield self.register_if.get()
            self.fsm_state["register_access"] = "DECODE_ACCESS"
            yield self.env.timeout(self.lat["register"])
            if update.get("op") == "read_status":
                self.fsm_state["register_access"] = "READ_STATUS"
                channel_id = update["channel_id"]
                update["response"].succeed(
                    {
                        "occupancy": len(self.message_fifos[channel_id]),
                        "doorbell_pending": channel_id in self.doorbell_status,
                        "masked": self.channels.get(channel_id, {}).get("masked", False),
                    }
                )
                self.metrics["status_reads"] += 1
                continue
            self.fsm_state["register_access"] = "WRITE_CONFIG"
            self._apply_config(update)
            self.metrics["config_writes"] += 1

    def message_push_process(self):
        while True:
            self.fsm_state["message_push"] = "IDLE"
            channel_id, message = yield self.sender_if.get()
            self.fsm_state["message_push"] = "ACCEPT_MESSAGE"
            yield self.env.timeout(self.lat["push"])
            self.fsm_state["message_push"] = "CHECK_SPACE"
            fifo = self.message_fifos[channel_id]
            if len(fifo) < self.fifo_depth:
                fifo.append(message)
                self.fsm_state["message_push"] = "ENQUEUE"
                self.metrics["enqueued_messages"] += 1
                yield self.doorbell_queue.put(channel_id)
            else:
                self.fsm_state["message_push"] = "REJECT_FULL"
                self.metrics["dropped_on_full"] += 1
                self.logger.warning("channel fifo full channel=%s message dropped", channel_id)

    def message_pop_process(self):
        while True:
            self.fsm_state["message_pop"] = "IDLE"
            yield self.env.timeout(1)
            if not self.receiver_enabled:
                self.fsm_state["message_pop"] = "EMPTY_WAIT"
                continue
            for channel_id, fifo in self.message_fifos.items():
                if not fifo:
                    continue
                self.fsm_state["message_pop"] = "CHECK_PENDING"
                yield self.env.timeout(self.lat["pop"])
                self.fsm_state["message_pop"] = "DEQUEUE"
                message = fifo.popleft()
                self.fsm_state["message_pop"] = "DELIVER"
                self.delivered.append((channel_id, message))
                self.metrics["delivered_messages"] += 1
                self.logger.info("delivered channel=%s time=%s", channel_id, self.env.now)

    def doorbell_process(self):
        while True:
            self.fsm_state["doorbell"] = "IDLE"
            channel_id = yield self.doorbell_queue.get()
            self.fsm_state["doorbell"] = "RAISE_DOORBELL"
            yield self.env.timeout(self.lat["doorbell"])
            self.metrics["doorbells"] += 1
            self.doorbell_status.add(channel_id)
            self.fsm_state["doorbell"] = "RECORD_EVENT"
            yield self.interrupt_pending_queue.put(channel_id)

    def interrupt_notify_process(self):
        while True:
            self.fsm_state["interrupt_notify"] = "IDLE"
            channel_id = yield self.interrupt_pending_queue.get()
            self.fsm_state["interrupt_notify"] = "COLLECT_DOORBELLS"
            yield self.env.timeout(self.lat["interrupt"])
            self.fsm_state["interrupt_notify"] = "APPLY_MASK"
            if self.channels.get(channel_id, {}).get("masked", False):
                self.metrics["masked_doorbells"] += 1
                self.logger.warning("doorbell masked channel=%s", channel_id)
                continue
            self.fsm_state["interrupt_notify"] = "ASSERT_IRQ"
            self.interrupts.append((self.env.now, channel_id))
            self.metrics["interrupt_count"] += 1
            self.logger.info("interrupt asserted channel=%s time=%s", channel_id, self.env.now)
            # The doorbell is level: it stays asserted, and no further doorbell is
            # notified, until software clears this one (interrupt_if wait model).
            clear_event = self.env.event()
            self.irq_clear_events[channel_id] = clear_event
            self.fsm_state["interrupt_notify"] = "WAIT_SW_CLEAR"
            self.metrics["irq_clear_waits"] += 1
            yield clear_event
            self.fsm_state["interrupt_notify"] = "DEASSERT_IRQ"
            self.logger.debug("interrupt deasserted channel=%s time=%s", channel_id, self.env.now)
