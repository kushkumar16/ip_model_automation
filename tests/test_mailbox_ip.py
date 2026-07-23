import unittest

import simpy

from ip_model_automation.ip import MailboxIpModel


class TestMailboxIpModel(unittest.TestCase):
    def test_message_enqueue_delivers_and_interrupts(self):
        env = simpy.Environment()
        model = MailboxIpModel(env, interrupt_latency=1)
        model.configure_channel(0)
        model.send_message(0, "m0")
        env.run(until=10)
        self.assertGreaterEqual(model.metrics["enqueued_messages"], 1)
        self.assertGreaterEqual(model.metrics["delivered_messages"], 1)
        self.assertGreaterEqual(model.metrics["interrupt_count"], 1)
        self.assertEqual(model.read_message(0), "m0")

    def test_full_fifo_applies_backpressure(self):
        env = simpy.Environment()
        model = MailboxIpModel(env, fifo_depth=4)
        model.set_receiver_enabled(False)
        model.configure_channel(0)
        for index in range(6):
            model.send_message(0, f"m{index}")
        env.run(until=20)
        self.assertEqual(model.metrics["enqueued_messages"], 4)
        self.assertEqual(model.metrics["dropped_on_full"], 2)

    def test_interrupt_waits_for_software_clear_before_next_assertion(self):
        """interrupt_if: wait_for_ack_before_next_request, outstanding_limit 1."""
        env = simpy.Environment()
        model = MailboxIpModel(env, interrupt_latency=1)
        model.configure_channel(0)
        model.configure_channel(1)
        model.send_message(0, "m0")
        model.send_message(1, "m1")
        env.run(until=10)

        # The message path ran to completion, but only one IRQ is asserted:
        # the notify process is parked waiting for the software clear.
        self.assertGreaterEqual(model.metrics["enqueued_messages"], 2)
        self.assertEqual(model.metrics["interrupt_count"], 1)
        self.assertEqual(model.fsm_state["interrupt_notify"], "WAIT_SW_CLEAR")

        model.clear_interrupt(model.interrupts[0][1])
        env.run(until=20)
        self.assertEqual(model.metrics["interrupt_count"], 2)
        self.assertEqual(model.metrics["irq_clear_waits"], 2)

    def test_status_read_returns_a_response_to_the_caller(self):
        """register_if: wait_for_response — the caller blocks on the access."""
        env = simpy.Environment()
        model = MailboxIpModel(env, register_latency=2)
        model.configure_channel(0)
        model.set_receiver_enabled(False)
        model.send_message(0, "m0")
        seen = {}

        def reader():
            yield env.timeout(5)
            started = env.now
            seen["status"] = yield model.read_status(0)
            seen["elapsed"] = env.now - started

        env.process(reader())
        env.run(until=20)
        self.assertEqual(seen["status"]["occupancy"], 1)
        self.assertTrue(seen["status"]["doorbell_pending"])
        self.assertGreaterEqual(seen["elapsed"], 2)  # the read costs a register access

    def test_masked_doorbell_suppresses_interrupt(self):
        env = simpy.Environment()
        model = MailboxIpModel(env, interrupt_latency=1)
        model.configure_channel(0, masked=True)
        model.send_message(0, "m0")
        env.run(until=10)
        self.assertGreaterEqual(model.metrics["masked_doorbells"], 1)
        self.assertEqual(model.metrics["interrupt_count"], 0)
        self.assertGreaterEqual(model.metrics["delivered_messages"], 1)


if __name__ == "__main__":
    unittest.main()
