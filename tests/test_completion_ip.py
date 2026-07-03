import unittest

import simpy

from ip_model_automation.ip import Command, CompletionIpModel


class TestCompletionIpModel(unittest.TestCase):
    def test_completion_emits_read(self):
        env = simpy.Environment()
        model = CompletionIpModel(env, service_latency=2, tenant_select_latency=1, token_check_latency=1, emit_latency=1)
        model.configure_tenant("T0", read=2, write=2, read_bw=20, write_bw=20)
        model.submit(Command("r0", "READ", tenant_id="T0", size_kb=4))
        env.run(until=8)
        self.assertEqual(model.completed[0][1].cmd_id, "r0")

    def test_completion_wwv_write_debits_read_and_write_budget(self):
        env = simpy.Environment()
        model = CompletionIpModel(env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=1, wwv_write=3, wwv_read=2)
        model.configure_tenant("T0", read=2, write=1, read_bw=8, write_bw=4)
        model.submit(Command("w0", "WRITE", tenant_id="T0", size_kb=4))
        env.run(until=10)
        self.assertEqual(model.completed[0][1].cmd_id, "w0")
        self.assertEqual(model.tokens["T0"]["write"], 0.0)
        self.assertEqual(model.tokens["T0"]["read"], 0.5)
        self.assertEqual(model.tokens["T0"]["write_bw"], 0.0)
        self.assertEqual(model.tokens["T0"]["read_bw"], 2.0)

    def test_completion_hard_limit_blocks_burst_tokens(self):
        env = simpy.Environment()
        model = CompletionIpModel(env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=1)
        model.configure_tenant("T0", read=0, write=1, read_bw=0, write_bw=4, limit_type="HARD", burst_read=10, burst_read_bw=40)
        model.submit(Command("w0", "WRITE", tenant_id="T0", size_kb=4))
        env.run(until=12)
        self.assertEqual(model.completed, [])
        self.assertGreater(model.metrics["token_stalls"], 0)

    def test_completion_soft_limit_uses_burst_tokens(self):
        env = simpy.Environment()
        model = CompletionIpModel(env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=1)
        model.configure_tenant("T0", read=0, write=1, read_bw=0, write_bw=4, limit_type="SOFT", burst_read=10, burst_read_bw=40)
        model.submit(Command("w0", "WRITE", tenant_id="T0", size_kb=4))
        env.run(until=10)
        self.assertEqual(model.completed[0][1].cmd_id, "w0")
        self.assertEqual(model.burst_tokens["T0"]["read"], 8.5)
        self.assertEqual(model.burst_tokens["T0"]["read_bw"], 34.0)
        self.assertGreater(model.metrics["burst_debits"], 0)

    def test_completion_output_backpressure_holds_ready_command(self):
        env = simpy.Environment()
        model = CompletionIpModel(env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=1)
        model.configure_tenant("T0", read=1, write=1, read_bw=4, write_bw=4)
        model.set_completion_ready(False)
        model.submit(Command("r0", "READ", tenant_id="T0", size_kb=4))
        env.run(until=8)
        self.assertEqual(model.completed, [])
        self.assertEqual(len(model.pending["T0"]), 1)
        self.assertGreater(model.metrics["output_stalls"], 0)
        model.set_completion_ready(True)
        env.run(until=14)
        self.assertEqual(model.completed[0][1].cmd_id, "r0")


if __name__ == "__main__":
    unittest.main()
