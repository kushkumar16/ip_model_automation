import unittest

import simpy

from ip_model_automation.ip import Descriptor, GdmaIpModel


class TestGdmaIpModel(unittest.TestCase):
    def test_gdma_completes_descriptor(self):
        env = simpy.Environment()
        model = GdmaIpModel(
            env,
            fetch_latency=1,
            read_latency=2,
            write_latency=3,
            completion_latency=1,
            channel_scan_latency=1,
            irq_latency=1,
        )
        model.enable_channel(0)
        model.submit(Descriptor("d0", channel_id=0))
        env.run(until=10)
        self.assertEqual(model.metrics["completed_descriptors"], 1)
        self.assertEqual(model.metrics["channel_grants"], 1)
        self.assertEqual(model.metrics["read_requests"], 1)
        self.assertEqual(model.metrics["write_responses"], 1)

    def test_gdma_rejects_disabled_channel_descriptor(self):
        env = simpy.Environment()
        model = GdmaIpModel(env, fetch_latency=1, channel_scan_latency=1)
        model.submit(Descriptor("d0", channel_id=1))
        env.run(until=8)
        self.assertEqual(model.completed, [])
        self.assertIn("channel_disabled", model.errors)
        self.assertEqual(model.metrics["errors"], 1)
        self.assertEqual(model.channel_state[1], "ERROR")

    def test_gdma_write_backpressure_stalls_until_destination_ready(self):
        env = simpy.Environment()
        model = GdmaIpModel(
            env,
            fetch_latency=1,
            read_latency=1,
            write_latency=1,
            completion_latency=1,
            channel_scan_latency=1,
            irq_latency=1,
        )
        model.enable_channel(0)
        model.set_memory_ready(destination=False)
        model.submit(Descriptor("d0", channel_id=0))
        env.run(until=8)
        self.assertEqual(model.completed, [])
        self.assertGreater(model.metrics["write_stalls"], 0)
        model.set_memory_ready(destination=True)
        env.run(until=14)
        self.assertEqual(model.completed[0][1].desc_id, "d0")

    def test_gdma_configure_channel_disable_and_halt(self):
        env = simpy.Environment()
        model = GdmaIpModel(env)
        model.configure_channel(0, enabled=True, priority=2)
        self.assertIn(0, model.enabled)
        self.assertEqual(model.channel_priority[0], 2)
        model.configure_channel(0, enabled=False)
        self.assertNotIn(0, model.enabled)
        self.assertEqual(model.channel_state[0], "DISABLED")
        model.enable_channel(1)
        model.halt_channel(1)
        self.assertNotIn(1, model.enabled)
        self.assertEqual(model.channel_state[1], "HALTED")

    def test_gdma_rejects_zero_length_descriptor(self):
        env = simpy.Environment()
        model = GdmaIpModel(env, fetch_latency=1, channel_scan_latency=1)
        model.enable_channel(0)
        model.submit(Descriptor("d0", channel_id=0, length_bytes=0))
        env.run(until=8)
        self.assertEqual(model.completed, [])
        self.assertIn("bad_descriptor", model.errors)
        self.assertEqual(model.channel_state[0], "ERROR")

    def test_gdma_completion_backpressure_stalls_until_ready(self):
        env = simpy.Environment()
        model = GdmaIpModel(
            env,
            fetch_latency=1,
            read_latency=1,
            write_latency=1,
            completion_latency=1,
            channel_scan_latency=1,
            irq_latency=1,
        )
        model.enable_channel(0)
        model.set_memory_ready(completion=False)
        model.submit(Descriptor("d0", channel_id=0))
        env.run(until=10)
        self.assertEqual(model.completed, [])
        self.assertGreater(model.metrics["completion_stalls"], 0)
        model.set_memory_ready(completion=True)
        env.run(until=16)
        self.assertEqual(model.completed[0][1].desc_id, "d0")

    def test_gdma_full_descriptor_queue_counts_fetch_stall(self):
        env = simpy.Environment()
        model = GdmaIpModel(env, fetch_latency=1, channel_scan_latency=100, descriptor_queue_depth=1)
        model.enable_channel(0)
        model.submit(Descriptor("d0", channel_id=0))
        model.submit(Descriptor("d1", channel_id=0))
        model.submit(Descriptor("d2", channel_id=0))
        env.run(until=50)
        self.assertGreaterEqual(model.metrics["descriptor_queue_full_stalls"], 1)

    def test_gdma_full_data_buffer_counts_stall(self):
        env = simpy.Environment()
        model = GdmaIpModel(
            env,
            fetch_latency=1,
            read_latency=1,
            write_latency=1,
            completion_latency=1,
            channel_scan_latency=1,
            buffer_depth=1,
        )
        model.enable_channel(0)
        model.set_memory_ready(destination=False)
        model.submit(Descriptor("d0", channel_id=0))
        model.submit(Descriptor("d1", channel_id=0))
        model.submit(Descriptor("d2", channel_id=0))
        env.run(until=50)
        self.assertGreaterEqual(model.metrics["buffer_full_stalls"], 1)

    def test_gdma_interrupt_coalescing_threshold(self):
        env = simpy.Environment()
        model = GdmaIpModel(
            env,
            fetch_latency=1,
            read_latency=1,
            write_latency=1,
            completion_latency=1,
            channel_scan_latency=1,
            irq_latency=1,
            coalesce_threshold=2,
        )
        model.enable_channel(0)
        model.submit(Descriptor("d0", channel_id=0))
        model.submit(Descriptor("d1", channel_id=0))
        env.run(until=20)
        self.assertEqual(model.metrics["completed_descriptors"], 2)
        self.assertEqual(model.metrics["irq_count"], 1)
        self.assertEqual(model.irqs, [0])

    def test_gdma_next_coalescing_window_waits_for_irq_clear(self):
        """interrupt_if: wait_for_ack_before_next_request, outstanding_limit 1."""
        env = simpy.Environment()
        model = GdmaIpModel(
            env,
            fetch_latency=1,
            read_latency=1,
            write_latency=1,
            completion_latency=1,
            channel_scan_latency=1,
            irq_latency=1,
            coalesce_threshold=1,
        )
        model.enable_channel(0)
        for index in range(3):
            model.submit(Descriptor(f"d{index}", channel_id=0))
        env.run(until=40)

        # The data path drains all three descriptors, but only the first IRQ is
        # asserted: coalescing is parked until software clears it.
        self.assertEqual(model.metrics["completed_descriptors"], 3)
        self.assertEqual(model.metrics["irq_count"], 1)
        self.assertEqual(model.fsm_state["interrupt_coalescing"], "WAIT_CLEAR")

        model.clear_interrupt()
        env.run(until=60)
        self.assertEqual(model.metrics["irq_count"], 2)


if __name__ == "__main__":
    unittest.main()
