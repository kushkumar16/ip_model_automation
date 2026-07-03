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
        self.assertGreaterEqual(model.metrics["interrupt_count"], 2)
        self.assertTrue(model.channels[0]["enabled"])

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
