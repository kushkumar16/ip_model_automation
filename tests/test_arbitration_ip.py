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

    def _weighted_selection_sequence(self, tenant_weights):
        """Tenant selection order under `tenant_weights`, one command per SQ.

        One command in each SQ is what makes weighting observable at all. The
        previous version of this test put four commands in a single T1 queue, so
        the first grant drained all four at once, T1 was selected exactly once,
        and the weighted pointer was never revisited. With nothing to revisit,
        the assertion held for every weighting.
        """
        env = simpy.Environment()
        model = ArbitrationIpModel(
            env,
            weights={"ports": {"port0": 1}, "tenants": tenant_weights, "sqs": {}},
            scan_latency=1,
            issue_latency=1,
            port_mode="single",
            pending_count_latency=1,
            burst_read_latency=1,
            burst_calc_latency=1,
            burst_debit_latency=1,
            log_level="CRITICAL",
        )
        for sq in ("SQ0", "SQ1"):
            model.enqueue(Command(f"t0_{sq}", "READ", port_id="port0", tenant_id="T0", sq_id=sq))
        for sq in ("SQ0", "SQ1", "SQ2", "SQ3"):
            model.enqueue(Command(f"t1_{sq}", "READ", port_id="port0", tenant_id="T1", sq_id=sq))
        env.run(until=400)
        return [entry["tenant_id"] for entry in model.selection_trace]

    def test_arbitration_weighted_tenant_selection_under_port(self):
        """Weighted round-robin must select T1 four times per T0 at 1:4.

        WeightedOrder expands the weights into a list -- {T0:1, T1:4} becomes
        [T0, T1, T1, T1, T1] -- and advance_to_after moves the pointer past the
        entry it selected. So the weighting is only visible across repeated
        rounds in which both tenants are still pending.
        """
        self.assertEqual(
            self._weighted_selection_sequence({"T0": 1, "T1": 4}),
            ["T0", "T1", "T1", "T1", "T1", "T0"],
        )

    def test_arbitration_weighting_changes_the_order_it_selects_in(self):
        """The test above must be able to fail, which it previously could not.

        Its predecessor passed identically at {T0:1,T1:4}, {1,1}, {4,1}, {1,9}
        and even {99,1}: it asserted a two-element prefix that plain round-robin
        satisfies just as well as weighted. A weighting test that cannot tell
        WRR from RR is not a weak test, it is an absent one -- so this pins the
        contrast rather than trusting the sequence above to be weight-derived.
        """
        weighted = self._weighted_selection_sequence({"T0": 1, "T1": 4})
        equal = self._weighted_selection_sequence({"T0": 1, "T1": 1})
        inverted = self._weighted_selection_sequence({"T0": 4, "T1": 1})

        # These sequences are measured, not derived, and the equal-weight one is
        # phase-sensitive: it moved from T0,T1,T1,T0,T1,T1 when the arbiter
        # stopped spinning IDLE -> PORT_SCAN -> STALL while idle, which changed
        # when a scan lands relative to the issue pipeline's bitmap updates. The
        # weighted and inverted sequences did not move. What this test pins is
        # the contrast below, which no round-robin can satisfy; the exact orders
        # are the strongest available form of that, not a separate claim about
        # scheduling phase.
        self.assertEqual(equal, ["T0", "T1", "T1", "T1", "T0", "T1"])
        self.assertEqual(inverted, ["T0", "T0", "T1", "T1", "T1", "T1"])
        self.assertNotEqual(weighted, equal, "weighting made no difference to the order")
        self.assertNotEqual(weighted, inverted, "inverting the weights made no difference")

    def test_arbitration_weighted_order_across_four_tenants(self):
        """The weighted_order_four_tenants scenario, which no test drove.

        Port alternation comes first: arbitration.hierarchy is [port, tenant, sq]
        and the two ports carry equal weight, so selections alternate port0 and
        port1 until one side runs dry. Tenant weighting then orders the choice
        within a port. The scenario used to declare T0,T1,T1,T1,T1,T2,T2,T3,
        which ignored the port level entirely and asked for eight selections
        from four queued commands.
        """
        weighted = self._four_tenant_sequence({"T0": 1, "T1": 4, "T2": 2, "T3": 1})
        equal = self._four_tenant_sequence({"T0": 1, "T1": 1, "T2": 1, "T3": 1})

        self.assertEqual(weighted, ["T0", "T2", "T1", "T2", "T1", "T3", "T1", "T1", "T0"])
        self.assertEqual(equal, ["T0", "T2", "T1", "T3", "T0", "T2", "T1", "T1", "T1"])
        self.assertNotEqual(weighted, equal, "tenant weights made no difference to the order")

    def _four_tenant_sequence(self, tenant_weights):
        env = simpy.Environment()
        model = ArbitrationIpModel(
            env,
            weights={"ports": {"port0": 1, "port1": 1}, "tenants": tenant_weights, "sqs": {}},
            log_level="CRITICAL",
        )
        topology = {
            "port0": {"T0": ("SQ0", "SQ1"), "T1": ("SQ0", "SQ1", "SQ2", "SQ3")},
            "port1": {"T2": ("SQ0", "SQ1"), "T3": ("SQ0",)},
        }
        for port_id, tenants in topology.items():
            for tenant_id, sqs in tenants.items():
                for sq_id in sqs:
                    model.enqueue(
                        Command(f"{tenant_id}_{sq_id}", "READ", port_id=port_id, tenant_id=tenant_id, sq_id=sq_id)
                    )
        env.run(until=2000)
        return [entry["tenant_id"] for entry in model.selection_trace]

    def test_arbitration_charges_its_declared_latencies_at_defaults(self):
        """The declared timing model, which no test exercised.

        Every other test in this file overrides the latencies -- and until
        scan_latency was fixed, passing it also zeroed tenant_scan, sq_scan and
        grant, so three of the four declared arbiter_main costs were absent from
        every test that existed. This one runs at model defaults and pins the
        declared no_stall_command_issue path of 23 cycles: bitmap_update 3 +
        port_scan 4 + tenant_scan 6 + sq_scan 8 + grant_selected_sq 2.

        Measured from the arbiter entering IDLE, not from enqueue. The arbiter
        free-runs, so the wait between a command arriving and the next scan
        picking it up is arrival phase, not declared cost -- timing the path from
        enqueue would pin the phase instead of the latencies.
        """
        env = simpy.Environment()
        model = ArbitrationIpModel(env, log_level="CRITICAL")

        def arrive_after_reset():
            yield env.timeout(100)
            model.enqueue(Command("c", "READ", port_id="port0", tenant_id="T0", sq_id="SQ0"))

        env.process(arrive_after_reset())

        entered = {}
        previous = None
        while env.peek() < 400:
            env.step()
            state = model.fsm_state["arbiter_main"]
            if state != previous and env.now >= 100:
                entered.setdefault(state, env.now)
                previous = state
            if model.downstream_requests:
                break

        self.assertIn("IDLE", entered, "the arbiter never refreshed its bitmaps for the new command")
        self.assertIn("GRANT", entered)
        grant_completes = entered["GRANT"] + model.latency["grant"]
        self.assertEqual(grant_completes - entered["IDLE"], 23)

    def test_arbitration_issue_pipeline_charges_its_declared_latencies(self):
        """The burst_min_issue_count scenario's declared timing, which nothing pinned.

        That scenario declares pending_count_read_latency, burst_read_latency and
        min_calculation_latency as expected performance properties over five
        issue_pipeline states. The test named for it set every one of those
        latencies to 1 and asserted counts only, so the whole issue-pipeline half
        of the timing model was unheld: pending_count 3->9, burst_read 4->9,
        burst_calc 2->9 and burst_debit 2->9 each left the suite green. Verified
        by mutation, before and after.

        The spans below are the template's own placement: each operation is
        priced on a transition, so a state holds whatever its exit edge carries.
        READ_PENDING_COUNT's exit carries 3; the 1-cycle selection_accept sits on
        the edge *into* it, leaving WAIT_SELECTION.

        This assertion used to read `1 + 3` for READ_PENDING_COUNT, matching a
        model that charged both inside the state -- a 4 the template states
        nowhere. Pinning it made the test reject the placement the template does
        state, which is the opposite of what a test for declared timing is for.
        """
        env = simpy.Environment()
        model = ArbitrationIpModel(env, log_level="CRITICAL")
        model.enqueue(Command("c", "READ", port_id="port0", tenant_id="T0", sq_id="SQ0"))

        entered = {}
        entered_arbiter = {}
        previous = None
        previous_arbiter = None
        while env.peek() < 300:
            env.step()
            state = model.fsm_state["issue_pipeline"]
            if state != previous:
                entered.setdefault(state, env.now)
                previous = state
            arbiter_state = model.fsm_state["arbiter_main"]
            if arbiter_state != previous_arbiter:
                entered_arbiter.setdefault(arbiter_state, env.now)
                previous_arbiter = arbiter_state
            if model.downstream_requests and state == "UPDATE_BURST":
                break

        self.assertEqual(model.downstream_requests[0]["time"] - entered["UPDATE_BURST"], model.latency["burst_debit"])
        spans = {
            "READ_PENDING_COUNT": entered["READ_BURST"] - entered["READ_PENDING_COUNT"],
            "READ_BURST": entered["CALC_ISSUE_COUNT"] - entered["READ_BURST"],
            "CALC_ISSUE_COUNT": entered["ISSUE_REQUEST"] - entered["CALC_ISSUE_COUNT"],
            "ISSUE_REQUEST": entered["UPDATE_BURST"] - entered["ISSUE_REQUEST"],
        }
        self.assertEqual(
            spans,
            {
                "READ_PENDING_COUNT": 3,  # pending_count_read, on its exit edge
                "READ_BURST": 4,  # burst_read
                "CALC_ISSUE_COUNT": 2,  # min_burst_calculation
                "ISSUE_REQUEST": 3,  # downstream_issue_request
            },
        )
        # The declared new_command_to_issue path totals 38 cycles. Measured from
        # READ_PENDING_COUNT the pipeline's remaining share is five operations:
        # selection_accept is already paid by then, on the edge in.
        self.assertEqual(model.downstream_requests[0]["time"] - entered["READ_PENDING_COUNT"], 3 + 4 + 2 + 3 + 2)

        # ...which is exactly why the pipeline's whole share must also be pinned
        # from the handoff. Every span above starts at or after READ_PENDING_COUNT,
        # so none of them contains selection_accept, and deleting its timeout
        # outright left this entire test green -- the declared 38-cycle path would
        # have silently become 37. Anchoring at the grant covers all six.
        grant_completed = entered_arbiter["GRANT"] + model.latency["grant"]
        self.assertEqual(model.downstream_requests[0]["time"] - grant_completed, 1 + 3 + 4 + 2 + 3 + 2)

    def test_arbitration_issue_ready_dropping_during_the_request_holds_the_command(self):
        """issue_ready lowered after ISSUE_REQUEST is entered, which no test drove.

        The declared invariants are that commands are popped only when
        issue_ready is true and that queue entries remain pending during
        downstream backpressure. The existing backpressure test lowers
        issue_ready *before* the selection is made, so it never exercises the
        re-drive that makes those invariants hold mid-request -- deleting that
        re-drive entirely left the whole suite green.
        """
        env = simpy.Environment()
        model = ArbitrationIpModel(env, log_level="CRITICAL")
        model.enqueue(Command("c0", "READ", port_id="port0", tenant_id="T0", sq_id="SQ0"))

        def drop_once_inside_the_request():
            while model.fsm_state["issue_pipeline"] != "ISSUE_REQUEST":
                yield env.timeout(1)
            model.set_issue_ready(False)
            yield env.timeout(20)
            model.set_issue_ready(True)

        env.process(drop_once_inside_the_request())
        env.run(until=40)

        self.assertEqual(model.issued, [], "a command was popped while issue_ready was low")
        self.assertEqual(len(model.queues["port0"]["T0"]["SQ0"]), 1, "the queue entry did not remain pending")
        self.assertGreater(model.metrics["output_backpressure_cycles"], 0, "the stall went unrecorded")
        self.assertEqual(model.fsm_state["issue_pipeline"], "ISSUE_STALL")

        # The retry takes the declared path: ISSUE_REQUEST -> ISSUE_STALL on a
        # downstream that did not accept, a hold in ISSUE_STALL while
        # issue_ready is low, then ISSUE_STALL -> READ_PENDING_COUNT, re-reading
        # the count and burst rather than re-issuing against a stale reading.
        reached = []
        previous = None
        while env.peek() < 200:
            env.step()
            state = model.fsm_state["issue_pipeline"]
            if state != previous:
                reached.append(state)
                previous = state
            if model.issued:
                break
        self.assertEqual([command.cmd_id for _, command in model.issued], ["c0"], "the command never issued")
        self.assertEqual(
            reached[:4],
            ["ISSUE_STALL", "READ_PENDING_COUNT", "READ_BURST", "CALC_ISSUE_COUNT"],
            "the retry did not re-read the pending count and burst",
        )

    def test_arbitration_issue_slots_serialises_competing_requesters(self):
        """issue_slots is declared capacity 1, and must enforce it.

        Mirrors completion_ip's output port test. A capacity is only observable
        when something contends for it, so this drives a second requester of the
        declared port and asserts it waits; raising the capacity makes this test
        fail, which is the property a single-process timing assertion lacks.
        """
        env = simpy.Environment()
        model = ArbitrationIpModel(env, log_level="CRITICAL")
        holds = []

        def requester(name, hold):
            with model.issue_slots.request() as slot:
                yield slot
                holds.append((name, env.now))
                yield env.timeout(hold)

        env.process(requester("first", 5))
        env.process(requester("second", 5))
        env.run(until=100)

        self.assertEqual([name for name, _ in holds], ["first", "second"])
        self.assertEqual(holds[0][1], 0)
        self.assertEqual(holds[1][1], 5, "the second requester did not wait for the issue slot")

    def test_arbitration_issue_slot_latency_is_charged_off_the_critical_path(self):
        """issue_slots' declared latency_cycles: 1, which was charged nowhere.

        The declared new_command_to_issue path names eleven operations summing
        to exactly 38 and the port is not among them, so charging it inline
        would make the path 39. It is charged in the background instead, as
        ruled for completion_ip's output port: the command leaves at the end of
        the request, and the port stays busy one cycle longer. Both halves are
        pinned -- a port latency that changed nothing observable would be no
        better than the uncharged one it replaced.
        """
        env = simpy.Environment()
        model = ArbitrationIpModel(env, log_level="CRITICAL")

        def arrive():
            yield env.timeout(100)
            model.enqueue(Command("c", "READ", port_id="port0", tenant_id="T0", sq_id="SQ0"))

        env.process(arrive())
        busy = []

        def watcher():
            while True:
                busy.append((env.now, model.issue_slots.count))
                yield env.timeout(1)

        env.process(watcher())

        entered = {}
        previous = None
        while env.peek() < 400:
            env.step()
            state = model.fsm_state["arbiter_main"]
            if state != previous and env.now >= 100:
                entered.setdefault(state, env.now)
                previous = state
            if model.downstream_requests:
                break
        env.run(until=200)

        self.assertEqual(model.downstream_requests[0]["time"] - entered["IDLE"], 38, "the declared 38-cycle path moved")
        held = [now for now, count in busy if count]
        self.assertEqual(
            len(held),
            model.latency["issue"] + model.issue_slot_latency,
            "the issue slot was not busy for its own latency beyond the request",
        )

    def test_arbitration_idle_arbiter_waits_instead_of_scanning(self):
        """An idle arbiter costs nothing and records nothing.

        It used to spin IDLE -> PORT_SCAN -> STALL every 6 cycles with nothing
        queued, taking the declared edge with its condition `any_port_pending`
        false and paying a 4-cycle port scan to rediscover what the bitmap it had
        just refreshed already said. `stalls` and `no_port_pending_stalls` then
        measured elapsed idle time rather than arbitration events.

        The wake must also not be lost: a command arriving long after the arbiter
        has blocked is still served.
        """
        env = simpy.Environment()
        model = ArbitrationIpModel(env, log_level="CRITICAL")
        env.run(until=500)

        self.assertEqual(model.metrics["stalls"], 0, "an idle arbiter is still scanning")
        self.assertEqual(model.metrics["no_port_pending_stalls"], 0)
        self.assertEqual(model.fsm_state["arbiter_main"], "IDLE")

        model.enqueue(Command("late", "READ", port_id="port0", tenant_id="T0", sq_id="SQ0"))
        env.run(until=700)
        self.assertEqual(
            [command.cmd_id for _, command in model.issued], ["late"], "the wake-up after a long idle was lost"
        )

    def test_arbitration_blocked_tenant_does_not_starve_the_other_port(self):
        """A port whose tenants are all ineligible must not consume the arbiter.

        `_select_port` used to return the first *pending* port and the scan
        committed to it, so one credit-blocked tenant held the arbiter forever:
        measured at 400 cycles with zero grants while an eligible command sat on
        the other port, and nothing moved until credit was restored. The scan now
        walks the ports in policy order, because `eligible_port_found` cannot be
        decided until a port's tenants have been looked at.
        """
        env = simpy.Environment()
        model = ArbitrationIpModel(env, log_level="CRITICAL")
        model.set_tenant_credit("T0", read_iops_credit=0)
        model.enqueue(Command("blocked", "READ", port_id="port0", tenant_id="T0", sq_id="SQ0"))
        model.enqueue(Command("ready", "READ", port_id="port1", tenant_id="T2", sq_id="SQ0"))

        env.run(until=200)
        self.assertEqual(
            [command.cmd_id for _, command in model.issued],
            ["ready"],
            "the eligible command on the other port was starved by a blocked tenant",
        )

        model.set_tenant_credit("T0", read_iops_credit=1)
        env.run(until=400)
        self.assertEqual([command.cmd_id for _, command in model.issued], ["ready", "blocked"])

    def test_arbitration_stalls_when_the_last_port_drains_mid_scan(self):
        """PORT_SCAN -> STALL on no_eligible_port, which now needs a real race.

        While the arbiter polled, this transition fired on every idle pass and
        was trivially covered. Now that an idle arbiter blocks in IDLE until a
        port is pending, PORT_SCAN is entered only when one is -- so the declared
        `no_eligible_port` condition is reachable in exactly one way: the last
        pending command leaves during the four cycles the scan takes. That is a
        real case (the issue pipeline drains it), and without this test the fix
        for M23 would quietly make a declared transition unreachable, which is
        what rounds one and two were about.
        """
        env = simpy.Environment()
        model = ArbitrationIpModel(env, log_level="CRITICAL")
        model.enqueue(Command("c0", "READ", port_id="port0", tenant_id="T0", sq_id="SQ0"))

        def drain_during_the_scan():
            while model.fsm_state["arbiter_main"] != "PORT_SCAN":
                yield env.timeout(1)
            # the command is issued while the scan is in flight
            model.queues["port0"]["T0"]["SQ0"].clear()
            model._update_pending_bitmaps()

        env.process(drain_during_the_scan())
        env.run(until=60)

        self.assertEqual(model.metrics["no_port_pending_stalls"], 1, "the declared no_eligible_port stall never fired")
        self.assertEqual(model.metrics["grants"], 0)

    def _sq_selection_sequence(self, sq_weights, until=400):
        """Selections at the SQ level, under load that keeps all four SQs pending.

        A single command per SQ is not enough: one selection issues everything
        pending in that SQ, so four SQs drain in four selections and no weighting
        is observable. The feeder keeps each SQ topped up, which is the sustained
        load the declared SQ weights describe.
        """
        env = simpy.Environment()
        topology = {"port0": {"tenants": {"T1": ["SQ0", "SQ1", "SQ2", "SQ3"]}}}
        model = ArbitrationIpModel(
            env,
            topology=topology,
            weights={"ports": {"port0": 1}, "tenants": {"T1": 1}, "sqs": sq_weights},
            log_level="CRITICAL",
        )

        def feeder():
            index = 0
            while True:
                for sq_id in ("SQ0", "SQ1", "SQ2", "SQ3"):
                    if not model.queues["port0"]["T1"][sq_id]:
                        model.enqueue(
                            Command(f"c{index}_{sq_id}", "READ", port_id="port0", tenant_id="T1", sq_id=sq_id)
                        )
                        index += 1
                yield env.timeout(1)

        env.process(feeder())
        env.run(until=until)
        return [entry["sq_id"] for entry in model.selection_trace]

    def test_arbitration_weighted_order_at_the_sq_level(self):
        """The SQ level of the declared [port, tenant, sq] hierarchy.

        Every weighting test projected the selection trace onto tenant_id and
        discarded sq_id, so one of the three declared levels had no test that
        could fail for it. Neutering the SQ pointer advance left the whole suite
        green while starving SQ1..SQ3 whenever SQ0 stayed pending -- verified by
        mutation, before and after.
        """
        weighted = self._sq_selection_sequence({"SQ0": 4, "SQ1": 2, "SQ2": 1, "SQ3": 1})
        equal = self._sq_selection_sequence({"SQ0": 1, "SQ1": 1, "SQ2": 1, "SQ3": 1})
        inverted = self._sq_selection_sequence({"SQ0": 1, "SQ1": 1, "SQ2": 2, "SQ3": 4})

        self.assertEqual(
            weighted[:8], ["SQ0", "SQ0", "SQ0", "SQ0", "SQ1", "SQ1", "SQ2", "SQ3"], "SQ weights were not respected"
        )
        self.assertEqual(equal[:8], ["SQ0", "SQ1", "SQ2", "SQ3", "SQ0", "SQ1", "SQ2", "SQ3"])
        self.assertEqual(inverted[:8], ["SQ0", "SQ1", "SQ2", "SQ2", "SQ3", "SQ3", "SQ3", "SQ3"])
        self.assertNotEqual(weighted, equal, "SQ weighting made no difference to the order")
        self.assertNotEqual(weighted, inverted, "inverting the SQ weights made no difference")

    def test_arbitration_scan_latency_does_not_silently_zero_the_others(self):
        """scan_latency sets the three scans and nothing else.

        It used to set port_scan to the value and then zero tenant_scan, sq_scan
        and grant, so reaching for it as a convenience opted out of most of the
        declared timing model without saying so.
        """
        env = simpy.Environment()
        model = ArbitrationIpModel(env, scan_latency=5, log_level="CRITICAL")
        self.assertEqual(model.latency["port_scan"], 5)
        self.assertEqual(model.latency["tenant_scan"], 5)
        self.assertEqual(model.latency["sq_scan"], 5)
        self.assertEqual(model.latency["grant"], 2, "grant is not a scan and must keep its default")

    def test_arbitration_qos_credit_gates_the_grant(self):
        """qos_credit_if's sampled eligibility must decide whether a candidate is granted.

        The interface declares wait_for_response at arbiter_main.TENANT_SCAN with
        "the eligibility result decides whether the candidate may be granted",
        and the model had no credit or liveness state at all -- so nothing was
        sampled and the grant could not depend on it. Check-only by declaration:
        arbitration.eligibility_owner is arbitration_ip_check_only, so this IP
        reads the credit and never debits it.
        """
        env = simpy.Environment()
        model = ArbitrationIpModel(
            env,
            weights={"ports": {"port0": 1}, "tenants": {"T0": 1, "T1": 1}, "sqs": {}},
            scan_latency=1,
            issue_latency=1,
            port_mode="single",
            pending_count_latency=1,
            burst_read_latency=1,
            burst_calc_latency=1,
            burst_debit_latency=1,
            log_level="CRITICAL",
        )
        model.set_tenant_credit("T0", read_iops_credit=False)
        model.enqueue(Command("t0", "READ", port_id="port0", tenant_id="T0", sq_id="SQ0"))
        model.enqueue(Command("t1", "READ", port_id="port0", tenant_id="T1", sq_id="SQ0"))
        env.run(until=200)

        selected = [entry["tenant_id"] for entry in model.selection_trace]
        self.assertEqual(selected, ["T1"], "an ineligible tenant was granted")
        self.assertGreater(model.metrics["eligibility_rejections"], 0)
        self.assertEqual(len(model.queues["port0"]["T0"]["SQ0"]), 1, "its command must still be queued")

        # Restoring credit makes it selectable again, and nothing was debited.
        model.set_tenant_credit("T0", read_iops_credit=True)
        env.run(until=400)
        self.assertIn("T0", [entry["tenant_id"] for entry in model.selection_trace])
        self.assertTrue(all(model.tenant_credit["T1"].values()), "the check must not debit credit")

    def test_arbitration_credit_status_rejects_undeclared_fields(self):
        """Only the five fields qos_credit_if's eligibility_check declares."""
        env = simpy.Environment()
        model = ArbitrationIpModel(env, log_level="CRITICAL")
        with self.assertRaises(ValueError):
            model.set_tenant_credit("T0", made_up_credit=False)

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

        # The three scan stages each cost scan_latency now, where the parameter
        # used to zero two of them and grant, and arbiter_main holds RESET and
        # IDLE for real cycles, so the pipeline reaches its first backpressure
        # cycle at t=13 rather than immediately.
        env.run(until=14)

        self.assertEqual(model.downstream_requests, [])
        self.assertEqual(len(model.queues["port0"]["T0"]["SQ0"]), 1)
        self.assertGreater(model.metrics["output_backpressure_cycles"], 0)
        self.assertEqual(model.fsm_state["issue_pipeline"], "ISSUE_STALL")

        model.set_issue_ready(True)
        env.run(until=30)

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
