from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Optional

import simpy

from .arbitration_ip import ArbitrationIpModel
from .common import Command, get_ip_logger
from .completion_ip import CompletionIpModel

DEFAULT_TOPOLOGY = {"port0": {"tenants": {"T0": ["SQ0"], "T1": ["SQ0"]}}}


class StoragePipelineSubsystemModel:
    """SimPy delay model of the storage pipeline subsystem.

    Composes two member IP models -- the Arbitration IP and the Completion
    IP -- with three glue processes that bridge their boundaries: a host
    submits a command or a QoS config request once, to this subsystem, and
    `intake_bridge` forwards it to whichever member owns it. `dispatch_bridge`
    watches the Arbitration IP's own issued-command record and forwards every
    newly issued command into the Completion IP for QoS-accounted completion.
    `backpressure_monitor` samples the Completion IP's pending backlog and
    throttles the Arbitration IP's issue readiness when it grows too large.

    Glue timing comes from this subsystem's own template; each member IP's
    timing comes from its own reviewed template (overridable via *_kwargs).
    """

    def __init__(
        self,
        env: simpy.Environment,
        arbitration_kwargs: Optional[Dict[str, Any]] = None,
        completion_kwargs: Optional[Dict[str, Any]] = None,
        completion_backlog_limit: int = 4,
        default_token_budget: float = 1_000_000.0,
        log_level: str = "WARNING",
        log_file: str = "run.log",
    ):
        self.env = env
        self.logger = get_ip_logger("storage_pipeline_subsystem", log_level, log_file)
        self.completion_backlog_limit = completion_backlog_limit

        arbitration_kwargs = dict(arbitration_kwargs or {})
        completion_kwargs = dict(completion_kwargs or {})
        for kwargs in (arbitration_kwargs, completion_kwargs):
            kwargs.setdefault("log_level", log_level)
            kwargs.setdefault("log_file", log_file)
        arbitration_kwargs.setdefault("topology", DEFAULT_TOPOLOGY)
        arbitration_kwargs.setdefault("port_mode", "single")
        arbitration_kwargs.setdefault("weights", {"T0": 1, "T1": 1})
        completion_kwargs.setdefault("tenant_weights", {"T0": 1, "T1": 1})

        self.arbitration = ArbitrationIpModel(env, **arbitration_kwargs)
        self.completion = CompletionIpModel(env, **completion_kwargs)

        for tenant_id in ("T0", "T1"):
            self.completion.configure_tenant(
                tenant_id,
                read=default_token_budget,
                write=default_token_budget,
                read_bw=default_token_budget,
                write_bw=default_token_budget,
            )

        # intake_queue: declared depth `unbounded` with a depth_note that
        # backpressure comes from the host not outrunning the bridge, not from
        # a subsystem-level capacity -- so an unbounded simpy.Store is exactly
        # what the template describes, not a scaffold shortcut.
        self.intake_queue: simpy.Store = simpy.Store(env)
        self._issued_seen = 0
        self.issue_throttled = False
        self.metrics: Dict[str, int] = defaultdict(int)
        self.transition_counts: Dict[str, int] = defaultdict(int)
        self.fsm_state: Dict[str, str] = {
            "intake_bridge": "IDLE",
            "dispatch_bridge": "POLL_ISSUED",
            "backpressure_monitor": "SAMPLE_BACKLOG",
        }

        self.env.process(self.intake_bridge())
        self.env.process(self.dispatch_bridge())
        self.env.process(self.backpressure_monitor())
        self.logger.info("initialized backlog_limit=%s", completion_backlog_limit)

    # ------------------------------------------------------------------ #
    # Public API (functionality_model.apis)
    # ------------------------------------------------------------------ #
    def submit(self, command: Command):
        """command_intake_if: offer a command, returning the host's accept.

        wait_for_ack_inline with its wait point at intake_bridge.ACCEPT_COMMAND
        -- the host needs no result, only the accept that the command was
        handed to the Arbitration IP. The returned event completes there, not
        at the instant of this call: an unbounded Store's own `put` completes
        immediately regardless of subsystem state, which would ack the host at
        t=0 whether or not anything downstream could accept the command yet.
        """
        self.logger.info("submit cmd=%s kind=%s tenant=%s", command.cmd_id, command.kind, command.tenant_id)
        accepted = self.env.event()
        self.intake_queue.put(("command", command, accepted))
        return accepted

    def configure_qos(self, tenant_id: str, token_budget: float):
        """qos_configuration_if: same inline-ack shape as submit(), completing
        at intake_bridge.APPLY_QOS_CONFIG once the Completion IP is updated."""
        self.logger.info("configure_qos tenant=%s budget=%s", tenant_id, token_budget)
        accepted = self.env.event()
        self.intake_queue.put(("qos_config", (tenant_id, token_budget), accepted))
        return accepted

    def get_metrics(self) -> Dict[str, int]:
        return dict(self.metrics)

    # ------------------------------------------------------------------ #
    # FSM processes (glue)
    # ------------------------------------------------------------------ #
    def intake_bridge(self):
        while True:
            self.fsm_state["intake_bridge"] = "IDLE"
            kind, payload, accepted = yield self.intake_queue.get()
            if kind == "command":
                self.fsm_state["intake_bridge"] = "ACCEPT_COMMAND"
                yield self.env.timeout(1)
                self.transition_counts["IDLE->ACCEPT_COMMAND"] += 1
                self.fsm_state["intake_bridge"] = "FORWARD_TO_ARBITRATION"
                yield self.env.timeout(1)
                self.arbitration.enqueue(payload)
                self.metrics["commands_submitted"] += 1
                self.transition_counts["ACCEPT_COMMAND->FORWARD_TO_ARBITRATION"] += 1
                # command_intake_if's wait point is ACCEPT_COMMAND, resuming
                # on command_forwarded; its timing_notes tie the accept to
                # enqueue_into_arbitration_ip specifically -- the action the
                # transitions table declares on this exact transition, paid
                # above. The ack used to wait one more full transition,
                # through FORWARD_TO_ARBITRATION -> IDLE, acking the host a
                # cycle later than the contract states (round thirteen's M2).
                accepted.succeed()
                self.logger.debug("command forwarded cmd=%s", payload.cmd_id)
                # FORWARD_TO_ARBITRATION -> IDLE (action: acknowledge_host)
                # is still its own declared 1-cycle transition, paid here,
                # after the host's own ack -- the bridge's own return to
                # IDLE is not on the host's critical path.
                yield self.env.timeout(1)
                self.transition_counts["FORWARD_TO_ARBITRATION->IDLE"] += 1
            else:
                tenant_id, token_budget = payload
                self.fsm_state["intake_bridge"] = "ACCEPT_QOS_CONFIG"
                yield self.env.timeout(1)
                self.transition_counts["IDLE->ACCEPT_QOS_CONFIG"] += 1
                self.fsm_state["intake_bridge"] = "APPLY_QOS_CONFIG"
                yield self.env.timeout(1)
                # CONFIGURE_QOS: "applied to all four Completion IP token
                # buckets alike" -- read_bw/write_bw take the same budget as
                # read/write, not a scaled bandwidth unit nothing declares
                # (round thirteen's M3).
                self.completion.configure_tenant(
                    tenant_id,
                    read=token_budget,
                    write=token_budget,
                    read_bw=token_budget,
                    write_bw=token_budget,
                )
                self.metrics["qos_configs_applied"] += 1
                self.transition_counts["ACCEPT_QOS_CONFIG->APPLY_QOS_CONFIG"] += 1
                # APPLY_QOS_CONFIG -> IDLE (action: acknowledge_host) is its
                # own declared 1-cycle transition (round thirteen's M2).
                yield self.env.timeout(1)
                self.transition_counts["APPLY_QOS_CONFIG->IDLE"] += 1
                accepted.succeed()
                self.logger.debug("qos config applied tenant=%s budget=%s", tenant_id, token_budget)

    def dispatch_bridge(self):
        # One issued-but-unforwarded command per pass, not a batch drain: the
        # declared POLL_ISSUED <-> FORWARD_TO_COMPLETION pair (plus the
        # POLL_ISSUED self-loop when nothing is new) is what a reader of the
        # template sees, so the model returns to POLL_ISSUED, for real,
        # between every command forwarded rather than looping inside
        # FORWARD_TO_COMPLETION for a whole batch.
        while True:
            self.fsm_state["dispatch_bridge"] = "POLL_ISSUED"
            yield self.env.timeout(1)
            if self._issued_seen >= len(self.arbitration.issued):
                self.transition_counts["POLL_ISSUED->POLL_ISSUED"] += 1
                continue
            _time, command = self.arbitration.issued[self._issued_seen]
            self._issued_seen += 1
            self.transition_counts["POLL_ISSUED->FORWARD_TO_COMPLETION"] += 1
            self.fsm_state["dispatch_bridge"] = "FORWARD_TO_COMPLETION"
            yield self.env.timeout(1)
            yield self.completion.submit(command)
            self.metrics["completion_submitted"] += 1
            self.transition_counts["FORWARD_TO_COMPLETION->POLL_ISSUED"] += 1
            self.logger.debug("command forwarded to completion cmd=%s", command.cmd_id)

    def backpressure_monitor(self):
        while True:
            self.fsm_state["backpressure_monitor"] = "SAMPLE_BACKLOG"
            yield self.env.timeout(2)
            backlog = sum(len(queue) for queue in self.completion.pending.values())
            backlogged = backlog >= self.completion_backlog_limit
            if backlogged == self.issue_throttled:
                self.transition_counts["SAMPLE_BACKLOG->SAMPLE_BACKLOG"] += 1
                continue
            self.fsm_state["backpressure_monitor"] = "APPLY_THROTTLE"
            yield self.env.timeout(1)
            self.transition_counts["SAMPLE_BACKLOG->APPLY_THROTTLE"] += 1
            self.issue_throttled = backlogged
            self.arbitration.set_issue_ready(not backlogged)
            self.transition_counts["APPLY_THROTTLE->SAMPLE_BACKLOG"] += 1
            if backlogged:
                self.metrics["backpressure_events"] += 1
                self.logger.warning("completion backlog=%s, arbitration issue throttled", backlog)
            else:
                self.logger.info("completion backlog=%s, arbitration issue readiness restored", backlog)
