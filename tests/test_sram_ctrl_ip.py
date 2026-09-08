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

    def test_sram_rmw_bank_hold_excludes_a_competing_same_bank_request(self):
        """`rmw_holds_bank_for_read_merge_and_write` claims
        no_interleaved_access_to_same_bank_during_rmw.

        Nothing exercised it. The scenario's input_sequence is a single
        REQ2_RMW_bank2, so the only request in flight was the RMW itself and
        there was nothing that *could* have interleaved -- the test asserted
        bank_utilization[2] == 2 and a comment claimed the stronger property.
        A scheduler that released the bank between the read and write halves
        would have passed unchanged.

        The measurement queues a READ behind the RMW on the same bank. The
        RMW's two touches must be the only accesses to bank 2 until it
        completes, and it must still complete in the DLD-quoted 17 cycles with
        a competitor waiting.
        """
        env = simpy.Environment()
        model = SramCtrlIpModel(env)
        model.submit_request(Command("RMW0", "RMW", addr=2, source_id="S0"))
        model.submit_request(Command("RD0", "READ", addr=2, source_id="S0"))

        env.run(until=18)
        self.assertEqual(model.bank_utilization[2], 2, "a third access touched bank 2 during the RMW")
        self.assertEqual([command.cmd_id for _, command in model.completed], ["RMW0"])
        self.assertEqual(model.completed[0][0], 17, "RMW no longer costs the declared 17 cycles")
        self.assertEqual(len(model.bank_queues[2]), 1, "the competing read was serviced early")

        env.run(until=40)
        self.assertEqual(model.bank_utilization[2], 3)
        self.assertEqual([command.cmd_id for _, command in model.completed], ["RMW0", "RD0"])

    def test_sram_req_ready_is_low_exactly_while_accept_is_in_queue_full(self):
        """`central_and_bank_queue_full_deasserts_ready_until_drain` claims
        req_ready_deasserted_while_bank_queue_full.

        Nothing observed it. All nine requests were submitted at t=0 before
        env.run, so the whole burst was consumed inside one uninterrupted run
        and req_ready was only ever sampled once, at the end, already
        reasserted. The test proved reassertion and the stall counter; the
        deassertion the scenario is named for went unmeasured, and a model that
        never dropped req_ready at all would have passed.

        The measurement samples req_ready every cycle and asserts the
        relationship the scenario states: low if and only if request_accept is
        in QUEUE_FULL, once per counted stall, high again once drained.
        """
        env = simpy.Environment()
        model = SramCtrlIpModel(env)
        request_count = 9
        for index in range(request_count):
            model.submit_request(Command(f"REQ{3 + index}", "READ", addr=0, source_id="S0"))

        low_cycles = []
        states_while_low = set()
        for cycle in range(1, 400):
            env.run(until=cycle)
            if not model.req_ready:
                low_cycles.append(cycle)
                states_while_low.add(model.fsm_state["request_accept"])

        self.assertTrue(low_cycles, "req_ready was never observed low")
        self.assertEqual(states_while_low, {"QUEUE_FULL"}, "req_ready went low outside QUEUE_FULL")

        windows = sum(1 for i, c in enumerate(low_cycles) if i == 0 or c != low_cycles[i - 1] + 1)
        self.assertEqual(windows, model.metrics["queue_full_stalls"], "one deassertion per counted stall")

        self.assertTrue(model.req_ready, "req_ready was not reasserted once the queue drained")
        self.assertEqual(len(model.completed), request_count)

    def test_sram_response_hold_blocks_the_next_access_on_that_bank(self):
        """`response_backpressure_holds_bank_in_return_data` claims
        bank_not_started_on_new_access_while_held.

        Nothing exercised it. The scenario's input_sequence is one
        REQ8_READ_bank3 with rsp_ready low, so no second request existed to be
        wrongly started. The test proved the response is held; it could not
        prove the bank is.

        The measurement queues a second read on the same bank while the first
        is held in RETURN_DATA. Bank 3 must be touched exactly once for as long
        as the hold lasts, and both requests must complete in order once
        rsp_ready returns.
        """
        env = simpy.Environment()
        model = SramCtrlIpModel(env)
        model.set_response_ready(False)
        model.submit_request(Command("REQ8", "READ", addr=3, source_id="S0"))
        model.submit_request(Command("REQ8B", "READ", addr=3, source_id="S0"))

        env.run(until=50)
        self.assertEqual(model.fsm_state["bank_scheduler"], "RETURN_DATA")
        self.assertEqual(model.bank_utilization[3], 1, "a second access to bank 3 started during the hold")
        self.assertEqual(len(model.bank_queues[3]), 1, "the queued read left the bank queue during the hold")
        self.assertEqual(model.completed, [])

        model.set_response_ready(True)
        env.run(until=80)
        self.assertEqual(model.bank_utilization[3], 2)
        self.assertEqual([command.cmd_id for _, command in model.completed], ["REQ8", "REQ8B"])

    def test_sram_uncorrectable_rmw_still_issues_its_write_half(self):
        """M7: the model abandoned an RMW's write half on an uncorrectable read.

        Nothing on develop drove this path -- the uncorrectable scenario uses a
        plain READ -- so check_declared_transitions could not see the undeclared
        WAIT_ECC exit either, and the defect survived every deterministic gate.

        The first resolution corrected the template to match the model, arguing
        against writing into a line just declared unusable. The author reversed
        it: the controller does not make that call on the requester's behalf.
        So the RMW completes both halves and reports the ECC status it saw.
        """
        env = simpy.Environment()
        model = SramCtrlIpModel(env)
        model.forced_status["RMWU"] = "UNCORRECTABLE"
        model.submit_request(Command("RMWU", "RMW", addr=2, source_id="S0"))
        env.run(until=60)

        self.assertEqual(len(model.completed), 1)
        completed_at, command = model.completed[0]
        self.assertEqual(completed_at, 17, "the write half was skipped, not merely reported")
        self.assertEqual(model.bank_utilization[2], 2, "bank 2 was touched once, so no write was issued")
        self.assertEqual(command.status, "UNCORRECTABLE", "the ECC status the read half saw was lost")
        self.assertEqual(model.metrics["uncorrectable_count"], 1)
        self.assertIsNone(model.get_response("RMWU"))
        self.assertEqual(model.metrics["interrupts_asserted"], 0)


if __name__ == "__main__":
    unittest.main()
