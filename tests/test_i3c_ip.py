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

    def test_empty_fifo_reads_return_none(self):
        env = simpy.Environment()
        model = I3cIpModel(env)
        self.assertIsNone(model.read_rx())
        self.assertIsNone(model.read_ibi())

    def test_get_metrics_returns_snapshot_dict(self):
        env = simpy.Environment()
        model = I3cIpModel(env)
        model.raise_ibi_request()
        snapshot = model.get_metrics()
        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["ibi_requests"], 1)

    def test_full_command_queue_drops_command(self):
        env = simpy.Environment()
        model = I3cIpModel(env, command_depth=2, byte_bits=2, shift_latency=1)
        model.queue_command("cmd0", "READ", byte_count=1)
        model.queue_command("cmd1", "READ", byte_count=1)
        model.queue_command("cmd2", "READ", byte_count=1)
        env.run(until=1)
        self.assertEqual(model.metrics["commands_issued"], 2)
        self.assertEqual(model.metrics["command_queue_dropped"], 1)

    def test_full_tx_fifo_drops_byte(self):
        env = simpy.Environment()
        model = I3cIpModel(env, tx_depth=1)
        model.write_tx(0x11)
        model.write_tx(0x22)
        env.run(until=1)
        self.assertEqual(len(model.tx_fifo), 1)
        self.assertEqual(model.metrics["tx_dropped"], 1)

    def test_clear_interrupt_empties_asserted_interrupts(self):
        env = simpy.Environment()
        model = I3cIpModel(env, byte_bits=2, shift_latency=1, interrupt_latency=1)
        model.queue_command("cmd0", "WRITE", byte_count=1)
        model.write_tx(0xAB)
        env.run(until=30)
        self.assertGreaterEqual(model.metrics["interrupt_count"], 1)
        model.clear_interrupt()
        env.run(until=35)
        self.assertEqual(model.interrupts, [])

    def test_rx_overflow_is_counted_when_fifo_full(self):
        env = simpy.Environment()
        model = I3cIpModel(env, rx_depth=1, byte_bits=2, shift_latency=1, interrupt_latency=1)
        model.queue_command("cmd0", "READ", byte_count=3)
        env.run(until=40)
        self.assertEqual(model.metrics["received_bytes"], 1)
        self.assertEqual(model.metrics["rx_overflows"], 2)

    def test_ibi_overflow_is_counted_when_fifo_full(self):
        env = simpy.Environment()
        model = I3cIpModel(env, ibi_depth=1, ibi_detect_latency=1, interrupt_latency=1)
        model.raise_ibi_request()
        model.raise_ibi_request()
        env.run(until=20)
        self.assertEqual(model.metrics["ibi_bytes_captured"], 1)
        self.assertEqual(model.metrics["ibi_overflows"], 1)

    def test_masked_interrupt_suppresses_irq(self):
        env = simpy.Environment()
        model = I3cIpModel(env, byte_bits=2, shift_latency=1, interrupt_latency=1)
        model.configure(masked=True)
        model.queue_command("cmd0", "WRITE", byte_count=1)
        model.write_tx(0x55)
        env.run(until=30)
        self.assertGreaterEqual(model.metrics["masked_interrupts"], 1)
        self.assertEqual(model.metrics["interrupt_count"], 0)

    def test_interrupt_holds_until_cleared_then_asserts_again(self):
        """interrupt_if: wait_for_ack_before_next_request, outstanding_limit 1."""
        env = simpy.Environment()
        model = I3cIpModel(env, byte_bits=2, shift_latency=1, interrupt_latency=1)
        for index in range(2):
            model.queue_command(f"cmd{index}", "WRITE", byte_count=1)
            model.write_tx(0x10 + index)
        env.run(until=60)

        # Transfers keep running; the second transfer-done waits for the clear.
        self.assertGreaterEqual(model.metrics["transfers_completed"], 2)
        self.assertEqual(model.metrics["interrupt_count"], 1)
        self.assertEqual(model.fsm_state["interrupt_control"], "WAIT_SW_CLEAR")

        model.clear_interrupt()
        env.run(until=90)
        self.assertEqual(model.metrics["interrupt_count"], 2)


if __name__ == "__main__":
    unittest.main()
