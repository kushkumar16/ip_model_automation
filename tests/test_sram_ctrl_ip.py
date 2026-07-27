import unittest

import simpy

from ip_model_automation.ip import Command, SramCtrlIpModel


class TestSramCtrlIpModel(unittest.TestCase):
    def test_sram_uncontended_read_matches_dld_latency(self):
        env = simpy.Environment()
        model = SramCtrlIpModel(env)
        model.submit_request(Command("REQ0", "READ", addr=0, source_id="S0"))
        env.run(until=30)
        response = model.get_response("REQ0")
        self.assertIsNotNone(response)
        self.assertEqual(response["rsp_status"], "OK")
        self.assertEqual(response["rsp_id"], "REQ0")
        self.assertEqual(response["latency"], 11)
        self.assertEqual(model.metrics["latency"], 11)

    def test_sram_write_completes_without_response_payload(self):
        env = simpy.Environment()
        model = SramCtrlIpModel(env)
        model.submit_request(Command("REQ1", "WRITE", addr=1, source_id="S0"))
        env.run(until=30)
        self.assertIsNone(model.get_response("REQ1"))
        self.assertEqual(len(model.completed), 1)
        self.assertEqual(model.completed[0][1].cmd_id, "REQ1")
        self.assertEqual(model.metrics["latency"], 8)

    def test_sram_rmw_holds_bank_for_read_merge_and_write(self):
        env = simpy.Environment()
        model = SramCtrlIpModel(env)
        model.submit_request(Command("REQ2", "RMW", addr=2, source_id="S0"))
        env.run(until=30)
        self.assertIsNone(model.get_response("REQ2"))
        self.assertEqual(len(model.completed), 1)
        self.assertEqual(model.metrics["latency"], 17)
        # bank 2 was touched twice (read half + write half) with nothing else
        # interleaved in between, since bank_scheduler never left that bank's
        # transaction to service another request.
        self.assertEqual(model.bank_utilization[2], 2)

    def test_sram_central_and_bank_queue_full_deasserts_ready_until_drain(self):
        env = simpy.Environment()
        model = SramCtrlIpModel(env)
        # bank_scheduler keeps roughly abreast of a slow trickle of requests, so
        # a burst comfortably larger than the per-bank depth (4) is needed to
        # actually observe the accept path deassert req_ready.
        request_count = 9
        for index in range(request_count):
            model.submit_request(Command(f"REQ{3 + index}", "READ", addr=0, source_id="S0"))
        env.run(until=400)
        self.assertTrue(model.req_ready)
        self.assertGreater(model.metrics["queue_full_stalls"], 0)
        self.assertEqual(len(model.completed), request_count)

    def test_sram_response_backpressure_holds_bank_in_return_data(self):
        env = simpy.Environment()
        model = SramCtrlIpModel(env)
        model.set_response_ready(False)
        model.submit_request(Command("REQ8", "READ", addr=3, source_id="S0"))
        env.run(until=20)
        self.assertEqual(model.fsm_state["bank_scheduler"], "RETURN_DATA")
        self.assertEqual(model.completed, [])
        self.assertIsNone(model.get_response("REQ8"))
        self.assertEqual(model.metrics["queue_full_stalls"], 0)
        model.set_response_ready(True)
        env.run(until=25)
        response = model.get_response("REQ8")
        self.assertIsNotNone(response)
        self.assertEqual(response["rsp_status"], "OK")

    def test_sram_scrub_config_applied_at_next_boundary(self):
        env = simpy.Environment()
        model = SramCtrlIpModel(env)
        model.configure(scrub_interval_cycles=1024)
        second_ack = model.configure(scrub_interval_cycles=2048)
        env.run(until=1)
        self.assertLessEqual(len(model.config_queue.items), 1)
        self.assertFalse(second_ack.triggered)

        model.trigger_scrub_now()
        env.run(until=10)
        self.assertEqual(model.scrub_interval_cycles, 1024)
        self.assertTrue(second_ack.triggered)

        model.trigger_scrub_now()
        env.run(until=25)
        self.assertEqual(model.scrub_interval_cycles, 2048)

    def test_sram_scrub_yields_to_demand_and_reports_counters(self):
        env = simpy.Environment()
        model = SramCtrlIpModel(env)
        # Mask bank 1 out of bank_scheduler's own round robin so the injected
        # entry stays put as "queued demand" for the scrub process to notice,
        # rather than being drained by the scheduler before scrub ever looks.
        model.bank_mask = 0b1101
        model.bank_queues[1].append(Command("PENDING", "READ", addr=1, source_id="S0"))
        model._scrub_cursor = 1

        model.trigger_scrub_now()
        env.run(until=10)
        self.assertEqual(model.metrics["scrub_yields_to_demand"], 1)
        self.assertEqual(len(model.bank_queues[1]), 1)
        self.assertEqual(model.metrics["corrected_count"], 0)

        model.scrub_forced_errors[2] = True
        model.trigger_scrub_now()
        env.run(until=30)
        self.assertEqual(model.metrics["corrected_count"], 1)
        self.assertEqual(model.metrics["uncorrectable_count"], 0)

    def test_sram_rmw_to_masked_bank_rejected_at_accept(self):
        env = simpy.Environment()
        model = SramCtrlIpModel(env)
        model.configure(bank_mask=0b1101)  # clear bank 1
        model.trigger_scrub_now()
        env.run(until=10)
        self.assertEqual(model.bank_mask, 0b1101)

        model.submit_request(Command("REQ9", "RMW", addr=1, source_id="S0"))
        env.run(until=30)
        self.assertEqual(model.metrics["rmw_masked_bank_rejections"], 1)
        self.assertEqual(len(model.bank_queues[1]), 0)
        self.assertEqual(model.completed, [])

    def test_sram_uncorrectable_error_reported_via_status_only(self):
        env = simpy.Environment()
        model = SramCtrlIpModel(env)
        model.forced_status["REQ10"] = "UNCORRECTABLE"
        model.submit_request(Command("REQ10", "READ", addr=3, source_id="S0"))
        env.run(until=30)
        response = model.get_response("REQ10")
        self.assertIsNotNone(response)
        self.assertEqual(response["rsp_status"], "UNCORRECTABLE")
        self.assertEqual(model.metrics["uncorrectable_count"], 1)
        self.assertEqual(model.metrics["interrupts_asserted"], 0)


if __name__ == "__main__":
    unittest.main()
