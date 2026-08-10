import unittest

import simpy

from ip_model_automation.ip import TimerIpModel


class TestTimerIpModel(unittest.TestCase):
    def test_timer_emits_interrupt(self):
        env = simpy.Environment()
        model = TimerIpModel(env, tick_period=1, interrupt_latency=1)
        model.configure(0, "ONE_SHOT", compare=2)
        env.run(until=5)
        self.assertEqual(model.metrics["interrupt_count"], 1)
        self.assertFalse(model.channels[0]["enabled"])

    def test_timer_periodic_reloads_and_repeats(self):
        env = simpy.Environment()
        model = TimerIpModel(env, tick_period=1, interrupt_latency=1)
        model.configure(0, "PERIODIC", compare=2, reload=0)
        env.run(until=8)
        self.assertGreaterEqual(model.metrics["compare_events"], 3)
        self.assertTrue(model.channels[0]["enabled"])

        # interrupt_if is wait_for_ack_before_next_request: counting continues,
        # but the level IRQ holds at one until software clears it.
        self.assertEqual(model.metrics["interrupt_count"], 1)
        self.assertEqual(model.fsm_state["interrupt_aggregation"], "WAIT_SW_CLEAR")
        model.clear_interrupt(0)
        env.run(until=16)
        self.assertGreaterEqual(model.metrics["interrupt_count"], 2)

    def test_timer_config_sync_delays_counter_visibility(self):
        env = simpy.Environment()
        model = TimerIpModel(env, tick_period=1, sync_latency=3, interrupt_latency=1)
        model.configure(0, "ONE_SHOT", compare=1)
        env.run(until=3)
        self.assertNotIn(0, model.channels)
        env.run(until=7)
        self.assertIn(0, model.channels)
        self.assertEqual(model.metrics["interrupt_count"], 1)

    def test_timer_watchdog_timeout_generates_interrupt(self):
        env = simpy.Environment()
        model = TimerIpModel(env, tick_period=1, interrupt_latency=1, watchdog_latency=1)
        model.configure_watchdog(timeout=3)
        env.run(until=8)
        self.assertGreaterEqual(model.metrics["watchdog_timeouts"], 1)
        self.assertIn(-1, [channel_id for _, channel_id in model.interrupts])

    def test_timer_register_read_returns_a_response(self):
        """register_if: wait_for_response — the caller blocks until the read returns."""
        env = simpy.Environment()
        model = TimerIpModel(env, tick_period=1, register_latency=2, interrupt_latency=1)
        model.configure(0, "PERIODIC", compare=4, reload=0)
        seen = {}

        def reader():
            yield env.timeout(6)
            started = env.now
            seen["state"] = yield model.read_counter(0)
            seen["elapsed"] = env.now - started

        env.process(reader())
        env.run(until=20)
        self.assertTrue(seen["state"]["enabled"])
        self.assertGreaterEqual(seen["state"]["count"], 0)
        self.assertGreaterEqual(seen["elapsed"], 2)

    def test_timer_clear_interrupt_drops_status_bit(self):
        env = simpy.Environment()
        model = TimerIpModel(env, tick_period=1, interrupt_latency=1)
        model.configure(0, "ONE_SHOT", compare=2)
        env.run(until=5)
        self.assertIn(0, model.interrupt_status)
        model.clear_interrupt(0)
        self.assertNotIn(0, model.interrupt_status)

    def test_timer_watchdog_kick_defers_timeout(self):
        env = simpy.Environment()
        model = TimerIpModel(env, tick_period=1, interrupt_latency=1, watchdog_latency=1)
        model.configure_watchdog(timeout=4)
        env.run(until=3)
        model.kick_watchdog()
        env.run(until=7)
        self.assertEqual(model.metrics["watchdog_kicks"], 1)
        self.assertEqual(model.metrics["watchdog_timeouts"], 0)
        env.run(until=12)
        self.assertGreaterEqual(model.metrics["watchdog_timeouts"], 1)

    def test_timer_masked_interrupt_is_counted_not_asserted(self):
        env = simpy.Environment()
        model = TimerIpModel(env, tick_period=1, interrupt_latency=1)
        model.interrupt_mask[0] = False
        model.configure(0, "ONE_SHOT", compare=2)
        env.run(until=6)
        self.assertGreaterEqual(model.metrics["masked_events"], 1)
        self.assertEqual(model.interrupts, [])
        self.assertIn(0, model.interrupt_status)

    def test_timer_non_configure_register_write_applies_nothing(self):
        env = simpy.Environment()
        model = TimerIpModel(env, tick_period=1)
        model.write_register({"op": "status_read"})
        env.run(until=3)
        self.assertEqual(model.metrics["register_writes"], 1)
        self.assertEqual(model.metrics["config_applied"], 0)
        self.assertEqual(model.channels, {})

    def test_timer_tick_can_be_driven_from_outside(self):
        """tick_if is an input interface, so a peer must be able to drive it.

        The template declares `direction: input`, `requester: peer`, and the
        prescaler waiting in WAIT_RAW_TICK for raw_tick_observed. The model used
        to manufacture ticks internally from a constructor argument, so the
        declared interface had no way in and nothing composing this IP could
        supply its clock -- while check_wait_model_coverage still passed, because
        the FSM does enter WAIT_RAW_TICK. It checks the wait point is reached,
        not what is being waited for.
        """
        env = simpy.Environment()
        model = TimerIpModel(env, tick_period=None, interrupt_latency=1)
        model.configure(0, "PERIODIC", compare=2, reload=0)
        env.run(until=5)

        # With no internal driver and no peer, the counter cannot move at all,
        # and the prescaler is parked in its declared wait point.
        self.assertEqual(model.metrics["effective_ticks"], 0)
        self.assertEqual(model.channels[0]["count"], 0)
        self.assertEqual(model.fsm_state["prescaler"], "WAIT_RAW_TICK")

        def peer():
            for _ in range(4):
                yield model.present_raw_tick()
                yield env.timeout(1)

        env.process(peer())
        env.run(until=20)
        self.assertEqual(model.metrics["external_raw_ticks"], 4)
        self.assertEqual(model.metrics["internal_raw_ticks"], 0)
        self.assertEqual(model.metrics["effective_ticks"], 4)
        # Four peer ticks reach the counter, and two of them complete the
        # compare=2 period. The count itself is back at its reload value, which
        # is why it is the wrong thing to assert on here.
        self.assertEqual(model.metrics["counter_updates"], 4)
        self.assertEqual(model.metrics["compare_events"], 2)

    def test_timer_tick_functional_respects_prescaler(self):
        env = simpy.Environment()
        model = TimerIpModel(env, tick_period=1000, interrupt_latency=1)
        model.configure(0, "ONE_SHOT", compare=10, prescale=4)
        env.run(until=5)
        for _ in range(3):
            model.tick_functional()
        self.assertEqual(model.metrics["effective_ticks"], 0)
        model.tick_functional()
        self.assertEqual(model.metrics["effective_ticks"], 1)

    def test_timer_counter_ignores_tick_for_unknown_channel(self):
        env = simpy.Environment()
        model = TimerIpModel(env, tick_period=1000)
        model.effective_tick_queue.put(99)
        env.run(until=5)
        self.assertEqual(model.metrics["counter_updates"], 0)

    def test_timer_debug_freeze_pays_both_declared_handshake_steps(self):
        """Freezing and resuming are each two acknowledged steps, not one.

        The DLD says so -- "Freeze request must be acknowledged before counters
        stop" -- and the template prices both halves at 2 cycles each, so
        reaching FROZEN costs 4 and returning to RUN costs 4. The model used to
        pay once per direction, halving both, and the existing freeze test could
        not see it: it asserts that the counter holds and resumes, never what
        that cost.
        """
        env = simpy.Environment()
        model = TimerIpModel(env, tick_period=1, interrupt_latency=1, debug_freeze_latency=2)
        model.configure(0, "PERIODIC", compare=3, reload=0)
        env.run(until=10)

        model.set_debug_freeze(True)
        requested = env.now
        while not model.frozen and env.peek() < 60:
            env.step()
        # At least the declared 4. The upper bound is 4 + 1: debug_freeze polls
        # on a one-unit loop rather than waiting on an event, so it can notice
        # the request up to a unit late. That slack is a separate modelling
        # artifact, not latitude in the declared cost.
        self.assertGreaterEqual(env.now - requested, 4, "freeze paid less than the declared 2 + 2 cycles")
        self.assertLessEqual(env.now - requested, 5)
        self.assertEqual(model.fsm_state["debug_freeze"], "FROZEN")

        model.set_debug_freeze(False)
        requested = env.now
        while model.frozen and env.peek() < 120:
            env.step()
        self.assertGreaterEqual(env.now - requested, 4, "resume paid less than the declared 2 + 2 cycles")
        self.assertLessEqual(env.now - requested, 5)
        self.assertEqual(model.fsm_state["debug_freeze"], "RUN")

    def test_timer_debug_freeze_holds_and_resumes_counter(self):
        env = simpy.Environment()
        model = TimerIpModel(env, tick_period=1, interrupt_latency=1, debug_freeze_latency=1)
        model.configure(0, "PERIODIC", compare=3, reload=0)
        env.run(until=2)
        model.set_debug_freeze(True)
        env.run(until=6)
        frozen_count = model.channels[0]["count"]
        env.run(until=9)
        self.assertEqual(model.channels[0]["count"], frozen_count)
        self.assertGreater(model.metrics["debug_frozen_ticks"], 0)
        updates_before_resume = model.metrics["counter_updates"]
        model.set_debug_freeze(False)
        env.run(until=13)
        self.assertGreater(model.metrics["counter_updates"], updates_before_resume)
        self.assertEqual(model.metrics["debug_resumes"], 1)


if __name__ == "__main__":
    unittest.main()
