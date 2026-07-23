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

    def test_empty_rx_fifo_read_returns_none(self):
        env = simpy.Environment()
        model = SpiMasterIpModel(env)
        self.assertIsNone(model.read_rx())

    def test_get_metrics_returns_snapshot_dict(self):
        env = simpy.Environment()
        model = SpiMasterIpModel(env, byte_bits=2, shift_latency=1)
        model.write_tx(0x0F)
        env.run(until=20)
        snapshot = model.get_metrics()
        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["transmitted_bytes"], 1)

    def test_full_tx_fifo_drops_byte(self):
        env = simpy.Environment()
        model = SpiMasterIpModel(env, tx_depth=1)
        model.write_tx(0x11)
        model.write_tx(0x22)
        env.run(until=1)
        self.assertEqual(model.metrics["tx_dropped"], 1)

    def test_clear_interrupt_empties_asserted_interrupts(self):
        env = simpy.Environment()
        model = SpiMasterIpModel(env, byte_bits=2, shift_latency=1, interrupt_latency=1)
        model.write_tx(0xAB)
        env.run(until=20)
        self.assertGreaterEqual(model.metrics["interrupt_count"], 1)
        model.clear_interrupt()
        env.run(until=25)
        self.assertEqual(model.interrupts, [])

    def test_masked_interrupt_suppresses_irq(self):
        env = simpy.Environment()
        model = SpiMasterIpModel(env, byte_bits=2, shift_latency=1, interrupt_latency=1)
        model.configure(masked=True)
        model.write_tx(0x55)
        env.run(until=30)
        self.assertGreaterEqual(model.metrics["masked_interrupts"], 1)
        self.assertEqual(model.metrics["interrupt_count"], 0)

    def test_interrupt_holds_until_cleared_then_asserts_again(self):
        """interrupt_if: wait_for_ack_before_next_request, outstanding_limit 1."""
        env = simpy.Environment()
        model = SpiMasterIpModel(env, byte_bits=2, shift_latency=1, interrupt_latency=1)
        model.write_tx(0xAA)
        model.write_tx(0xBB)
        env.run(until=40)

        # Both bytes shift out — the transfer pipeline does not block — but the
        # second transfer-done cannot raise an IRQ while the first is pending.
        self.assertGreaterEqual(model.metrics["transfers_completed"], 2)
        self.assertEqual(model.metrics["interrupt_count"], 1)
        self.assertEqual(model.fsm_state["interrupt_control"], "WAIT_SW_CLEAR")

        model.clear_interrupt()
        env.run(until=60)
        self.assertEqual(model.metrics["interrupt_count"], 2)


if __name__ == "__main__":
    unittest.main()
