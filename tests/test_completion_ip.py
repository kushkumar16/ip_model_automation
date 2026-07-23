import unittest

import simpy

from ip_model_automation.ip import Command, CompletionIpModel


class TestCompletionIpModel(unittest.TestCase):
    def test_completion_emits_read(self):
        env = simpy.Environment()
        model = CompletionIpModel(
            env, service_latency=2, tenant_select_latency=1, token_check_latency=1, emit_latency=1
        )
        model.configure_tenant("T0", read=2, write=2, read_bw=20, write_bw=20)
        model.submit(Command("r0", "READ", tenant_id="T0", size_kb=4))
        env.run(until=8)
        self.assertEqual(model.completed[0][1].cmd_id, "r0")

    def test_completion_token_starvation_blocks_until_refill(self):
        env = simpy.Environment()
        model = CompletionIpModel(
            env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=1
        )
        model.configure_tenant("T0", read=2, write=0, read_bw=8, write_bw=0)
        model.submit(Command("w0", "WRITE", tenant_id="T0", size_kb=4))
        env.run(until=12)
        self.assertEqual(model.completed, [])
        self.assertGreater(model.metrics["token_stalls"], 0)
        model.base_tokens["T0"].update({"write": 1.0, "write_bw": 4.0})
        model.refill_once()
        env.run(until=24)
        self.assertEqual(model.completed[0][1].cmd_id, "w0")

    def test_completion_flush_costs_no_tokens(self):
        env = simpy.Environment()
        model = CompletionIpModel(
            env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=1
        )
        model.configure_tenant("T0", read=0, write=0, read_bw=0, write_bw=0)
        model.submit(Command("f0", "FLUSH", tenant_id="T0"))
        env.run(until=8)
        self.assertEqual(model.completed[0][1].cmd_id, "f0")
        self.assertEqual(model.metrics["token_stalls"], 0)

    def test_completion_write_debits_write_budget_only(self):
        env = simpy.Environment()
        model = CompletionIpModel(
            env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=1
        )
        model.configure_tenant("T0", read=2, write=1, read_bw=8, write_bw=4)
        model.submit(Command("w0", "WRITE", tenant_id="T0", size_kb=4))
        env.run(until=10)
        self.assertEqual(model.completed[0][1].cmd_id, "w0")
        self.assertEqual(model.tokens["T0"]["write"], 0.0)
        self.assertEqual(model.tokens["T0"]["write_bw"], 0.0)
        self.assertEqual(model.tokens["T0"]["read"], 2.0)
        self.assertEqual(model.tokens["T0"]["read_bw"], 8.0)

    def test_completion_inactive_tenant_parks_command_at_accept(self):
        env = simpy.Environment()
        model = CompletionIpModel(
            env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=1
        )
        model.configure_tenant("T0", read=2, write=2, read_bw=8, write_bw=8, alive=False)
        model.submit(Command("r0", "READ", tenant_id="T0", size_kb=4))
        env.run(until=4)
        self.assertEqual(model.metrics["accepted_commands"], 0)
        self.assertGreater(model.metrics["tenant_inactive_stalls"], 0)
        self.assertEqual(len(model.pending["T0"]), 1)

    def test_completion_scheduler_stalls_when_tenant_goes_inactive(self):
        env = simpy.Environment()
        model = CompletionIpModel(
            env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=1
        )
        model.configure_tenant("T0", read=2, write=2, read_bw=8, write_bw=8)
        model.submit(Command("r0", "READ", tenant_id="T0", size_kb=4))
        env.run(until=2)
        self.assertEqual(model.metrics["accepted_commands"], 1)
        model.tenant_alive["T0"] = False
        env.run(until=12)
        self.assertEqual(model.completed, [])
        self.assertGreater(model.metrics["tenant_inactive_stalls"], 0)
        self.assertEqual(len(model.pending["T0"]), 1)

    def test_completion_step_functional_debits_and_pops_ready_command(self):
        env = simpy.Environment()
        model = CompletionIpModel(
            env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=1
        )
        self.assertIsNone(model.step_functional())
        model.configure_tenant("T0", read=2, write=2, read_bw=8, write_bw=8)
        model.pending["T0"].append(Command("r0", "READ", tenant_id="T0", size_kb=4))
        issued = model.step_functional()
        self.assertEqual(issued.cmd_id, "r0")
        self.assertEqual(model.tokens["T0"]["read"], 1.0)
        self.assertEqual(model.tokens["T0"]["read_bw"], 4.0)
        self.assertEqual(len(model.pending["T0"]), 0)

    def test_completion_step_functional_reports_each_stall_kind(self):
        env = simpy.Environment()
        model = CompletionIpModel(
            env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=1
        )
        model.configure_tenant("T0", read=0, write=0, read_bw=0, write_bw=0)
        model.pending["T0"].append(Command("r0", "READ", tenant_id="T0", size_kb=4))

        model.tenant_alive["T0"] = False
        self.assertIsNone(model.step_functional())
        self.assertEqual(model.metrics["tenant_inactive_stalls"], 1)

        model.tenant_alive["T0"] = True
        model.set_completion_ready(False)
        self.assertIsNone(model.step_functional())
        self.assertEqual(model.metrics["output_stalls"], 1)

        model.set_completion_ready(True)
        self.assertIsNone(model.step_functional())
        self.assertEqual(model.metrics["token_stalls"], 1)
        self.assertEqual(len(model.pending["T0"]), 1)

    def test_completion_serves_pending_tenant_missing_from_weight_map(self):
        env = simpy.Environment()
        model = CompletionIpModel(
            env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=1
        )
        model.pending["TX"].append(Command("f0", "FLUSH", tenant_id="TX"))
        issued = model.step_functional()
        self.assertEqual(issued.cmd_id, "f0")

    def test_completion_refill_once_restores_base_tokens_and_snapshots_window(self):
        env = simpy.Environment()
        model = CompletionIpModel(
            env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=1
        )
        model.configure_tenant("T0", read=2, write=2, read_bw=8, write_bw=8)
        model.submit(Command("r0", "READ", tenant_id="T0", size_kb=4))
        env.run(until=8)
        self.assertEqual(model.tokens["T0"]["read"], 1.0)
        model.refill_once()
        self.assertEqual(model.tokens["T0"]["read"], 2.0)
        self.assertEqual(model.tokens["T0"]["read_bw"], 8.0)
        self.assertEqual(model.metrics["refill_windows"], 1)
        self.assertEqual(len(model.window_metrics), 1)
        self.assertEqual(model.window_metrics[0]["completed"], 1.0)

    def test_completion_refill_process_refills_each_window(self):
        env = simpy.Environment()
        model = CompletionIpModel(
            env,
            service_latency=1,
            tenant_select_latency=1,
            token_check_latency=1,
            emit_latency=1,
            refill_window=100,
            start_refill_process=True,
        )
        model.configure_tenant("T0", read=2, write=2, read_bw=8, write_bw=8)
        env.run(until=170)
        self.assertEqual(model.metrics["refill_windows"], 1)
        env.run(until=330)
        self.assertEqual(model.metrics["refill_windows"], 2)

    def test_completion_output_backpressure_holds_ready_command(self):
        env = simpy.Environment()
        model = CompletionIpModel(
            env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=1
        )
        model.configure_tenant("T0", read=1, write=1, read_bw=4, write_bw=4)
        model.set_completion_ready(False)
        model.submit(Command("r0", "READ", tenant_id="T0", size_kb=4))
        env.run(until=8)
        self.assertEqual(model.completed, [])
        self.assertEqual(len(model.pending["T0"]), 1)
        self.assertGreater(model.metrics["output_stalls"], 0)
        # completion_queue_if is wait_for_ack_inline: the scheduler holds the
        # completion at its wait point instead of emitting it.
        self.assertEqual(model.fsm_state["completion_scheduler"], "STALL_OUTPUT")
        model.set_completion_ready(True)
        env.run(until=14)
        self.assertEqual(model.completed[0][1].cmd_id, "r0")


if __name__ == "__main__":
    unittest.main()
