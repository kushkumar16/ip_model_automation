from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

import simpy

from .common import Command, get_ip_logger

BANK_COUNT = 4
BANK_QUEUE_DEPTH = 4
CENTRAL_QUEUE_DEPTH = 16


class SramCtrlIpModel:
    """SimPy delay model of the SRAM Controller IP (rev B: four banks + ECC scrub).

    Timing follows the reviewed template's end-to-end paths: an uncontended
    read/write/RMW takes 11/8/17 cycles from request acceptance (req_valid and
    req_ready both high) to completion -- accept_and_decode + pick_bank +
    (read or write access) [+ ecc_check] [+ rmw_merge + write access]. That
    DLD-quoted total excludes push_into_bank_queue_latency and
    queue_full_retry_latency: those charge the request_accept FSM's own
    throughput (when it is ready for the *next* request), not the time
    bank_scheduler waits to see a freshly pushed entry, so a push signals the
    scheduler immediately rather than after its own trailing cycle.

    sram_banks and outstanding_request_slots are tracked as plain counters
    (bank_utilization, outstanding_requests) rather than blocking SimPy
    Resources: the template defines no stall state for exceeding them, and
    bank_scheduler is a single sequential process, so no bank is ever actually
    contended by two accesses at once.
    """

    def __init__(
        self,
        env: simpy.Environment,
        accept_and_decode_latency: int = 2,
        push_into_bank_queue_latency: int = 1,
        queue_full_retry_latency: int = 1,
        pick_bank_latency: int = 3,
        sram_read_latency: int = 4,
        sram_write_latency: int = 3,
        ecc_check_latency: int = 2,
        rmw_merge_latency: int = 3,
        scrub_interval_latency: int = 4096,
        scrub_entry_read_latency: int = 5,
        ecc_correction_writeback_latency: int = 6,
        bank_mask: int = 0b1111,
        log_level: str = "WARNING",
        log_file: str = "run.log",
    ):
        self.env = env
        self.logger = get_ip_logger("sram_ctrl_ip", log_level, log_file)
        self.accept_and_decode_latency = accept_and_decode_latency
        self.push_into_bank_queue_latency = push_into_bank_queue_latency
        self.queue_full_retry_latency = queue_full_retry_latency
        self.pick_bank_latency = pick_bank_latency
        self.sram_read_latency = sram_read_latency
        self.sram_write_latency = sram_write_latency
        self.ecc_check_latency = ecc_check_latency
        self.rmw_merge_latency = rmw_merge_latency
        self.scrub_interval_latency = scrub_interval_latency
        self.scrub_entry_read_latency = scrub_entry_read_latency
        self.ecc_correction_writeback_latency = ecc_correction_writeback_latency

        self.input_q = simpy.Store(env, capacity=CENTRAL_QUEUE_DEPTH)
        self.config_queue = simpy.Store(env, capacity=1)
        self.bank_queues: List[Deque[Command]] = [deque() for _ in range(BANK_COUNT)]
        self._bank_cursor = 0
        self._scrub_cursor = 0
        self._bank_activity = env.event()
        self._scrub_wake = env.event()
        self._rsp_ready_event = env.event()

        self.bank_mask = bank_mask
        self.scrub_enable = True
        self.scrub_interval_cycles = scrub_interval_latency
        self.ecc_correct_enable = True
        self.req_ready = True
        self.rsp_ready = True

        self.outstanding_requests: Dict[str, float] = {}
        self.forced_status: Dict[str, str] = {}
        self.scrub_forced_errors: Dict[int, bool] = {}
        self.responses: Dict[str, Dict[str, object]] = {}
        self.completed: List[tuple] = []
        self.per_requester_accepted_counts: Dict[str, int] = defaultdict(int)
        self.bank_utilization: Dict[int, int] = defaultdict(int)

        self.metrics = defaultdict(
            int,
            {
                "transition_counts": 0,
                "latency": 0,
                "queue_full_stalls": 0,
                "bank_conflicts": 0,
                "corrected_count": 0,
                "uncorrectable_count": 0,
                "queue_high_water": 0,
                "per_requester_accepted_counts": 0,
                "bank_utilization": 0,
            },
        )

        self.fsm_state: Dict[str, str] = {
            "request_accept": "RESET",
            "bank_scheduler": "SCHED_IDLE",
            "ecc_scrub": "SCRUB_WAIT",
        }

        self.env.process(self.request_accept_process())
        self.env.process(self.bank_scheduler_process())
        self.env.process(self.ecc_scrub_process())
        self.logger.info("initialized bank_mask=%s", bin(self.bank_mask))

    # ------------------------------------------------------------------ #
    # Public API (functionality_model.apis)
    # ------------------------------------------------------------------ #
    def submit_request(self, command: Command):
        self.logger.info("submit_request req=%s kind=%s addr=%s", command.cmd_id, command.kind, command.addr)
        return self.input_q.put(command)

    def get_response(self, req_id: str) -> Optional[Dict[str, object]]:
        return self.responses.get(req_id)

    def configure(self, **updates) -> simpy.Event:
        self.logger.info("configure request=%s", updates)
        return self.config_queue.put(updates)

    def trigger_scrub_now(self) -> None:
        if not self._scrub_wake.triggered:
            self._scrub_wake.succeed()

    def set_response_ready(self, ready: bool) -> None:
        self.rsp_ready = ready
        self.logger.info("rsp_ready=%s", ready)
        if ready:
            if not self._rsp_ready_event.triggered:
                self._rsp_ready_event.succeed()
        else:
            self._rsp_ready_event = self.env.event()

    def get_metrics(self) -> Dict[str, int]:
        return dict(self.metrics)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _set_fsm_state(self, fsm: str, state: str) -> None:
        self.fsm_state[fsm] = state
        self.metrics["transition_counts"] += 1
        self.logger.debug("fsm=%s state=%s time=%s", fsm, state, self.env.now)

    def _bank_enabled(self, bank: int) -> bool:
        return bool(self.bank_mask & (1 << bank))

    def _decode_bank(self, addr: int) -> int:
        return addr % BANK_COUNT

    def _signal_bank_activity(self) -> None:
        if not self._bank_activity.triggered:
            self._bank_activity.succeed()

    def _select_bank(self) -> Optional[int]:
        for offset in range(BANK_COUNT):
            bank = (self._bank_cursor + offset) % BANK_COUNT
            if self._bank_enabled(bank) and self.bank_queues[bank]:
                self._bank_cursor = (bank + 1) % BANK_COUNT
                return bank
        return None

    def _apply_config(self, updates: Dict[str, object]) -> None:
        if "scrub_enable" in updates:
            self.scrub_enable = bool(updates["scrub_enable"])
        if "scrub_interval_cycles" in updates:
            self.scrub_interval_cycles = int(updates["scrub_interval_cycles"])
        if "ecc_correct_enable" in updates:
            self.ecc_correct_enable = bool(updates["ecc_correct_enable"])
        if "bank_mask" in updates:
            self.bank_mask = int(updates["bank_mask"])
        self.metrics["config_applications"] += 1
        self.logger.info("configuration applied %s", updates)

    def _complete(self, command: Command, start_time: float, status: str) -> None:
        latency = self.env.now - start_time
        command.status = status
        self.completed.append((self.env.now, command))
        self.metrics["completed_commands"] += 1
        self.metrics[f"completed_{command.kind.lower()}"] += 1
        self.metrics["latency"] = latency
        if command.kind == "READ":
            self.responses[command.cmd_id] = {"rsp_id": command.cmd_id, "rsp_status": status, "latency": latency}
        self.logger.info("completed req=%s kind=%s status=%s latency=%s", command.cmd_id, command.kind, status, latency)

    # ------------------------------------------------------------------ #
    # FSM processes
    # ------------------------------------------------------------------ #
    def request_accept_process(self):
        """FSM `request_accept` states: RESET, ACCEPT_IDLE, DECODE_BANK, PUSH_QUEUE, QUEUE_FULL."""
        self._set_fsm_state("request_accept", "RESET")
        yield self.env.timeout(0)
        while True:
            self._set_fsm_state("request_accept", "ACCEPT_IDLE")
            command = yield self.input_q.get()
            accept_time = self.env.now
            self._set_fsm_state("request_accept", "DECODE_BANK")
            yield self.env.timeout(self.accept_and_decode_latency)
            bank = self._decode_bank(command.addr)

            if command.kind == "RMW" and not self._bank_enabled(bank):
                self.metrics["rmw_masked_bank_rejections"] += 1
                self.logger.error(
                    "rmw targets masked bank req=%s bank=%s mask=%s", command.cmd_id, bank, bin(self.bank_mask)
                )
                continue

            if len(self.bank_queues[bank]) >= BANK_QUEUE_DEPTH:
                self._set_fsm_state("request_accept", "QUEUE_FULL")
                self.req_ready = False
                self.metrics["queue_full_stalls"] += 1
                self.logger.warning("bank queue full bank=%s req=%s", bank, command.cmd_id)
                while len(self.bank_queues[bank]) >= BANK_QUEUE_DEPTH:
                    yield self.env.timeout(self.queue_full_retry_latency)
                self.req_ready = True

            self._set_fsm_state("request_accept", "PUSH_QUEUE")
            if self.bank_queues[bank]:
                self.metrics["bank_conflicts"] += 1
            # Pushed and signalled *before* the trailing cycle below: the
            # DLD's own end-to-end formula (11/8/17 cycles) excludes this
            # push cost from the request's critical path, so bank_scheduler
            # must see the entry the instant decode finishes.
            self.bank_queues[bank].append(command)
            self.outstanding_requests[command.cmd_id] = accept_time
            self.per_requester_accepted_counts[command.source_id] += 1
            self.metrics["per_requester_accepted_counts"] += 1
            occupancy = sum(len(queue) for queue in self.bank_queues)
            self.metrics["queue_high_water"] = max(self.metrics["queue_high_water"], occupancy)
            self._signal_bank_activity()
            self.logger.info("accepted req=%s kind=%s bank=%s", command.cmd_id, command.kind, bank)
            yield self.env.timeout(self.push_into_bank_queue_latency)

    def bank_scheduler_process(self):
        """FSM `bank_scheduler` states: SCHED_IDLE, PICK_BANK, ISSUE_ACCESS, WAIT_ECC, RETURN_DATA, RMW_MERGE."""
        while True:
            self._set_fsm_state("bank_scheduler", "SCHED_IDLE")
            bank = self._select_bank()
            if bank is None:
                yield self._bank_activity
                self._bank_activity = self.env.event()
                continue

            self._set_fsm_state("bank_scheduler", "PICK_BANK")
            yield self.env.timeout(self.pick_bank_latency)
            command = self.bank_queues[bank].popleft()
            start_time = self.outstanding_requests.pop(command.cmd_id)

            if command.kind == "WRITE":
                self._set_fsm_state("bank_scheduler", "ISSUE_ACCESS")
                self._touch_bank(bank)
                yield self.env.timeout(self.sram_write_latency)
                self._complete(command, start_time, "OK")
                continue

            self._set_fsm_state("bank_scheduler", "ISSUE_ACCESS")
            self._touch_bank(bank)
            yield self.env.timeout(self.sram_read_latency)
            self._set_fsm_state("bank_scheduler", "WAIT_ECC")
            yield self.env.timeout(self.ecc_check_latency)
            status = self.forced_status.pop(command.cmd_id, "OK")
            if status == "UNCORRECTABLE":
                self.metrics["uncorrectable_count"] += 1
                self.logger.error("uncorrectable ecc error req=%s bank=%s", command.cmd_id, bank)
            elif status == "CORRECTED":
                self.metrics["corrected_count"] += 1

            if command.kind == "RMW":
                if status == "UNCORRECTABLE":
                    self._complete(command, start_time, status)
                    continue
                self._set_fsm_state("bank_scheduler", "RMW_MERGE")
                yield self.env.timeout(self.rmw_merge_latency)
                self._set_fsm_state("bank_scheduler", "ISSUE_ACCESS")
                self._touch_bank(bank)
                yield self.env.timeout(self.sram_write_latency)
                self._complete(command, start_time, "OK")
                continue

            self._set_fsm_state("bank_scheduler", "RETURN_DATA")
            while not self.rsp_ready:
                yield self._rsp_ready_event
            self._complete(command, start_time, status)

    def _touch_bank(self, bank: int) -> None:
        self.bank_utilization[bank] += 1
        self.metrics["bank_utilization"] += 1

    def ecc_scrub_process(self):
        """FSM `ecc_scrub` states: SCRUB_WAIT, LOAD_CONFIG, READ_ENTRY, CORRECT_ENTRY, REPORT."""
        while True:
            self._set_fsm_state("ecc_scrub", "SCRUB_WAIT")
            wake = self._scrub_wake
            yield wake | self.env.timeout(self.scrub_interval_cycles)
            if wake.triggered:
                self._scrub_wake = self.env.event()

            self._set_fsm_state("ecc_scrub", "LOAD_CONFIG")
            if self.config_queue.items:
                updates = yield self.config_queue.get()
                self._apply_config(updates)
            if not self.scrub_enable:
                continue

            self._set_fsm_state("ecc_scrub", "READ_ENTRY")
            bank = self._scrub_cursor
            self._scrub_cursor = (self._scrub_cursor + 1) % BANK_COUNT
            if self.bank_queues[bank]:
                self.metrics["scrub_yields_to_demand"] += 1
                self.logger.debug("scrub yields to demand bank=%s", bank)
                continue

            yield self.env.timeout(self.scrub_entry_read_latency)
            correctable = self.scrub_forced_errors.pop(bank, False)
            if correctable and self.ecc_correct_enable:
                self._set_fsm_state("ecc_scrub", "CORRECT_ENTRY")
                yield self.env.timeout(self.ecc_correction_writeback_latency)
                self.metrics["corrected_count"] += 1
                self.logger.info("scrub corrected entry bank=%s", bank)

            self._set_fsm_state("ecc_scrub", "REPORT")
            self.metrics["scrub_cycles"] += 1
