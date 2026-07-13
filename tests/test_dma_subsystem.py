import unittest

import simpy

from ip_model_automation.common import Descriptor
from ip_model_automation.ip import DmaSubsystemModel

FAST_GDMA = {
    "fetch_latency": 2,
    "read_latency": 2,
    "write_latency": 2,
    "completion_latency": 2,
    "channel_scan_latency": 1,
    "irq_latency": 1,
}

FAST_INTERCONNECT = {
    "decode_latency": 1,
    "arbitration_latency": 1,
    "slave_latency": 1,
    "response_latency": 1,
    "write_join_latency": 1,
    "error_latency": 1,
}

FAST_ARBITRATION = {
    "bitmap_latency": 1,
    "port_scan_latency": 1,
    "tenant_scan_latency": 1,
    "sq_scan_latency": 1,
    "grant_latency": 1,
    "selection_accept_latency": 1,
    "pending_count_latency": 1,
    "burst_read_latency": 1,
    "burst_calc_latency": 1,
    "burst_debit_latency": 1,
    "issue_latency": 1,
}

FAST_COMPLETION = {
    "service_latency": 1,
    "tenant_select_latency": 1,
    "token_check_latency": 1,
    "emit_latency": 1,
    "retry_latency": 1,
}


def make_subsystem(env, **overrides):
    kwargs = {
        "gdma_kwargs": dict(FAST_GDMA),
        "interconnect_kwargs": dict(FAST_INTERCONNECT),
        "arbitration_kwargs": dict(FAST_ARBITRATION),
        "completion_kwargs": dict(FAST_COMPLETION),
    }
    for key, value in overrides.items():
        if key.endswith("_kwargs"):
            kwargs[key].update(value)
        else:
            kwargs[key] = value
    return DmaSubsystemModel(env, **kwargs)


class TestDmaSubsystemModel(unittest.TestCase):
    def test_single_descriptor_end_to_end(self):
        env = simpy.Environment()
        subsystem = make_subsystem(env)
        subsystem.configure_channel(0)
        subsystem.submit(Descriptor("d0", channel_id=0, src_addr=0x1000, dst_addr=0x2000, length_bytes=4096))
        env.run(until=400)
        self.assertEqual(subsystem.metrics["descriptors_submitted"], 1)
        self.assertEqual(subsystem.metrics["fabric_commands_issued"], 2)
        self.assertEqual(subsystem.metrics["arb_enqueued"], 2)
        self.assertEqual(subsystem.metrics["completions_collected"], 2)
        self.assertEqual(subsystem.metrics["descriptors_completed"], 1)
        self.assertEqual(subsystem.metrics["subsystem_irqs"], 1)
        self.assertGreater(subsystem.metrics["end_to_end_latency"], 0)
        self.assertEqual(subsystem.irqs, [0])

    def test_fabric_backpressure_throttles_engine(self):
        env = simpy.Environment()
        subsystem = make_subsystem(
            env,
            interconnect_kwargs={"outstanding_limit": 1, "slave_latency": 20, "response_latency": 20},
        )
        subsystem.configure_channel(0)
        for index in range(4):
            subsystem.submit(
                Descriptor(
                    f"d{index}",
                    channel_id=0,
                    src_addr=0x1000 * (index + 1),
                    dst_addr=0x2000 * (index + 1),
                    length_bytes=4096,
                )
            )
        env.run(until=3000)
        self.assertGreaterEqual(subsystem.metrics["fabric_backpressure_events"], 1)
        self.assertEqual(subsystem.metrics["descriptors_completed"], 4)
        self.assertEqual(subsystem.metrics["subsystem_irqs"], 4)

    def test_qos_tokens_gate_completions(self):
        env = simpy.Environment()
        subsystem = make_subsystem(env)
        subsystem.configure_channel(0)
        subsystem.configure_qos("T0", token_budget=0.0)
        subsystem.submit(Descriptor("d0", channel_id=0, src_addr=0x1000, dst_addr=0x2000, length_bytes=4096))
        env.run(until=500)
        self.assertGreaterEqual(subsystem.completion.metrics["token_stalls"], 1)
        self.assertEqual(subsystem.metrics["descriptors_completed"], 0)

        subsystem.configure_qos("T0", token_budget=1_000_000.0)
        env.run(until=1000)
        self.assertEqual(subsystem.metrics["descriptors_completed"], 1)
        self.assertEqual(subsystem.metrics["subsystem_irqs"], 1)

    def test_qos_refill_and_metrics_wrappers(self):
        env = simpy.Environment()
        subsystem = make_subsystem(env)
        subsystem.refill_qos()
        self.assertEqual(subsystem.completion.metrics["refill_windows"], 1)
        snapshot = subsystem.get_metrics()
        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot.get("descriptors_submitted", 0), 0)

    def test_unrouted_fabric_command_counts_decode_error(self):
        env = simpy.Environment()
        subsystem = make_subsystem(env)
        subsystem.interconnect.routes.clear()
        subsystem.set_route(0x1000, 0x2000, "mem0")
        subsystem.configure_channel(0)
        subsystem.submit(Descriptor("d0", channel_id=0, src_addr=0x9000, dst_addr=0xA000, length_bytes=4096))
        env.run(until=400)
        self.assertGreaterEqual(subsystem.metrics["fabric_decode_errors"], 1)
        self.assertEqual(subsystem.metrics["descriptors_completed"], 0)

    def test_untracked_member_activity_is_ignored(self):
        env = simpy.Environment()
        subsystem = make_subsystem(env)
        from ip_model_automation.common import Command

        subsystem.interconnect.responses.append((0.0, Command("ghost:rd", "READ", status="mem0:OK")))
        subsystem.completion.completed.append((0.0, Command("ghost:wr", "WRITE")))
        subsystem.gdma.completed_ids.append("ghost")
        env.run(until=20)
        self.assertEqual(subsystem.metrics["arb_enqueued"], 0)
        self.assertEqual(subsystem.metrics["completions_collected"], 0)
        self.assertEqual(subsystem.metrics["descriptors_completed"], 0)

    def test_completion_backlog_throttles_arbitration_issue(self):
        env = simpy.Environment()
        subsystem = make_subsystem(env, completion_backlog_limit=1)
        subsystem.configure_channel(0)
        subsystem.configure_qos("T0", token_budget=0.0)
        subsystem.submit(Descriptor("d0", channel_id=0, src_addr=0x1000, dst_addr=0x2000, length_bytes=4096))
        env.run(until=500)
        self.assertTrue(subsystem.issue_throttled)
        self.assertGreaterEqual(subsystem.metrics["completion_backlog_events"], 1)
        subsystem.configure_qos("T0", token_budget=1_000_000.0)
        env.run(until=1500)
        self.assertEqual(subsystem.metrics["descriptors_completed"], 1)
        self.assertFalse(subsystem.issue_throttled)

    def test_multi_channel_descriptors_complete_independently(self):
        env = simpy.Environment()
        subsystem = make_subsystem(env)
        subsystem.configure_channel(0)
        subsystem.configure_channel(1)
        subsystem.submit(Descriptor("d0", channel_id=0, src_addr=0x1000, dst_addr=0x2000, length_bytes=4096))
        subsystem.submit(Descriptor("d1", channel_id=1, src_addr=0x3000, dst_addr=0x4000, length_bytes=8192))
        env.run(until=800)
        self.assertEqual(subsystem.metrics["descriptors_completed"], 2)
        self.assertEqual(subsystem.metrics["subsystem_irqs"], 2)
        self.assertCountEqual(subsystem.irqs, [0, 1])
        completed_tenants = {cmd.tenant_id for _t, cmd in subsystem.completion.completed}
        self.assertEqual(completed_tenants, {"T0", "T1"})


if __name__ == "__main__":
    unittest.main()
