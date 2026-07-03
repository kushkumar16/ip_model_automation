import unittest

import simpy

from ip_model_automation.ip import SpiMasterIpModel


class TestSpiMasterIpModel(unittest.TestCase):
    def test_single_byte_transfer_completes_and_interrupts(self):
        env = simpy.Environment()
        model = SpiMasterIpModel(env, byte_bits=2, shift_latency=1, interrupt_latency=1)
        model.configure()
        model.write_tx(0xAB)
        env.run(until=30)
        self.assertGreaterEqual(model.metrics["transmitted_bytes"], 1)
        self.assertGreaterEqual(model.metrics["received_bytes"], 1)
        self.assertGreaterEqual(model.metrics["transfers_completed"], 1)
        self.assertGreaterEqual(model.metrics["interrupt_count"], 1)
        self.assertEqual(model.read_rx(), 0xAB)

    def test_rx_overflow_is_counted(self):
        env = simpy.Environment()
        model = SpiMasterIpModel(env, rx_depth=4, byte_bits=2, shift_latency=1)
        model.configure()
        for byte in range(6):
            model.write_tx(byte)
        env.run(until=100)
        self.assertEqual(model.metrics["transmitted_bytes"], 6)
        self.assertEqual(model.metrics["received_bytes"], 4)
        self.assertEqual(model.metrics["rx_overflows"], 2)

    def test_masked_interrupt_suppresses_irq(self):
        env = simpy.Environment()
        model = SpiMasterIpModel(env, byte_bits=2, shift_latency=1, interrupt_latency=1)
        model.configure(masked=True)
        model.write_tx(0x55)
        env.run(until=30)
        self.assertGreaterEqual(model.metrics["masked_interrupts"], 1)
        self.assertEqual(model.metrics["interrupt_count"], 0)


if __name__ == "__main__":
    unittest.main()
