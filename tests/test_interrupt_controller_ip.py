import unittest

import simpy

from ip_model_automation.ip import InterruptControllerIpModel


class TestInterruptControllerIpModel(unittest.TestCase):
    def test_interrupt_delivery(self):
        env = simpy.Environment()
        model = InterruptControllerIpModel(env, sample_latency=1, pending_latency=1, priority_latency=1, delivery_latency=1)
        model.configure_source(3, priority=0)
        model.assert_edge(3)
        env.run(until=8)
        self.assertEqual(model.metrics["delivered_irqs"], 1)

    def test_interrupt_priority_mask_ack_and_eoi(self):
        env = simpy.Environment()
        model = InterruptControllerIpModel(env, sample_latency=1, pending_latency=1, filter_latency=1, priority_latency=1, delivery_latency=1, ack_latency=1, eoi_latency=1)
        model.configure_source(1, priority=5)
        model.configure_source(2, priority=0)
        model.assert_edge(1)
        model.assert_edge(2)
        env.run(until=10)
        self.assertEqual(model.delivered[0][1], 2)
        model.ack()
        env.run(until=13)
        self.assertIn(2, model.active)
        self.assertEqual(model.metrics["ack_count"], 1)
        model.eoi(2)
        env.run(until=16)
        self.assertNotIn(2, model.active)
        self.assertEqual(model.metrics["eoi_count"], 1)

    def test_interrupt_mask_blocks_delivery_until_unmasked(self):
        env = simpy.Environment()
        model = InterruptControllerIpModel(env, sample_latency=1, pending_latency=1, filter_latency=1, priority_latency=1, delivery_latency=1)
        model.configure_source(4, priority=0, masked=True)
        model.assert_edge(4)
        env.run(until=8)
        self.assertEqual(model.delivered, [])
        self.assertGreater(model.metrics["masked_irqs"], 0)
        model.set_mask(4, False)
        env.run(until=14)
        self.assertEqual(model.delivered[0][1], 4)

    def test_interrupt_level_reasserts_after_eoi(self):
        env = simpy.Environment()
        model = InterruptControllerIpModel(env, sample_latency=1, pending_latency=1, filter_latency=1, priority_latency=1, delivery_latency=1, ack_latency=1, eoi_latency=1)
        model.configure_source(5, priority=0, trigger_type="LEVEL")
        model.set_level(5, True)
        env.run(until=8)
        model.ack()
        env.run(until=11)
        model.eoi(5)
        env.run(until=18)
        self.assertGreaterEqual(model.metrics["reasserted_level_irqs"], 1)
        self.assertGreaterEqual(model.metrics["delivered_irqs"], 2)

    def test_interrupt_disabled_source_event_is_dropped(self):
        env = simpy.Environment()
        model = InterruptControllerIpModel(env, sample_latency=1, pending_latency=1, filter_latency=1, priority_latency=1, delivery_latency=1)
        model.configure_source(1, priority=1, enabled=False)
        self.assertNotIn(1, model.enabled)
        model.assert_edge(1)
        env.run(until=10)
        self.assertGreaterEqual(model.metrics["disabled_source_events"], 1)
        self.assertNotIn(1, model.pending)

    def test_interrupt_functional_edge_and_ack(self):
        env = simpy.Environment()
        model = InterruptControllerIpModel(env)
        model.configure_source(2, priority=1)
        model.configure_source(3, priority=0)
        model._assert_edge_functional(2)
        model._assert_edge_functional(3)
        model._assert_edge_functional(9)
        self.assertEqual(model.pending, {2, 3})
        selected = model.ack_functional("CPU0")
        self.assertEqual(selected, 3)
        self.assertIn(3, model.active)
        self.assertEqual(model.last_delivered["CPU0"], 3)
        self.assertEqual(model.ack_functional("CPU0"), 2)
        self.assertIsNone(model.ack_functional("CPU0"))

    def test_interrupt_software_interrupt_delivery(self):
        env = simpy.Environment()
        model = InterruptControllerIpModel(env, pending_latency=1, filter_latency=1, priority_latency=1, delivery_latency=1, software_latency=1)
        model.inject_software_interrupt(7)
        env.run(until=8)
        self.assertEqual(model.delivered[0][1], 7)
        self.assertEqual(model.metrics["software_interrupts"], 1)


if __name__ == "__main__":
    unittest.main()
