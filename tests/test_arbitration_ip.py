import unittest

import simpy

from ip_model_automation.ip import ArbitrationIpModel, Command


class TestArbitrationIpModel(unittest.TestCase):
    def test_arbitration_issues_command(self):
        env = simpy.Environment()
        model = ArbitrationIpModel(
            env,
            {"T0": 1},
            scan_latency=1,
            issue_latency=1,
            pending_count_latency=1,
            burst_read_latency=1,
            burst_calc_latency=1,
            burst_debit_latency=1,
        )
        model.enqueue(Command("c0", "READ", tenant_id="T0"))
        env.run(until=20)
        self.assertEqual(model.metrics["grants"], 1)
        self.assertEqual(model.metrics["downstream_requests"], 1)
        self.assertEqual(model.downstream_requests[0]["issue_count"], 1)
        self.assertEqual(model.metrics["policy_updates"], 1)
        self.assertIn("policy_update", model.fsm_state)

    def test_arbitration_updates_hierarchical_bitmaps(self):
        env = simpy.Environment()
        model = ArbitrationIpModel(
            env,
            scan_latency=1,
            issue_latency=1,
            pending_count_latency=1,
            burst_read_latency=1,
            burst_calc_latency=1,
            burst_debit_latency=1,
        )
        model.enqueue(Command("c0", "READ", port_id="port1", tenant_id="T2", sq_id="SQ1"))

        env.run(until=1)
        self.assertTrue(model.bitmap_dirty)

        env.run(until=20)
        self.assertTrue(model.selection_trace)
        self.assertEqual(model.selection_trace[0], {"port_id": "port1", "tenant_id": "T2", "sq_id": "SQ1"})
        self.assertFalse(model.port_pending_bitmap["port1"])
        self.assertFalse(model.tenant_pending_bitmap["port1"]["T2"])
        self.assertFalse(model.sq_pending_bitmap["T2"]["SQ1"])

    def test_arbitration_single_port_ignores_port1(self):
        env = simpy.Environment()
        model = ArbitrationIpModel(env, scan_latency=1, issue_latency=1, port_mode="single")
        model.enqueue(Command("p1", "READ", port_id="port1", tenant_id="T2", sq_id="SQ0"))
        env.run(until=6)

        self.assertEqual(model.metrics["grants"], 0)
        self.assertFalse(model.port_pending_bitmap["port1"])

    def test_arbitration_weighted_tenant_selection_under_port(self):
        env = simpy.Environment()
        weights = {"ports": {"port0": 1}, "tenants": {"T0": 1, "T1": 4}, "sqs": {"SQ0": 1}}
        model = ArbitrationIpModel(
            env,
            weights=weights,
            scan_latency=1,
            issue_latency=1,
            port_mode="single",
            pending_count_latency=1,
            burst_read_latency=1,
            burst_calc_latency=1,
            burst_debit_latency=1,
        )
        model.enqueue(Command("t0", "READ", port_id="port0", tenant_id="T0", sq_id="SQ0"))
        for idx in range(4):
            model.enqueue(Command(f"t1_{idx}", "READ", port_id="port0", tenant_id="T1", sq_id="SQ0"))

        env.run(until=80)

        self.assertEqual([entry["tenant_id"] for entry in model.selection_trace[:2]], ["T0", "T1"])
        self.assertEqual([request["issue_count"] for request in model.downstream_requests[:2]], [1, 4])

    def test_arbitration_issue_pipeline_uses_min_burst_and_pending_count(self):
        env = simpy.Environment()
        model = ArbitrationIpModel(
            env,
            scan_latency=1,
            issue_latency=1,
            port_mode="single",
            pending_count_latency=1,
            burst_read_latency=1,
            burst_calc_latency=1,
            burst_debit_latency=1,
        )
        model.configure_burst(device=6, tenants={"T1": 3}, sqs={"T1": {"SQ0": 5}})
        for idx in range(8):
            model.enqueue(Command(f"cmd{idx}", "READ", port_id="port0", tenant_id="T1", sq_id="SQ0"))

        env.run(until=30)

        request = model.downstream_requests[0]
        self.assertEqual(request["pending_cmd_count"], 8)
        self.assertEqual(request["issue_count"], 3)
        self.assertEqual(request["cmd_ids"], ["cmd0", "cmd1", "cmd2"])
        self.assertEqual(model.device_burst_available, 3)
        self.assertEqual(model.tenant_burst_available["T1"], 0)
        self.assertEqual(model.sq_burst_available["T1"]["SQ0"], 2)
        self.assertEqual(len(model.queues["port0"]["T1"]["SQ0"]), 5)

    def test_arbitration_downstream_backpressure_holds_selected_sq(self):
        env = simpy.Environment()
        model = ArbitrationIpModel(
            env,
            scan_latency=1,
            issue_latency=1,
            pending_count_latency=1,
            burst_read_latency=1,
            burst_calc_latency=1,
            burst_debit_latency=1,
            port_mode="single",
        )
        model.set_issue_ready(False)
        model.enqueue(Command("held", "READ", port_id="port0", tenant_id="T0", sq_id="SQ0"))

        env.run(until=12)

        self.assertEqual(model.downstream_requests, [])
        self.assertEqual(len(model.queues["port0"]["T0"]["SQ0"]), 1)
        self.assertGreater(model.metrics["output_backpressure_cycles"], 0)
        self.assertEqual(model.fsm_state["issue_pipeline"], "ISSUE_STALL")

        model.set_issue_ready(True)
        env.run(until=20)

        self.assertEqual(model.downstream_requests[0]["cmd_ids"], ["held"])
        self.assertEqual(len(model.queues["port0"]["T0"]["SQ0"]), 0)

    def test_arbitration_query_pending_bitmaps_api(self):
        env = simpy.Environment()
        model = ArbitrationIpModel(env, scan_latency=1, issue_latency=1)
        model.enqueue(Command("b0", "READ", port_id="port1", tenant_id="T2", sq_id="SQ1"))

        bitmaps = model.query_pending_bitmaps()

        self.assertTrue(bitmaps["port"]["port1"])
        self.assertTrue(bitmaps["tenant"]["port1"]["T2"])
        self.assertTrue(bitmaps["sq"]["T2"]["SQ1"])


if __name__ == "__main__":
    unittest.main()
