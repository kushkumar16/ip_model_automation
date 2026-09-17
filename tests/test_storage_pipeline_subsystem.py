import unittest

import simpy

from ip_model_automation.common import Command
from ip_model_automation.ip import StoragePipelineSubsystemModel

FAST_ARBITRATION = {
    "scan_latency": 1,
    "issue_latency": 1,
    "grant_latency": 1,
    "selection_accept_latency": 1,
    "pending_count_latency": 1,
    "burst_read_latency": 1,
    "burst_calc_latency": 1,
    "burst_debit_latency": 1,
    "bitmap_latency": 1,
    "issue_slot_latency": 1,
}

FAST_COMPLETION = {
    "service_latency": 1,
    "tenant_select_latency": 1,
    "token_check_latency": 1,
    "emit_latency": 1,
    "retry_latency": 1,
    "port_latency": 1,
}


def make_subsystem(env, **overrides):
    kwargs = {
        "arbitration_kwargs": dict(FAST_ARBITRATION),
        "completion_kwargs": dict(FAST_COMPLETION),
        "log_level": "CRITICAL",
    }
    for key, value in overrides.items():
        if key.endswith("_kwargs"):
            kwargs[key].update(value)
        else:
            kwargs[key] = value
    return StoragePipelineSubsystemModel(env, **kwargs)


class TestStoragePipelineSubsystemModel(unittest.TestCase):
    def test_command_ack_completes_at_enqueue_not_after_the_return_transition(self):
        """command_intake_if's ack ties to enqueue_into_arbitration_ip --
        the action ACCEPT_COMMAND -> FORWARD_TO_ARBITRATION's own transitions
        table entry declares -- not to the bridge's own separate
        FORWARD_TO_ARBITRATION -> IDLE return. The ack used to wait for that
        return transition too, acking the host one glue cycle later than
        command_intake_if's timing_notes state (round thirteen's M2 was
        about giving that return transition its own declared cost, a
        different question from when the host's own ack fires).
        """
        env = simpy.Environment()
        model = make_subsystem(env)
        accepted = model.submit(Command("c0", "READ", port_id="port0", tenant_id="T0", sq_id="SQ0"))

        acked_at = None
        while env.peek() < 10:
            env.step()
            if accepted.processed and acked_at is None:
                acked_at = env.now

        self.assertIsNotNone(acked_at, "the ack never fired")
        self.assertEqual(
            acked_at,
            2,
            "must be acked at ACCEPT_COMMAND(1) + FORWARD_TO_ARBITRATION's own enqueue(1) = 2, "
            "not one glue cycle later after the bridge's own return to IDLE",
        )

    def test_command_flows_end_to_end(self):
        """template test_scenarios[0]: command_flows_end_to_end.

        A single command submitted to the subsystem is scheduled by the
        Arbitration IP and recorded complete by the Completion IP -- the
        whole point of composing the two, proven with a real, non-synthetic
        model rather than the wiring-only fixtures check_subsystem_wiring.py
        tests itself against.
        """
        env = simpy.Environment()
        model = make_subsystem(env)
        submitted_at = env.now
        model.submit(Command("c0", "READ", port_id="port0", tenant_id="T0", sq_id="SQ0"))
        env.run(until=80)

        self.assertEqual(model.get_metrics()["commands_submitted"], 1)
        self.assertEqual(model.get_metrics()["completion_submitted"], 1)
        self.assertEqual(len(model.completion.completed), 1)
        completed_at, completed_command = model.completion.completed[0]
        self.assertEqual(completed_command.cmd_id, "c0")
        # expected_performance_properties: [end_to_end_latency_measured] --
        # the whole point of this scenario is a real, non-zero latency
        # through both member IPs and both glue processes, not just that
        # completion eventually happened (round thirteen's M4).
        self.assertGreater(completed_at - submitted_at, 0)
        # fsm_coverage: both bridges must actually pass through every declared
        # intermediate state, not just start and end in IDLE/POLL_ISSUED.
        self.assertGreater(model.transition_counts["IDLE->ACCEPT_COMMAND"], 0)
        self.assertGreater(model.transition_counts["ACCEPT_COMMAND->FORWARD_TO_ARBITRATION"], 0)
        self.assertGreater(model.transition_counts["POLL_ISSUED->FORWARD_TO_COMPLETION"], 0)
        self.assertGreater(model.transition_counts["FORWARD_TO_COMPLETION->POLL_ISSUED"], 0)
        # dispatch_bridge must also have polled and found nothing, at least
        # once, over an 80-cycle run -- the empty-poll path is real, not dead.
        self.assertGreater(model.transition_counts["POLL_ISSUED->POLL_ISSUED"], 0)
        self.assertEqual(model.fsm_state["intake_bridge"], "IDLE")
        self.assertEqual(model.fsm_state["dispatch_bridge"], "POLL_ISSUED")

    def test_multiple_commands_across_both_tenants_all_complete(self):
        """Both intake_bridge and dispatch_bridge must handle a real backlog,
        not just a single in-flight command, and both configured tenants must
        reach completion, not just the first one enqueued."""
        env = simpy.Environment()
        model = make_subsystem(env)
        for i, tenant_id in enumerate(("T0", "T1", "T0", "T1")):
            model.submit(Command(f"c{i}", "READ", port_id="port0", tenant_id=tenant_id, sq_id="SQ0"))
        env.run(until=300)

        self.assertEqual(model.get_metrics()["commands_submitted"], 4)
        self.assertEqual(model.get_metrics()["completion_submitted"], 4)
        self.assertEqual(len(model.completion.completed), 4)
        completed_ids = {cmd.cmd_id for _time, cmd in model.completion.completed}
        self.assertEqual(completed_ids, {"c0", "c1", "c2", "c3"})

    def test_qos_config_ack_completes_at_apply_not_after_the_return_transition(self):
        """qos_configuration_if's ack ties to qos_config_applied -- bound "no
        further than the Completion IP's own configure_tenant already defers
        it" -- not to the bridge's own separate APPLY_QOS_CONFIG -> IDLE
        return. The ack used to wait for that return transition too, the
        same extra-cycle-late shape M2 fixed on the command path but never
        carried over here (round fourteen's M3); this scenario declared
        expected_performance_properties: [qos_config_applied_promptly] but
        nothing here checked it before (round fourteen's M4).
        """
        env = simpy.Environment()
        model = make_subsystem(env)
        accepted = model.configure_qos("T0", 250.0)

        acked_at = None
        while env.peek() < 10:
            env.step()
            if accepted.processed and acked_at is None:
                acked_at = env.now

        self.assertIsNotNone(acked_at, "the ack never fired")
        self.assertEqual(
            acked_at,
            2,
            "must be acked at ACCEPT_QOS_CONFIG(1) + APPLY_QOS_CONFIG's own configure_tenant(1) = 2, "
            "not one glue cycle later after the bridge's own return to IDLE",
        )

    def test_qos_config_flows_to_completion_ip(self):
        """template test_scenarios[1]: qos_config_flows_to_completion_ip.

        A QoS config request submitted to the subsystem is applied to the
        Completion IP's own per-tenant token buckets -- through the same
        intake_bridge FSM as a command, not a separate back door.
        """
        env = simpy.Environment()
        model = make_subsystem(env)
        accepted = model.configure_qos("T0", 250.0)
        env.run(until=10)

        self.assertTrue(accepted.processed)
        self.assertEqual(model.get_metrics()["qos_configs_applied"], 1)
        # CONFIGURE_QOS applies the same budget to all four buckets alike
        # (round thirteen's M3: read_bw/write_bw used to get a 1000x-scaled
        # value instead).
        self.assertEqual(model.completion.tokens["T0"]["read"], 250.0)
        self.assertEqual(model.completion.tokens["T0"]["write"], 250.0)
        self.assertEqual(model.completion.tokens["T0"]["read_bw"], 250.0)
        self.assertEqual(model.completion.tokens["T0"]["write_bw"], 250.0)
        self.assertGreater(model.transition_counts["IDLE->ACCEPT_QOS_CONFIG"], 0)
        self.assertGreater(model.transition_counts["ACCEPT_QOS_CONFIG->APPLY_QOS_CONFIG"], 0)
        self.assertEqual(model.fsm_state["intake_bridge"], "IDLE")

    def test_completion_backlog_throttles_arbitration_issue_readiness(self):
        """template test_scenarios[2]: completion_backlog_throttles_arbitration_issue_readiness.

        When the Completion IP's pending backlog reaches the configured
        limit, the backpressure monitor makes the Arbitration IP
        not-issue-ready; when the backlog drops back below it, issue
        readiness is restored. Both directions are exercised, and both the
        held-steady (SAMPLE_BACKLOG -> SAMPLE_BACKLOG) and changed
        (-> APPLY_THROTTLE) transitions must fire for real.
        """
        env = simpy.Environment()
        model = make_subsystem(env, completion_backlog_limit=2)
        model.completion.set_completion_ready(False)
        for i in range(5):
            model.submit(Command(f"c{i}", "READ", port_id="port0", tenant_id="T0", sq_id="SQ0"))
        env.run(until=120)

        self.assertTrue(model.issue_throttled, "backlog reached the limit but issue readiness was not throttled")
        self.assertFalse(model.arbitration.output_ready)
        self.assertGreaterEqual(model.get_metrics()["backpressure_events"], 1)
        self.assertGreater(model.transition_counts["SAMPLE_BACKLOG->APPLY_THROTTLE"], 0)
        self.assertGreater(model.transition_counts["SAMPLE_BACKLOG->SAMPLE_BACKLOG"], 0)

        model.completion.set_completion_ready(True)
        env.run(until=400)

        self.assertFalse(model.issue_throttled, "backlog drained but issue readiness was never restored")
        self.assertTrue(model.arbitration.output_ready)
        self.assertEqual(len(model.completion.completed), 5)

    def test_no_output_stall_when_backlog_stays_below_the_limit(self):
        """template test_scenarios[2]'s other declared property:
        no_output_stall_below_the_backlog_limit. The scenario test above
        only exercises reaching and draining the limit; this one submits a
        real backlog that never reaches it and checks issue readiness is
        never disturbed at any point during the run, not just checked once
        at the end (round fourteen's M6).
        """
        env = simpy.Environment()
        model = make_subsystem(env, completion_backlog_limit=3)
        for i in range(2):
            model.submit(Command(f"c{i}", "READ", port_id="port0", tenant_id="T0", sq_id="SQ0"))

        throttled_at_any_point = False
        while env.peek() < 120:
            env.step()
            if model.issue_throttled or not model.arbitration.output_ready:
                throttled_at_any_point = True

        self.assertFalse(
            throttled_at_any_point,
            "issue readiness must not be disturbed while the backlog never reaches the configured limit",
        )
        self.assertEqual(len(model.completion.completed), 2)
        self.assertGreater(
            model.transition_counts["SAMPLE_BACKLOG->SAMPLE_BACKLOG"],
            0,
            "the held-steady transition must actually fire while nothing changes",
        )

    def test_metrics_snapshot_is_a_copy(self):
        env = simpy.Environment()
        model = make_subsystem(env)
        snapshot = model.get_metrics()
        snapshot["commands_submitted"] = 999
        self.assertEqual(model.get_metrics().get("commands_submitted", 0), 0)


if __name__ == "__main__":
    unittest.main()
