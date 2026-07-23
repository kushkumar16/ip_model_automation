from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, List, Tuple

import simpy

from .common import get_ip_logger


class I3cIpModel:
    """SimPy transaction-level delay model for the I3C Master IP.

    Concurrent processes: register access, command engine, bit transfer engine,
    IBI engine, and interrupt control. Timing comes from the reviewed template.
    The command engine and IBI engine share the I3C bus: an IBI request is only
    arbitrated onto the bus while no command transfer is in flight, matching
    the DLD's bus-idle-window assumption.
    """

    def __init__(
        self,
        env: simpy.Environment,
        command_depth: int = 8,
        tx_depth: int = 8,
        rx_depth: int = 8,
        ibi_depth: int = 8,
        byte_bits: int = 8,
        register_latency: int = 0,
        arbitrate_latency: int = 1,
        start_latency: int = 0,
        address_latency: int = 0,
        shift_latency: int = 1,
        ack_latency: int = 0,
        ibi_detect_latency: int = 1,
        ibi_arbitrate_latency: int = 0,
        ibi_ack_latency: int = 0,
        interrupt_latency: int = 1,
        log_level: str = "WARNING",
        log_file: str = "run.log",
    ):
        self.env = env
        self.logger = get_ip_logger("i3c_ip", log_level, log_file)
        self.command_depth = command_depth
        self.tx_depth = tx_depth
        self.rx_depth = rx_depth
        self.ibi_depth = ibi_depth
        self.byte_bits = byte_bits
        self.lat = {
            "register": register_latency,
            "arbitrate": arbitrate_latency,
            "start": start_latency,
            "address": address_latency,
            "shift": shift_latency,
            "ack": ack_latency,
            "ibi_detect": ibi_detect_latency,
            "ibi_arbitrate": ibi_arbitrate_latency,
            "ibi_ack": ibi_ack_latency,
            "interrupt": interrupt_latency,
        }

        self.register_if = simpy.Store(env)
        self.shift_queue = simpy.Store(env, capacity=4)
        self.interrupt_pending_queue = simpy.Store(env)

        self.command_queue: deque = deque()
        self.tx_fifo: deque = deque()
        self.rx_fifo: deque = deque()
        self.ibi_fifo: deque = deque()
        self.ibi_requests_pending: deque = deque()

        self.interrupt_masked = False
        self.bus_busy = False
        self.interrupts: List[Tuple[float, str]] = []
        # interrupt_if is wait_for_ack_before_next_request with outstanding_limit 1:
        # transfers continue, but the next assertion waits for the software clear.
        self.irq_clear_event: simpy.Event | None = None
        self.metrics = defaultdict(int)
        self._ibi_payload_counter = 0

        self.fsm_state = {
            "register_access": "IDLE",
            "command_engine": "IDLE",
            "bit_transfer_engine": "IDLE",
            "ibi_engine": "IDLE",
            "interrupt_control": "IDLE",
        }

        self.env.process(self.register_access_process())
        self.env.process(self.command_engine_process())
        self.env.process(self.bit_transfer_engine_process())
        self.env.process(self.ibi_engine_process())
        self.env.process(self.interrupt_control_process())
        self.logger.info(
            "initialized command_depth=%s tx_depth=%s rx_depth=%s ibi_depth=%s byte_bits=%s",
            command_depth,
            tx_depth,
            rx_depth,
            ibi_depth,
            byte_bits,
        )

    # ------------------------------------------------------------------ #
    # Public API (functionality_model.apis)
    # ------------------------------------------------------------------ #
    def configure(self, masked: bool = False):
        return self.register_if.put({"op": "configure", "masked": masked})

    def queue_command(self, cmd_id: str, direction: str, byte_count: int = 1):
        self.logger.info("queue_command id=%s direction=%s byte_count=%s", cmd_id, direction, byte_count)
        return self.register_if.put(
            {"op": "queue_command", "cmd_id": cmd_id, "direction": direction, "byte_count": byte_count}
        )

    def write_tx(self, byte: int):
        return self.register_if.put({"op": "write_tx", "byte": byte})

    def read_rx(self):
        if self.rx_fifo:
            return self.rx_fifo.popleft()
        return None

    def read_ibi(self):
        if self.ibi_fifo:
            return self.ibi_fifo.popleft()
        return None

    def raise_ibi_request(self):
        self.metrics["ibi_requests"] += 1
        self.ibi_requests_pending.append(True)
        self.logger.info("ibi request raised time=%s", self.env.now)

    def clear_interrupt(self):
        return self.register_if.put({"op": "clear_int"})

    def get_metrics(self) -> Dict[str, int]:
        return dict(self.metrics)

    # ------------------------------------------------------------------ #
    # FSM processes
    # ------------------------------------------------------------------ #
    def register_access_process(self):
        while True:
            self.fsm_state["register_access"] = "IDLE"
            update = yield self.register_if.get()
            self.fsm_state["register_access"] = "DECODE_ACCESS"
            yield self.env.timeout(self.lat["register"])
            op = update.get("op")
            if op == "configure":
                self.fsm_state["register_access"] = "WRITE_CONFIG"
                self.interrupt_masked = update.get("masked", False)
                self.metrics["config_writes"] += 1
                self.logger.info("config applied masked=%s", self.interrupt_masked)
            elif op == "queue_command":
                self.fsm_state["register_access"] = "WRITE_CONFIG"
                if len(self.command_queue) < self.command_depth:
                    self.command_queue.append(
                        {
                            "cmd_id": update["cmd_id"],
                            "direction": update["direction"],
                            "byte_count": update["byte_count"],
                        }
                    )
                    self.metrics["commands_issued"] += 1
                else:
                    self.metrics["command_queue_dropped"] += 1
                    self.logger.warning("command queue full, command dropped id=%s", update["cmd_id"])
            elif op == "write_tx":
                self.fsm_state["register_access"] = "WRITE_CONFIG"
                if len(self.tx_fifo) < self.tx_depth:
                    self.tx_fifo.append(update["byte"])
                else:
                    self.metrics["tx_dropped"] += 1
                    self.logger.warning("tx queue full byte dropped")
            elif op == "clear_int":
                self.fsm_state["register_access"] = "READ_STATUS"
                self.interrupts.clear()
                if self.irq_clear_event is not None and not self.irq_clear_event.triggered:
                    self.irq_clear_event.succeed()
                    self.irq_clear_event = None
                self.metrics["clear_latency"] = self.lat["register"]
                self.logger.info("interrupt cleared")

    def command_engine_process(self):
        while True:
            self.fsm_state["command_engine"] = "IDLE"
            yield self.env.timeout(1)
            if not self.command_queue or self.bus_busy:
                continue
            self.bus_busy = True
            cmd = self.command_queue.popleft()
            self.fsm_state["command_engine"] = "ARBITRATE_BUS"
            yield self.env.timeout(self.lat["arbitrate"])
            self.fsm_state["command_engine"] = "ISSUE_START"
            yield self.env.timeout(self.lat["start"])
            self.fsm_state["command_engine"] = "SEND_ADDRESS"
            yield self.env.timeout(self.lat["address"])
            self.fsm_state["command_engine"] = "WAIT_ACK"
            yield self.env.timeout(self.lat["ack"])
            self.fsm_state["command_engine"] = "DISPATCH_TRANSFER"
            yield self.shift_queue.put(cmd)

    def bit_transfer_engine_process(self):
        while True:
            self.fsm_state["bit_transfer_engine"] = "IDLE"
            cmd = yield self.shift_queue.get()
            for _ in range(cmd["byte_count"]):
                if cmd["direction"] == "WRITE":
                    byte = self.tx_fifo.popleft() if self.tx_fifo else 0
                else:
                    byte = None
                for _ in range(self.byte_bits):
                    self.fsm_state["bit_transfer_engine"] = "SHIFT_BIT"
                    yield self.env.timeout(self.lat["shift"])
                    self.fsm_state["bit_transfer_engine"] = "SAMPLE_BIT"
                self.fsm_state["bit_transfer_engine"] = "BYTE_COMPLETE"
                self.fsm_state["bit_transfer_engine"] = "CHECK_ACK"
                yield self.env.timeout(self.lat["ack"])
                if cmd["direction"] == "WRITE":
                    self.metrics["transmitted_bytes"] += 1
                else:
                    captured = byte if byte is not None else 0
                    if len(self.rx_fifo) < self.rx_depth:
                        self.rx_fifo.append(captured)
                        self.metrics["received_bytes"] += 1
                    else:
                        self.metrics["rx_overflows"] += 1
            self.fsm_state["bit_transfer_engine"] = "ISSUE_STOP"
            self.metrics["transfers_completed"] += 1
            yield self.interrupt_pending_queue.put("transfer_done")
            self.bus_busy = False

    def ibi_engine_process(self):
        while True:
            self.fsm_state["ibi_engine"] = "IDLE"
            yield self.env.timeout(1)
            if not self.ibi_requests_pending or self.bus_busy:
                continue
            self.bus_busy = True
            self.ibi_requests_pending.popleft()
            self.fsm_state["ibi_engine"] = "DETECT_REQUEST"
            yield self.env.timeout(self.lat["ibi_detect"])
            self.fsm_state["ibi_engine"] = "ARBITRATE_IBI"
            yield self.env.timeout(self.lat["ibi_arbitrate"])
            self.fsm_state["ibi_engine"] = "ACK_IBI"
            yield self.env.timeout(self.lat["ibi_ack"])
            self.metrics["ibi_accepted"] += 1
            self.fsm_state["ibi_engine"] = "READ_IBI_BYTE"
            payload = self._ibi_payload_counter & 0xFF
            self._ibi_payload_counter += 1
            if len(self.ibi_fifo) < self.ibi_depth:
                self.ibi_fifo.append(payload)
                self.metrics["ibi_bytes_captured"] += 1
            else:
                self.metrics["ibi_overflows"] += 1
            yield self.interrupt_pending_queue.put("ibi_received")
            self.bus_busy = False

    def interrupt_control_process(self):
        while True:
            self.fsm_state["interrupt_control"] = "IDLE"
            event = yield self.interrupt_pending_queue.get()
            self.fsm_state["interrupt_control"] = "EVALUATE_EVENTS"
            yield self.env.timeout(self.lat["interrupt"])
            self.fsm_state["interrupt_control"] = "APPLY_MASK"
            if self.interrupt_masked:
                self.metrics["masked_interrupts"] += 1
                self.logger.warning("interrupt masked event=%s", event)
                continue
            self.fsm_state["interrupt_control"] = "ASSERT_IRQ"
            self.interrupts.append((self.env.now, event))
            self.metrics["interrupt_count"] += 1
            self.logger.info("interrupt asserted event=%s time=%s", event, self.env.now)
            # Level interrupt: the next event cannot raise an IRQ until
            # software clears this one through the register interface.
            self.irq_clear_event = self.env.event()
            self.fsm_state["interrupt_control"] = "WAIT_SW_CLEAR"
            self.metrics["irq_clear_waits"] += 1
            yield self.irq_clear_event
            self.fsm_state["interrupt_control"] = "DEASSERT_IRQ"
            self.logger.debug("interrupt deasserted event=%s time=%s", event, self.env.now)
