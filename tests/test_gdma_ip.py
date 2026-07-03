import unittest

import simpy

from ip_model_automation.ip import Descriptor, GdmaIpModel


class TestGdmaIpModel(unittest.TestCase):
    def test_gdma_completes_descriptor(self):
        env = simpy.Environment()
        model = GdmaIpModel(env, fetch_latency=1, read_latency=2, write_latency=3, completion_latency=1, channel_scan_latency=1, irq_latency=1)
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
        model = GdmaIpModel(env, fetch_latency=1, read_latency=1, write_latency=1, completion_latency=1, channel_scan_latency=1, irq_latency=1)
        model.enable_channel(0)
        model.set_memory_ready(destination=False)
        model.submit(Descriptor("d0", channel_id=0))
        env.run(until=8)
        self.assertEqual(model.completed, [])
        self.assertGreater(model.metrics["write_stalls"], 0)
        model.set_memory_ready(destination=True)
        env.run(until=14)
        self.assertEqual(model.completed[0][1].desc_id, "d0")

    def test_gdma_interrupt_coalescing_threshold(self):
        env = simpy.Environment()
        model = GdmaIpModel(env, fetch_latency=1, read_latency=1, write_latency=1, completion_latency=1, channel_scan_latency=1, irq_latency=1, coalesce_threshold=2)
        model.enable_channel(0)
        model.submit(Descriptor("d0", channel_id=0))
        model.submit(Descriptor("d1", channel_id=0))
        env.run(until=20)
        self.assertEqual(model.metrics["completed_descriptors"], 2)
        self.assertEqual(model.metrics["irq_count"], 1)
        self.assertEqual(model.irqs, [0])


if __name__ == "__main__":
    unittest.main()
