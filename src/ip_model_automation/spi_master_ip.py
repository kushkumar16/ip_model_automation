from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Dict, List, Tuple

import simpy

from .common import get_ip_logger


class SpiMasterIpModel:
    """SimPy transaction-level delay model for the SPI Master IP.

    Concurrent processes: register access, transmit engine, shift engine, receive
    engine, and interrupt control. Timing comes from the reviewed template. A byte
    shifted out is looped back as the received byte at this abstraction level.
    """

    def __init__(
        self,
        env: simpy.Environment,
        tx_depth: int = 8,
        rx_depth: int = 8,
        byte_bits: int = 8,
        register_latency: int = 0,
        load_latency: int = 0,
        shift_latency: int = 1,
        capture_latency: int = 0,
        interrupt_latency: int = 1,
        log_level: str = "WARNING",
        log_file: str = "run.log",
    ):
        self.env = env
        self.logger = get_ip_logger("spi_master_ip", log_level, log_file)
        self.tx_depth = tx_depth
        self.rx_depth = rx_depth
        self.byte_bits = byte_bits
        self.lat = {
            "register": register_latency,
            "load": load_latency,
            "shift": shift_latency,
            "capture": capture_latency,
            "interrupt": interrupt_latency,
        }

        self.register_if = simpy.Store(env)
        self.shift_queue = simpy.Store(env, capacity=4)
        self.rx_byte_queue = simpy.Store(env)
        self.interrupt_pending_queue = simpy.Store(env)

        self.config: Dict[str, Any] = {"baud_div": 4, "mode": 0, "cs_select": 0}
        self.tx_fifo: deque = deque()
        self.rx_fifo: deque = deque()
        self.interrupt_masked = False
        self.cs_asserted = False
        self.interrupts: List[Tuple[float, str]] = []
        # interrupt_if is wait_for_ack_before_next_request with outstanding_limit 1:
        # transfers continue, but the next assertion waits for the software clear.
        self.irq_clear_event: simpy.Event | None = None
        self.metrics = defaultdict(int)

        self.fsm_state = {
            "register_access": "IDLE",
            "transmit_engine": "IDLE",
            "shift_engine": "IDLE",
            "receive_engine": "IDLE",
            "interrupt_control": "IDLE",
        }

        self.env.process(self.register_access_process())
        self.env.process(self.transmit_engine_process())
        self.env.process(self.shift_engine_process())
        self.env.process(self.receive_engine_process())
        self.env.process(self.interrupt_control_process())
        self.logger.info("initialized tx_depth=%s rx_depth=%s byte_bits=%s", tx_depth, rx_depth, byte_bits)

    # ------------------------------------------------------------------ #
    # Public API (functionality_model.apis)
    # ------------------------------------------------------------------ #
    def configure(self, baud_div: int = 4, mode: int = 0, cs_select: int = 0, masked: bool = False):
        return self.register_if.put(
            {"op": "config", "baud_div": baud_div, "mode": mode, "cs_select": cs_select, "masked": masked}
        )

    def write_tx(self, byte: int):
        self.logger.info("write_tx byte=%s", byte)
        return self.register_if.put({"op": "write_tx", "byte": byte})

    def read_rx(self):
        if self.rx_fifo:
            return self.rx_fifo.popleft()
        return None

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
            if op == "config":
                self.fsm_state["register_access"] = "WRITE_CONFIG"
                self.config.update({k: update[k] for k in ("baud_div", "mode", "cs_select")})
                self.interrupt_masked = update.get("masked", False)
                self.metrics["config_writes"] += 1
                self.logger.info("config applied masked=%s", self.interrupt_masked)
            elif op == "write_tx":
                self.fsm_state["register_access"] = "WRITE_CONFIG"
                if len(self.tx_fifo) < self.tx_depth:
                    self.tx_fifo.append(update["byte"])
                else:
                    self.metrics["tx_dropped"] += 1
                    self.logger.warning("tx fifo full byte dropped")
            elif op == "clear_int":
                self.fsm_state["register_access"] = "READ_STATUS"
                self.interrupts.clear()
                if self.irq_clear_event is not None and not self.irq_clear_event.triggered:
                    self.irq_clear_event.succeed()
                    self.irq_clear_event = None
                self.metrics["clear_latency"] = self.lat["register"]
                self.logger.info("interrupt cleared")

    def transmit_engine_process(self):
        while True:
            self.fsm_state["transmit_engine"] = "IDLE"
            yield self.env.timeout(1)
            if not self.tx_fifo:
                continue
            self.fsm_state["transmit_engine"] = "LOAD_BYTE"
            byte = self.tx_fifo.popleft()
            yield self.env.timeout(self.lat["load"])
            self.fsm_state["transmit_engine"] = "ASSERT_CS"
            self.cs_asserted = True
            self.fsm_state["transmit_engine"] = "SHIFT_REQUEST"
            yield self.shift_queue.put(byte)
            self.fsm_state["transmit_engine"] = "WAIT_SHIFT_DONE"

    def shift_engine_process(self):
        while True:
            self.fsm_state["shift_engine"] = "IDLE"
            byte = yield self.shift_queue.get()
            for _ in range(self.byte_bits):
                self.fsm_state["shift_engine"] = "SHIFT_BIT"
                yield self.env.timeout(self.lat["shift"])
                self.fsm_state["shift_engine"] = "SAMPLE_MISO"
            self.fsm_state["shift_engine"] = "BYTE_COMPLETE"
            self.metrics["transmitted_bytes"] += 1
            self.metrics["transfers_completed"] += 1
            yield self.rx_byte_queue.put(byte)
            yield self.interrupt_pending_queue.put("transfer_done")
            if not self.tx_fifo and not self.shift_queue.items:
                self.fsm_state["shift_engine"] = "DEASSERT_CS"
                self.cs_asserted = False

    def receive_engine_process(self):
        while True:
            self.fsm_state["receive_engine"] = "IDLE"
            byte = yield self.rx_byte_queue.get()
            self.fsm_state["receive_engine"] = "RECEIVE_BYTE"
            yield self.env.timeout(self.lat["capture"])
            self.fsm_state["receive_engine"] = "CHECK_SPACE"
            if len(self.rx_fifo) < self.rx_depth:
                self.rx_fifo.append(byte)
                self.fsm_state["receive_engine"] = "ENQUEUE"
                self.metrics["received_bytes"] += 1
            else:
                self.fsm_state["receive_engine"] = "OVERFLOW"
                self.metrics["rx_overflows"] += 1
                self.logger.warning("rx fifo overflow byte dropped time=%s", self.env.now)

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
