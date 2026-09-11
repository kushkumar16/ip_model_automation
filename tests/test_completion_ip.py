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
        """A tenant with no tokens is not eligible, so it is not polled at all.

        This used to assert `token_stalls > 0`: the scheduler selected the
        starved tenant, paid the tenant select and the token check, and recorded
        a stall, once per retry, for as long as the starvation lasted. Under the
        eligibility rule it is not selected while it holds no tokens on the axes
        its head command needs, so the command waits without costing selects.
        The stall counter is for a tenant that holds *some* tokens and still
        cannot afford its head command -- see the test below.
        """
        env = simpy.Environment()
        model = CompletionIpModel(
            env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=1
        )
        model.configure_tenant("T0", read=2, write=0, read_bw=8, write_bw=0)
        model.submit(Command("w0", "WRITE", tenant_id="T0", size_kb=4))
        env.run(until=12)
        self.assertEqual(model.completed, [])
        self.assertNotIn("T0", model.eligible_tenants, "a tenant with no write tokens was still eligible")
        self.assertEqual(model.metrics["token_stalls"], 0, "the starved tenant was selected anyway")

        model.base_tokens["T0"].update({"write": 1.0, "write_bw": 4.0})
        model.refill_once()
        env.run(until=24)
        self.assertEqual(model.completed[0][1].cmd_id, "w0")

    def test_completion_idle_scheduler_waits_instead_of_selecting(self):
        """An idle scheduler costs nothing, and the declared path is phase-flat.

        The scheduler used to pay an 8-cycle tenant select every pass with
        nothing to serve. It now waits in IDLE until a tenant has both active
        traffic and available tokens.

        The second assertion is the one that matters for the timing model: while
        the scheduler free-ran, the end-to-end latency of a single no-stall READ
        varied between 14 and 22 cycles with arrival phase, because a command
        either made the select that was already running or waited for the next.
        Waiting on eligibility removes the phase entirely.
        """
        env = simpy.Environment()
        model = CompletionIpModel(env, log_level="CRITICAL")
        model.configure_tenant("T0", read=9, write=9, read_bw=90, write_bw=90)
        env.run(until=500)

        self.assertEqual(model.metrics["stalls"], 0, "an idle scheduler is still selecting")
        self.assertEqual(model.fsm_state["completion_scheduler"], "IDLE")

        model.submit(Command("late", "READ", tenant_id="T0", size_kb=1))
        env.run(until=700)
        self.assertEqual(
            [command.cmd_id for _, command in model.completed], ["late"], "the wake after a long idle was lost"
        )

        latencies = []
        for phase in range(10):
            env = simpy.Environment()
            model = CompletionIpModel(env, log_level="CRITICAL")
            model.configure_tenant("T0", read=9, write=9, read_bw=90, write_bw=90)

            def arrive(at=phase, target=model):
                yield env.timeout(at)
                target.submit(Command("r0", "READ", tenant_id="T0", size_kb=1))

            env.process(arrive())
            env.run(until=300)
            latencies.append(model.completed[0][0] - phase)

        self.assertEqual(set(latencies), {4 + 8 + 5 + 4}, "the declared path still varies with arrival phase")

    def test_completion_partial_tokens_still_reach_wait_tokens(self):
        """CHECK_TOKENS -> WAIT_TOKENS, which eligibility must not make dead.

        Eligibility is a tenant-level test -- does it hold any tokens on the
        axes its head command needs -- not a command-level one. A tenant holding
        some bandwidth but not enough for its head command is therefore eligible,
        is selected, and stalls at the token check. Defining eligibility per
        command would have made this declared transition unreachable.
        """
        env = simpy.Environment()
        model = CompletionIpModel(
            env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=1
        )
        model.configure_tenant("T0", read=5, write=5, read_bw=2, write_bw=2)
        model.submit(Command("r0", "READ", tenant_id="T0", size_kb=8))
        env.run(until=20)

        self.assertEqual(model.completed, [], "a command was emitted it could not pay for")
        self.assertIn("T0", model.eligible_tenants, "a tenant holding tokens was ruled ineligible")
        self.assertGreater(model.metrics["token_stalls"], 0, "CHECK_TOKENS -> WAIT_TOKENS was never taken")

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

    def test_completion_backpressure_is_queue_full_not_tenant_inactive(self):
        """READY -> BACKPRESSURE is declared for incoming_valid_and_queue_full.

        The model used to enter BACKPRESSURE when the tenant was not alive -- a
        different condition the template does not name at accept -- and in that
        branch it appended the command to pending anyway, so it was an enqueue
        wearing the label of a stall. Nothing ever drove the declared condition,
        because tenant_pending_queues is declared unbounded and an unbounded
        queue cannot be full. pending_depth leaves it unbounded by default and
        lets a caller bound it, which is what makes the declared transition
        reachable at all.
        """
        env = simpy.Environment()
        model = CompletionIpModel(
            env,
            service_latency=1,
            tenant_select_latency=1,
            token_check_latency=1,
            emit_latency=1,
            pending_depth=1,
            log_level="CRITICAL",
        )
        model.configure_tenant("T0", read=0, write=0, read_bw=0, write_bw=0)
        model.submit(Command("a", "READ", tenant_id="T0", size_kb=4))
        model.submit(Command("b", "READ", tenant_id="T0", size_kb=4))
        env.run(until=40)

        # The tenant has no tokens, so nothing drains and the second command
        # meets a full queue.
        self.assertEqual(len(model.pending["T0"]), 1)
        self.assertGreater(model.metrics["queue_full_stalls"], 0)
        self.assertEqual(model.fsm_state["accept"], "BACKPRESSURE")
        self.assertEqual(model.metrics["tenant_inactive_stalls"], 0, "this is not a liveness stall")

        # Space appears, and accept resumes -- BACKPRESSURE -> READY.
        model.pending["T0"].clear()
        env.run(until=80)
        self.assertEqual(len(model.pending["T0"]), 1, "the held command was never enqueued")
        self.assertEqual(model.fsm_state["accept"], "READY")

    def test_completion_output_port_serialises_competing_requesters(self):
        """completion_output_port is declared capacity 1, and must enforce it.

        The previous version of this test submitted two commands and asserted
        the gap between their completions. It could not fail: the model runs one
        completion_scheduler process, which emits its commands one after another
        whatever the port's capacity is. Capacity 1, 1000 and 1,000,000 all gave
        the same completion times, and so did replacing the port with a no-op --
        the gap being measured was the scheduler's own loop.

        A capacity is only observable when something contends for it, so this
        drives a second requester of the declared port and asserts it waits.
        Raising the capacity makes this test fail, which is the property the old
        one lacked.
        """
        env = simpy.Environment()
        model = CompletionIpModel(env, emit_latency=6, log_level="CRITICAL")
        holds = []

        def requester(name, hold):
            with model.completion_output_port.request() as port:
                yield port
                holds.append((name, env.now))
                yield env.timeout(hold)

        env.process(requester("first", 6))
        env.process(requester("second", 6))
        env.run(until=100)

        self.assertEqual([name for name, _ in holds], ["first", "second"])
        self.assertEqual(holds[0][1], 0)
        self.assertEqual(holds[1][1], 6, "the second requester did not wait for the port")

    def test_completion_scheduler_holds_the_port_across_its_emit(self):
        """The scheduler must actually take the declared port, not bypass it."""
        env = simpy.Environment()
        model = CompletionIpModel(
            env, service_latency=1, tenant_select_latency=1, token_check_latency=1, emit_latency=6
        )
        model.configure_tenant("T0", read=4, write=4, read_bw=40, write_bw=40)
        model.submit(Command("r0", "READ", tenant_id="T0", size_kb=4))

        seen = []

        def watcher():
            while True:
                seen.append(model.completion_output_port.count)
                yield env.timeout(1)

        env.process(watcher())
        env.run(until=40)
        self.assertEqual(len(model.completed), 1)
        self.assertIn(1, seen, "the emit never held the completion output port")

    def test_completion_output_port_latency_is_charged_off_the_critical_path(self):
        """The port's declared latency_cycles: 1, which was charged nowhere.

        Charging it inline would make the declared no_stall_completion path 18
        rather than the 17 the template states and the model matches, so the
        port pays it in the background: the completion is done at the end of the
        emit, and the port stays busy one cycle longer. Both halves are pinned
        here -- a port latency that changed nothing observable would be no better
        than the uncharged one it replaced.
        """
        env = simpy.Environment()
        model = CompletionIpModel(env, log_level="CRITICAL")
        model.configure_tenant("T0", read=9, write=9, read_bw=90, write_bw=90)
        model.submit(Command("r0", "READ", tenant_id="T0", size_kb=1))

        busy = []

        def watcher():
            while True:
                busy.append((env.now, model.completion_output_port.count))
                yield env.timeout(1)

        env.process(watcher())
        env.run(until=120)

        self.assertEqual(len(model.completed), 1)
        completed_at = model.completed[0][0]
        # enqueue 4 + tenant_select 8 + token_check 5 + emit 4 = 21, the figure
        # the DLD states in its end-to-end table and again in its sequential
        # dependency. The enqueue is on the critical path because the scheduler
        # waits in IDLE until a tenant is eligible, and a tenant cannot become
        # eligible before its command has been enqueued.
        self.assertEqual(completed_at, 4 + 8 + 5 + 4)

        held = [now for now, count in busy if count]
        self.assertEqual(
            len(held),
            model.emit_latency + model.port_latency,
            "the port was not busy for its own latency beyond the emit",
        )
        self.assertEqual(max(held), completed_at, "the port was released before its declared latency elapsed")

    def test_completion_scheduler_takes_the_more_candidates_shortcut(self):
        """EMIT -> SELECT_TENANT is declared, and was never taken.

        Every command used to re-enter the loop at IDLE, which the template
        declares no transition into. With a second command already pending the
        scheduler must go straight back to SELECT_TENANT instead, taking the
        declared `more_candidates` edge. The select itself is still paid -- that
        is what SELECT_TENANT costs -- so what this pins is the path, not a
        saving: tenant_select + token_check + emit, with no idle poll between.
        """
        env = simpy.Environment()
        model = CompletionIpModel(
            env, service_latency=1, tenant_select_latency=8, token_check_latency=1, emit_latency=1
        )
        model.configure_tenant("T0", read=4, write=4, read_bw=40, write_bw=40)
        model.submit(Command("r0", "READ", tenant_id="T0", size_kb=4))
        model.submit(Command("r1", "READ", tenant_id="T0", size_kb=4))
        env.run(until=100)

        self.assertEqual(len(model.completed), 2)
        first, second = model.completed[0][0], model.completed[1][0]
        self.assertEqual(
            second - first,
            8 + 1 + 1,
            "the second completion did not re-pay a tenant select it should have skipped",
        )

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

    def test_completion_refill_performs_each_action_on_its_declared_transition(self):
        """The refill_restores_token_window scenario, which nothing exercised.

        Its fsm_coverage names ASSESS_USAGE, REFILL_BASE and PUBLISH_METRICS.
        One refill test called refill_once() directly, entering none of them; the
        other ran the process but asserted only a counter. Between them the three
        declared actions could be moved back off their transitions -- restoring
        tokens 20 cycles late and timestamping the window snapshot 40 cycles late
        -- with the whole suite green.
        """
        env = simpy.Environment()
        model = CompletionIpModel(
            env,
            service_latency=1,
            tenant_select_latency=1,
            token_check_latency=1,
            emit_latency=1,
            refill_window=100,
            start_refill_process=True,
            log_level="CRITICAL",
        )
        model.configure_tenant("T0", read=2, write=2, read_bw=8, write_bw=8)
        model.tokens["T0"].update({"read": 0.0, "write": 0.0, "read_bw": 0.0, "write_bw": 0.0})

        # restore_base_tokens is declared on ASSESS_USAGE -> REFILL_BASE, which
        # completes at window + usage_assessment = 120.
        env.run(until=119)
        self.assertEqual(model.tokens["T0"]["read"], 0.0, "tokens came back before REFILL_BASE was reached")
        env.run(until=121)
        self.assertEqual(model.tokens["T0"]["read"], 2.0, "tokens were not restored on entering REFILL_BASE")

        # write_window_metrics is declared on REFILL_BASE -> PUBLISH_METRICS, and
        # the snapshot it writes was taken at the window boundary, not when it is
        # published.
        env.run(until=129)
        self.assertEqual(model.window_metrics, [], "the window metric was published before PUBLISH_METRICS")
        env.run(until=131)
        self.assertEqual(len(model.window_metrics), 1)
        self.assertEqual(model.window_metrics[0]["time"], 100.0, "the snapshot did not carry the window boundary")

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
