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
