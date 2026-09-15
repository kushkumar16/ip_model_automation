from __future__ import annotations

from collections import defaultdict

import simpy

from .common import get_ip_logger


class WatchdogIpModel:
    """Monitors host liveness via a countdown the host must periodically
    reload (kick) before it reaches zero, or the watchdog expires and
    asserts an expiry signal latched until the host disarms."""

    def __init__(
        self,
        env: simpy.Environment,
        reset_latency: int = 1,
        arm_latency: int = 1,
        kick_latency: int = 1,
        disarm_latency: int = 1,
        countdown_tick_latency: int = 1,
        expire_latency: int = 1,
        accept_config_latency: int = 1,
        apply_timeout_latency: int = 1,
        default_timeout_cycles: int = 100,
        log_level: str = "WARNING",
        log_file: str = "run.log",
    ):
        self.env = env
        self.logger = get_ip_logger("watchdog_ip", log_level, log_file)
        self.latency = {
            "reset": reset_latency,
            "arm": arm_latency,
            "kick": kick_latency,
            "disarm": disarm_latency,
            "countdown_tick": countdown_tick_latency,
            "expire": expire_latency,
            "accept_config": accept_config_latency,
            "apply_timeout": apply_timeout_latency,
        }
        self.metrics: dict[str, int] = defaultdict(
            int, {"kicks_received": 0, "expirations": 0, "arm_count": 0, "disarm_count": 0}
        )
        self.transition_counts: dict[str, int] = defaultdict(int)
        self.fsm_state: dict[str, str] = {}

        self.configured_timeout_cycles = default_timeout_cycles
        self.countdown_cycles = 0
        self.armed = False
        self.expired = False

        self._host_q: simpy.Store = simpy.Store(env)
        self._config_q: simpy.Store = simpy.Store(env)

        self.fsm_state["watchdog_main"] = "RESET"
        self.env.process(self.watchdog_main())
        self.fsm_state["config_intake"] = "IDLE"
        self.env.process(self.config_intake())
        self.logger.info("initialized default_timeout_cycles=%s", default_timeout_cycles)

    # ------------------------------------------------------------------ #
    # Host-facing command API
    # ------------------------------------------------------------------ #
    def arm(self):
        """host_control_if: wait_for_ack_inline, resolves at watchdog_main.ARMED."""
        accepted = self.env.event()
        self._host_q.put(("ARM", accepted))
        return accepted

    def kick(self):
        """host_control_if: wait_for_ack_inline, resolves once the countdown reloads."""
        accepted = self.env.event()
        self._host_q.put(("KICK", accepted))
        return accepted

    def disarm(self):
        """host_control_if: wait_for_ack_inline, resolves once disarmed."""
        accepted = self.env.event()
        self._host_q.put(("DISARM", accepted))
        return accepted

    def configure_timeout(self, timeout_cycles: int):
        """host_control_if: acknowledged by config_intake, not watchdog_main --
        applied independently of any countdown already running, taking effect
        only on the next arm."""
        accepted = self.env.event()
        self._config_q.put((timeout_cycles, accepted))
        return accepted

    def get_metrics(self) -> dict[str, int]:
        return dict(self.metrics)

    # ------------------------------------------------------------------ #
    # FSM processes
    # ------------------------------------------------------------------ #
    def _set_fsm_state(self, fsm: str, state: str) -> None:
        self.fsm_state[fsm] = state
        self.logger.debug("fsm=%s state=%s time=%s", fsm, state, self.env.now)

    def watchdog_main(self):
        self._set_fsm_state("watchdog_main", "RESET")
        yield self.env.timeout(self.latency["reset"])
        self.transition_counts["RESET->DISARMED"] += 1

        while True:
            self._set_fsm_state("watchdog_main", "DISARMED")
            command, accepted = yield self._host_q.get()
            if command != "ARM":
                # No declared transition from DISARMED on KICK/DISARM: there is
                # nothing to kick or cancel yet, so the command is accepted
                # with no further effect.
                accepted.succeed()
                self.logger.debug("%s ignored while disarmed", command)
                continue

            yield self.env.timeout(self.latency["arm"])
            self.armed = True
            self.countdown_cycles = self.configured_timeout_cycles
            self.metrics["arm_count"] += 1
            self.transition_counts["DISARMED->ARMED"] += 1
            self._set_fsm_state("watchdog_main", "ARMED")
            accepted.succeed()
            self.logger.info("armed timeout_cycles=%s", self.configured_timeout_cycles)

            expired = yield from self._run_armed()
            if expired:
                yield from self._run_expired()
            # Either path returns here disarmed; loop back to DISARMED.

    def _run_armed(self):
        """ARMED: race the next host command against the countdown tick.

        Returns True if the countdown reached zero (caller must then run
        _run_expired), False if the host disarmed first.
        """
        get_event = self._host_q.get()
        while True:
            tick_event = self.env.timeout(self.latency["countdown_tick"])
            result = yield get_event | tick_event
            if get_event in result:
                command, accepted = result[get_event]
                if command == "KICK":
                    # This get_event has fired and we're looping again; a fresh
                    # one must be live before the next race or a subsequent
                    # put() (kick/disarm) would have no pending getter to be
                    # consumed by.
                    get_event = self._host_q.get()
                    yield self.env.timeout(self.latency["kick"])
                    self.countdown_cycles = self.configured_timeout_cycles
                    self.metrics["kicks_received"] += 1
                    self.transition_counts["ARMED->ARMED"] += 1
                    accepted.succeed()
                    self.logger.debug("kicked, countdown reloaded to %s", self.countdown_cycles)
                    continue
                if command == "DISARM":
                    yield self.env.timeout(self.latency["disarm"])
                    self.armed = False
                    self.countdown_cycles = 0
                    self.metrics["disarm_count"] += 1
                    self.transition_counts["ARMED->DISARMED"] += 1
                    accepted.succeed()
                    self.logger.info("disarmed while counting down")
                    return False
                # ARM while already armed: no declared transition, no effect.
                get_event = self._host_q.get()
                accepted.succeed()
                self.logger.debug("%s ignored while armed", command)
                continue

            # The tick fired instead of a command: charge it and decrement.
            self.countdown_cycles -= 1
            if self.countdown_cycles > 0:
                continue

            # About to return without ever consuming get_event: cancel it so it
            # doesn't linger on the Store and silently absorb a later put()
            # (e.g. the DISARM that _run_expired() waits for next).
            get_event.cancel()
            yield self.env.timeout(self.latency["expire"])
            self.armed = False
            self.expired = True
            self.metrics["expirations"] += 1
            self.transition_counts["ARMED->EXPIRED"] += 1
            self._set_fsm_state("watchdog_main", "EXPIRED")
            self.logger.warning("watchdog expired")
            return True

    def _run_expired(self):
        """EXPIRED: latched until DISARM clears it."""
        while True:
            command, accepted = yield self._host_q.get()
            if command == "DISARM":
                yield self.env.timeout(self.latency["disarm"])
                self.expired = False
                self.metrics["disarm_count"] += 1
                self.transition_counts["EXPIRED->DISARMED"] += 1
                accepted.succeed()
                self.logger.info("disarmed, expired latch cleared")
                return
            # ARM/KICK while expired: no declared transition, no effect --
            # the host must disarm before the watchdog can be armed again.
            accepted.succeed()
            self.logger.debug("%s ignored while expired", command)

    def config_intake(self):
        self._set_fsm_state("config_intake", "IDLE")
        while True:
            timeout_cycles, accepted = yield self._config_q.get()
            yield self.env.timeout(self.latency["accept_config"])
            self.transition_counts["IDLE->APPLY_TIMEOUT"] += 1
            self._set_fsm_state("config_intake", "APPLY_TIMEOUT")
            yield self.env.timeout(self.latency["apply_timeout"])
            self.configured_timeout_cycles = timeout_cycles
            self.transition_counts["APPLY_TIMEOUT->IDLE"] += 1
            self._set_fsm_state("config_intake", "IDLE")
            accepted.succeed()
            self.logger.info("configured timeout_cycles=%s", timeout_cycles)
