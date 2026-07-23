from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

import simpy

from .common import get_ip_logger


class TimerIpModel:
    def __init__(
        self,
        env: simpy.Environment,
        tick_period=1,
        register_latency=0,
        sync_latency=0,
        prescaler_latency=0,
        counter_latency=0,
        compare_latency=0,
        interrupt_latency=11,
        watchdog_latency=1,
        debug_freeze_latency=2,
        log_level: str = "WARNING",
        log_file: str = "run.log",
    ):
        self.env = env
        self.logger = get_ip_logger("timer_ip", log_level, log_file)
        self.tick_period = tick_period
        self.lat = {
            "register": register_latency,
            "sync": sync_latency,
            "prescaler": prescaler_latency,
            "counter": counter_latency,
            "compare": compare_latency,
            "interrupt": interrupt_latency,
            "watchdog": watchdog_latency,
            "debug_freeze": debug_freeze_latency,
        }

        self.register_if = simpy.Store(env)
        self.register_update_queue = simpy.Store(env, capacity=4)
        self.effective_tick_queue = simpy.Store(env, capacity=8)
        self.compare_event_queue = simpy.Store(env, capacity=8)
        self.interrupt_pending_queue = simpy.Store(env, capacity=8)
        self.watchdog_kick_queue = simpy.Store(env)

        self.register_shadow: Dict[int, Dict[str, Any]] = {}
        self.channels: Dict[int, Dict[str, Any]] = {}
        self.events: List[int] = []
        self.interrupts: List[tuple[float, int]] = []
        self.interrupt_status = set()
        self.interrupt_mask = defaultdict(lambda: True)
        # interrupt_if is wait_for_ack_before_next_request with outstanding_limit 1:
        # counting continues, but the next assertion waits for the software clear.
        self.irq_clear_events: Dict[int, simpy.Event] = {}
        self.metrics = defaultdict(int)
        self.frozen = False
        self.freeze_requested = False
        self.fsm_state = {
            "register_access": "IDLE",
            "configuration_synchronizer": "WAIT_UPDATE",
            "prescaler": "WAIT_RAW_TICK",
            "counter": "DISABLED",
            "compare_event": "WAIT_COUNTER_UPDATE",
            "interrupt_aggregation": "IDLE",
            "watchdog": "DISABLED",
            "debug_freeze": "RUN",
        }
        self.watchdog = {
            "enabled": False,
            "timeout": 0,
            "last_kick": 0.0,
            "interrupt": True,
        }

        self.env.process(self.register_access_process())
        self.env.process(self.configuration_synchronizer_process())
        self.env.process(self.prescaler_process())
        self.env.process(self.counter_process())
        self.env.process(self.compare_event_process())
        self.env.process(self.interrupt_aggregation_process())
        self.env.process(self.watchdog_process())
        self.env.process(self.debug_freeze_process())
        self.logger.info("initialized tick_period=%s", self.tick_period)

    def configure(self, channel_id: int, mode: str, compare: int, reload: int = 0, prescale: int = 1) -> None:
        self.write_register(
            {
                "op": "configure",
                "channel_id": channel_id,
                "mode": mode,
                "compare": compare,
                "reload": reload,
                "prescale": max(1, prescale),
            }
        )

    def write_register(self, update: Dict[str, Any]):
        return self.register_if.put(update)

    def clear_interrupt(self, channel_id: int) -> None:
        self.interrupt_status.discard(channel_id)
        self.metrics["clear_latency"] = self.lat["register"]
        # Releases the aggregation process parked in WAIT_SW_CLEAR for this channel.
        clear_event = self.irq_clear_events.pop(channel_id, None)
        if clear_event is not None and not clear_event.triggered:
            clear_event.succeed()
        self.logger.info("interrupt cleared channel=%s", channel_id)

    def read_counter(self, channel_id: int) -> simpy.Event:
        """Register read: `value = yield model.read_counter(channel)`.

        register_if is `wait_for_response` -- software blocks on the access and
        consumes what comes back, so the read costs a register access.
        """
        response = self.env.event()
        self.register_if.put({"op": "read", "channel_id": channel_id, "response": response})
        return response

    def configure_watchdog(self, timeout: int, interrupt: bool = True) -> None:
        self.watchdog.update({"enabled": True, "timeout": timeout, "last_kick": self.env.now, "interrupt": interrupt})
        self.logger.info("watchdog configured timeout=%s interrupt=%s", timeout, interrupt)

    def kick_watchdog(self):
        self.logger.info("watchdog kick time=%s", self.env.now)
        return self.watchdog_kick_queue.put(self.env.now)

    def set_debug_freeze(self, frozen: bool) -> None:
        self.freeze_requested = frozen
        self.logger.info("debug_freeze_requested=%s", frozen)

    def tick_functional(self) -> None:
        for channel_id in list(self.channels):
            self._emit_effective_tick(channel_id)

    def _apply_config(self, update: Dict[str, Any]) -> None:
        if update.get("op") != "configure":
            return
        channel_id = update["channel_id"]
        self.channels[channel_id] = {
            "mode": update["mode"],
            "compare": update["compare"],
            "reload": update["reload"],
            "prescale": update["prescale"],
            "divider": 0,
            "count": update["reload"],
            "enabled": True,
        }
        self.metrics["config_applied"] += 1
        self.logger.info("config applied channel=%s mode=%s compare=%s", channel_id, update["mode"], update["compare"])

    def _emit_effective_tick(self, channel_id: int) -> None:
        state = self.channels[channel_id]
        if not state["enabled"] or self.frozen:
            if self.frozen and state["enabled"]:
                self.metrics["debug_frozen_ticks"] += 1
            return
        state["divider"] += 1
        if state["divider"] < state["prescale"]:
            return
        state["divider"] = 0
        self.effective_tick_queue.put(channel_id)
        self.metrics["effective_ticks"] += 1

    def register_access_process(self):
        while True:
            self.fsm_state["register_access"] = "IDLE"
            update = yield self.register_if.get()
            self.fsm_state["register_access"] = "DECODE_ACCESS"
            yield self.env.timeout(self.lat["register"])
            if update.get("op") == "read":
                self.fsm_state["register_access"] = "READ_RETURN"
                channel_id = update["channel_id"]
                update["response"].succeed(
                    {
                        "count": self.channels.get(channel_id, {}).get("count", 0),
                        "enabled": self.channels.get(channel_id, {}).get("enabled", False),
                        "interrupt_pending": channel_id in self.interrupt_status,
                    }
                )
                self.metrics["register_reads"] += 1
                continue
            self.fsm_state["register_access"] = "WRITE_SHADOW"
            if update.get("op") == "configure":
                self.register_shadow[update["channel_id"]] = dict(update)
            yield self.register_update_queue.put(update)
            self.metrics["register_writes"] += 1

    def configuration_synchronizer_process(self):
        while True:
            self.fsm_state["configuration_synchronizer"] = "WAIT_UPDATE"
            update = yield self.register_update_queue.get()
            self.fsm_state["configuration_synchronizer"] = "SYNC_STAGE_0"
            yield self.env.timeout(self.lat["sync"])
            self.fsm_state["configuration_synchronizer"] = "APPLY_CONFIG"
            self._apply_config(update)
            self.fsm_state["configuration_synchronizer"] = "ACK_UPDATE"
            self.metrics["config_latency"] = self.lat["register"] + self.lat["sync"]

    def prescaler_process(self):
        while True:
            self.fsm_state["prescaler"] = "WAIT_RAW_TICK"
            yield self.env.timeout(self.tick_period)
            self.fsm_state["prescaler"] = "INCREMENT_DIVIDER"
            yield self.env.timeout(self.lat["prescaler"])
            for channel_id in list(self.channels):
                self._emit_effective_tick(channel_id)
            self.fsm_state["prescaler"] = "EMIT_EFFECTIVE_TICK"

    def counter_process(self):
        while True:
            self.fsm_state["counter"] = "DISABLED" if not self.channels else "COUNT"
            channel_id = yield self.effective_tick_queue.get()
            state = self.channels.get(channel_id)
            if state is None or not state["enabled"]:
                continue
            yield self.env.timeout(self.lat["counter"])
            state["count"] += 1
            self.metrics["counter_updates"] += 1
            if state["count"] >= state["compare"]:
                self.fsm_state["counter"] = "COMPARE_PENDING"
                yield self.compare_event_queue.put(channel_id)
                if state["mode"] == "ONE_SHOT":
                    state["enabled"] = False
                    self.fsm_state["counter"] = "STOPPED"
                elif state["mode"] == "PERIODIC":
                    state["count"] = state["reload"]
                    self.fsm_state["counter"] = "RELOAD"

    def compare_event_process(self):
        while True:
            self.fsm_state["compare_event"] = "WAIT_COUNTER_UPDATE"
            channel_id = yield self.compare_event_queue.get()
            self.fsm_state["compare_event"] = "CHECK_COMPARE"
            yield self.env.timeout(self.lat["compare"])
            self.fsm_state["compare_event"] = "EVENT_ASSERT"
            self.events.append(channel_id)
            self.interrupt_status.add(channel_id)
            self.metrics["compare_events"] = len(self.events)
            self.logger.info("compare event channel=%s time=%s", channel_id, self.env.now)
            yield self.interrupt_pending_queue.put(channel_id)

    def interrupt_aggregation_process(self):
        while True:
            self.fsm_state["interrupt_aggregation"] = "IDLE"
            channel_id = yield self.interrupt_pending_queue.get()
            self.fsm_state["interrupt_aggregation"] = "COLLECT_EVENTS"
            yield self.env.timeout(self.lat["interrupt"])
            self.fsm_state["interrupt_aggregation"] = "APPLY_MASK"
            if not self.interrupt_mask[channel_id]:
                self.metrics["masked_events"] += 1
                self.logger.warning("interrupt masked channel=%s", channel_id)
                continue
            self.fsm_state["interrupt_aggregation"] = "ASSERT_IRQ"
            self.interrupts.append((self.env.now, channel_id))
            self.metrics["interrupt_count"] += 1
            self.logger.info("interrupt asserted channel=%s time=%s", channel_id, self.env.now)
            # Level interrupt: aggregation parks here until software clears the
            # status, so a second event cannot raise a second IRQ unserviced.
            clear_event = self.env.event()
            self.irq_clear_events[channel_id] = clear_event
            self.fsm_state["interrupt_aggregation"] = "WAIT_SW_CLEAR"
            self.metrics["irq_clear_waits"] += 1
            yield clear_event
            self.fsm_state["interrupt_aggregation"] = "DEASSERT_IRQ"
            self.logger.debug("interrupt deasserted channel=%s time=%s", channel_id, self.env.now)

    def watchdog_process(self):
        while True:
            if not self.watchdog["enabled"]:
                self.fsm_state["watchdog"] = "DISABLED"
                yield self.env.timeout(1)
                continue
            self.fsm_state["watchdog"] = "WAIT_KICK_OR_TIMEOUT"
            if self.watchdog_kick_queue.items:
                kick_time = yield self.watchdog_kick_queue.get()
                self.fsm_state["watchdog"] = "KICK_RELOAD"
                self.watchdog["last_kick"] = kick_time
                self.metrics["watchdog_kicks"] += 1
            elif self.env.now - self.watchdog["last_kick"] >= self.watchdog["timeout"]:
                self.fsm_state["watchdog"] = "TIMEOUT"
                yield self.env.timeout(self.lat["watchdog"])
                self.metrics["watchdog_timeouts"] += 1
                self.watchdog["last_kick"] = self.env.now
                self.logger.critical("watchdog timeout time=%s", self.env.now)
                if self.watchdog["interrupt"]:
                    yield self.interrupt_pending_queue.put(-1)
            yield self.env.timeout(1)

    def debug_freeze_process(self):
        applied = False
        while True:
            if self.freeze_requested and not applied:
                self.fsm_state["debug_freeze"] = "FREEZE_REQUEST"
                yield self.env.timeout(self.lat["debug_freeze"])
                self.frozen = True
                applied = True
                self.fsm_state["debug_freeze"] = "FROZEN"
                self.metrics["debug_freezes"] += 1
                self.logger.warning("debug frozen time=%s", self.env.now)
            elif not self.freeze_requested and applied:
                self.fsm_state["debug_freeze"] = "RESUME_REQUEST"
                yield self.env.timeout(self.lat["debug_freeze"])
                self.frozen = False
                applied = False
                self.fsm_state["debug_freeze"] = "RUN"
                self.metrics["debug_resumes"] += 1
                self.logger.info("debug resumed time=%s", self.env.now)
            yield self.env.timeout(1)
