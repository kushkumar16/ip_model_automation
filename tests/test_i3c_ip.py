import unittest

import simpy

from ip_model_automation.ip import I3cIpModel


class TestI3cIpModel(unittest.TestCase):
    def test_private_write_completes_and_interrupts(self):
        env = simpy.Environment()
        model = I3cIpModel(env, byte_bits=2, shift_latency=1, interrupt_latency=1)
        model.configure()
        model.queue_command("cmd0", "WRITE", byte_count=1)
        model.write_tx(0xAB)
        env.run(until=30)
        self.assertGreaterEqual(model.metrics["transmitted_bytes"], 1)
        self.assertGreaterEqual(model.metrics["transfers_completed"], 1)
        self.assertGreaterEqual(model.metrics["interrupt_count"], 1)

    def test_private_read_captures_bytes(self):
        env = simpy.Environment()
        model = I3cIpModel(env, byte_bits=2, shift_latency=1, interrupt_latency=1)
        model.configure()
        model.queue_command("cmd0", "READ", byte_count=3)
        env.run(until=40)
        self.assertEqual(model.metrics["received_bytes"], 3)
        self.assertEqual(model.metrics["transfers_completed"], 1)
        self.assertIsNotNone(model.read_rx())

    def test_ibi_request_is_accepted_and_captured(self):
        env = simpy.Environment()
        model = I3cIpModel(env, ibi_detect_latency=1, interrupt_latency=1)
        model.raise_ibi_request()
        env.run(until=20)
        self.assertEqual(model.metrics["ibi_requests"], 1)
        self.assertEqual(model.metrics["ibi_accepted"], 1)
        self.assertEqual(model.metrics["ibi_bytes_captured"], 1)
        self.assertGreaterEqual(model.metrics["interrupt_count"], 1)
        self.assertIsNotNone(model.read_ibi())

    def test_masked_interrupt_suppresses_irq(self):
        env = simpy.Environment()
        model = I3cIpModel(env, byte_bits=2, shift_latency=1, interrupt_latency=1)
        model.configure(masked=True)
        model.queue_command("cmd0", "WRITE", byte_count=1)
        model.write_tx(0x55)
        env.run(until=30)
        self.assertGreaterEqual(model.metrics["masked_interrupts"], 1)
        self.assertEqual(model.metrics["interrupt_count"], 0)


if __name__ == "__main__":
    unittest.main()
