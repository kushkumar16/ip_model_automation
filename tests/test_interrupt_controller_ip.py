import unittest

import simpy

from ip_model_automation.ip import InterruptControllerIpModel


class TestInterruptControllerIpModel(unittest.TestCase):
    def test_interrupt_delivery(self):
        env = simpy.Environment()
        model = InterruptControllerIpModel(
            env, sample_latency=1, pending_latency=1, priority_latency=1, delivery_latency=1
        )
        model.configure_source(3, priority=0)
        model.assert_edge(3)
        env.run(until=8)
        self.assertEqual(model.metrics["delivered_irqs"], 1)

    def test_interrupt_priority_mask_ack_and_eoi(self):
        env = simpy.Environment()
        model = InterruptControllerIpModel(
            env,
            sample_latency=1,
            pending_latency=1,
            filter_latency=1,
            priority_latency=1,
            delivery_latency=1,
            ack_latency=1,
            eoi_latency=1,
        )
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
        model = InterruptControllerIpModel(
            env, sample_latency=1, pending_latency=1, filter_latency=1, priority_latency=1, delivery_latency=1
        )
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
        model = InterruptControllerIpModel(
            env,
            sample_latency=1,
            pending_latency=1,
            filter_latency=1,
            priority_latency=1,
            delivery_latency=1,
            ack_latency=1,
            eoi_latency=1,
        )
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
        model = InterruptControllerIpModel(
            env, sample_latency=1, pending_latency=1, filter_latency=1, priority_latency=1, delivery_latency=1
        )
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

    def test_register_read_returns_controller_state(self):
        """register_if: wait_for_response — software blocks on the read."""
        env = simpy.Environment()
        model = InterruptControllerIpModel(
            env, pending_latency=1, filter_latency=1, priority_latency=1, delivery_latency=1, register_latency=2
        )
        model.configure_source(4, priority=1)
        model.assert_edge(4)
        seen = {}

        def reader():
            yield env.timeout(10)
            started = env.now
            seen["state"] = yield model.read_state()
            seen["elapsed"] = env.now - started

        env.process(reader())
        env.run(until=30)
        self.assertIn(4, seen["state"]["enabled"])
        self.assertGreaterEqual(seen["elapsed"], 2)

    def test_delivery_waits_for_cpu_ack_before_next_interrupt(self):
        """cpu_irq_if: wait_for_ack_before_next_request, outstanding_limit 1 per target."""
        env = simpy.Environment()
        model = InterruptControllerIpModel(
            env, pending_latency=1, filter_latency=1, priority_latency=1, delivery_latency=1, ack_latency=1
        )
        model.configure_source(1, priority=1)
        model.configure_source(2, priority=2)
        model.assert_edge(1)
        model.assert_edge(2)
        env.run(until=20)

        # The higher-priority source is delivered and the level is held: the
        # second interrupt is not delivered until the CPU acknowledges.
        self.assertEqual(len(model.delivered), 1)
        self.assertEqual(model.delivered[0][1], 1)
        self.assertEqual(model.fsm_state["cpu_delivery"], "WAIT_ACK")

        model.ack("CPU0")
        env.run(until=40)
        self.assertEqual(len(model.delivered), 2)
        self.assertEqual(model.delivered[1][1], 2)

    def test_software_interrupt_after_a_source_event_is_not_swallowed(self):
        """A hardware event must not cost the next software interrupt.

        pending_update waits on two stores at once. SimPy's `a | b` leaves the
        losing branch's Get sitting in its store's queue, and the next put to
        that store is handed to the stale request -- which nobody is waiting on
        any more, so the item disappears. The source event here makes the
        software Get the loser; the software interrupt that follows is the one
        that used to vanish.
        """
        env = simpy.Environment()
        model = InterruptControllerIpModel(
            env,
            sample_latency=1,
            pending_latency=1,
            filter_latency=1,
            priority_latency=1,
            delivery_latency=1,
            software_latency=1,
        )
        model.configure_source(3, priority=0)
        model.configure_source(7, priority=0)

        model.assert_edge(3)
        env.run(until=10)
        self.assertIn(3, model.pending, "the hardware event itself was lost")

        model.inject_software_interrupt(7)
        env.run(until=25)
        # Accepted by the software_interrupt process either way -- the question
        # is whether pending_update ever saw it. Delivery is not asserted here:
        # the CPU has not acknowledged source 3, and holding the next interrupt
        # until it does is modelled behaviour with its own test.
        self.assertEqual(model.metrics["software_interrupts"], 1)
        self.assertIn(7, model.pending, "the software interrupt was accepted and then swallowed")
        self.assertEqual(model.metrics["pending_updates"], 2)

    def test_simultaneous_source_and_software_events_are_both_kept(self):
        """Both branches firing in the same instant must yield two pendings.

        `AnyOf` returns every event that triggered, and reading one value off it
        discards the rest. Two interrupts arriving in the same cycle is the
        normal case for a controller with two ingress paths, not an edge case.
        """
        env = simpy.Environment()
        model = InterruptControllerIpModel(
            env,
            sample_latency=1,
            pending_latency=1,
            filter_latency=1,
            priority_latency=1,
            delivery_latency=1,
            software_latency=1,
        )
        model.configure_source(3, priority=0)
        model.configure_source(7, priority=1)

        model.assert_edge(3)
        model.inject_software_interrupt(7)
        env.run(until=40)
        self.assertIn(3, model.pending)
        self.assertIn(7, model.pending, "the event that arrived alongside another was dropped")
        self.assertEqual(model.metrics["pending_updates"], 2)

    def test_interrupt_software_interrupt_delivery(self):
        env = simpy.Environment()
        model = InterruptControllerIpModel(
            env, pending_latency=1, filter_latency=1, priority_latency=1, delivery_latency=1, software_latency=1
        )
        model.inject_software_interrupt(7)
        env.run(until=8)
        self.assertEqual(model.delivered[0][1], 7)
        self.assertEqual(model.metrics["software_interrupts"], 1)


if __name__ == "__main__":
    unittest.main()
