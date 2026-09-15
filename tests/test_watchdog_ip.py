import unittest

import simpy

from ip_model_automation.watchdog_ip import WatchdogIpModel

FAST = {
    "reset_latency": 1,
    "arm_latency": 1,
    "kick_latency": 1,
    "disarm_latency": 1,
    "countdown_tick_latency": 1,
    "expire_latency": 1,
    "accept_config_latency": 1,
    "apply_timeout_latency": 1,
}


def make_model(env, **overrides):
    kwargs = dict(FAST, log_level="CRITICAL")
    kwargs.update(overrides)
    return WatchdogIpModel(env, **kwargs)


class TestWatchdogIpModel(unittest.TestCase):
    def test_kick_before_expiry_prevents_expiration(self):
        """template test_scenarios[0]: kick_before_expiry_prevents_expiration.

        Sustained kicking, one per cycle, against a 5-cycle timeout must never
        let the countdown reach zero. This is also the regression test for the
        SimPy Store.get() request-leak this dogfood round found: racing a fresh
        get() against a timeout every loop iteration without keeping the losing
        request alive let a later put() (kick()) be absorbed FIFO by an earlier
        abandoned getter instead of the live one, so kicks_received silently
        undercounted and the watchdog could still expire despite being kicked.
        """
        env = simpy.Environment()
        model = make_model(env, default_timeout_cycles=5)

        def driver():
            yield model.arm()
            for _ in range(20):
                yield env.timeout(1)
                yield model.kick()

        env.process(driver())
        env.run(until=25)

        self.assertEqual(model.get_metrics()["kicks_received"], 20)
        self.assertEqual(model.get_metrics()["expirations"], 0)
        self.assertEqual(model.fsm_state["watchdog_main"], "ARMED")

    def test_countdown_reaches_zero_without_kick_expires(self):
        """template test_scenarios[1]: countdown_reaches_zero_without_kick_expires.

        Also the regression test for the second leak this dogfood round found:
        when the countdown expires, _run_armed()'s still-live, never-consumed
        get() request was abandoned without being cancelled, so it lingered on
        the Store ahead of _run_expired()'s own fresh get() -- a later disarm()
        was then absorbed by that stale request instead of clearing the latch.
        """
        env = simpy.Environment()
        model = make_model(env, default_timeout_cycles=5)

        def driver():
            yield model.arm()
            yield env.timeout(20)  # never kicked -- countdown must reach zero
            yield model.disarm()

        env.process(driver())
        env.run(until=30)

        self.assertEqual(model.get_metrics()["expirations"], 1)
        self.assertEqual(model.get_metrics()["disarm_count"], 1)
        self.assertEqual(model.fsm_state["watchdog_main"], "DISARMED", "disarm did not clear the expired latch")

    def test_expired_latch_blocks_rearm_until_disarmed(self):
        """EXPIRED -> DISARMED is the only declared exit; ARM/KICK while expired
        have no declared transition and must have no effect (DLD 6.1)."""
        env = simpy.Environment()
        model = make_model(env, default_timeout_cycles=3)

        def driver():
            yield model.arm()
            yield env.timeout(10)  # expires
            yield model.arm()  # ignored while EXPIRED
            yield model.kick()  # ignored while EXPIRED

        env.process(driver())
        env.run(until=15)

        self.assertEqual(model.fsm_state["watchdog_main"], "EXPIRED")
        self.assertEqual(model.get_metrics()["expirations"], 1)
        self.assertEqual(model.get_metrics()["arm_count"], 1, "the ignored re-arm while EXPIRED must not count")
        self.assertEqual(model.get_metrics()["kicks_received"], 0, "the ignored kick while EXPIRED must not count")

    def test_kick_and_disarm_have_no_effect_while_disarmed(self):
        """No declared transition from DISARMED on KICK/DISARM (DLD 6.1): there
        is nothing to kick or cancel yet, so both are accepted with no effect."""
        env = simpy.Environment()
        model = make_model(env)

        def driver():
            yield model.kick()
            yield model.disarm()

        env.process(driver())
        env.run(until=5)

        self.assertEqual(model.fsm_state["watchdog_main"], "DISARMED")
        self.assertEqual(model.get_metrics()["kicks_received"], 0)
        self.assertEqual(model.get_metrics()["disarm_count"], 0)

    def test_arm_has_no_effect_while_already_armed(self):
        """ARMED -> ARMED has no declared transition on ARM (only on KICK); a
        redundant ARM while already armed must not reload the countdown or
        count as a second arm (DLD 6.1)."""
        env = simpy.Environment()
        model = make_model(env, default_timeout_cycles=5)
        captured = []

        def driver():
            yield model.arm()
            captured.append(model.countdown_cycles)  # sampled right at arm, before any tick decrements it
            yield env.timeout(2)  # let a couple of ticks decrement the countdown below 5
            yield model.arm()  # redundant, ignored -- must not reload the countdown back to 5
            captured.append(model.countdown_cycles)
            yield model.disarm()

        env.process(driver())
        env.run(until=10)

        self.assertEqual(model.fsm_state["watchdog_main"], "DISARMED")
        self.assertEqual(model.get_metrics()["arm_count"], 1, "the redundant ARM must not count as a second arm")
        self.assertEqual(captured[0], 5)
        self.assertLess(captured[1], 5, "the redundant ARM must not reload the countdown")

    def test_configure_timeout_flows_independently_of_countdown(self):
        """template test_scenarios[2]: configure_timeout_flows_independently_of_countdown.

        A CONFIGURE_TIMEOUT applied while ARMED must not disturb the running
        countdown -- it only takes effect the next time the watchdog arms.
        """
        env = simpy.Environment()
        model = make_model(env, default_timeout_cycles=5)

        def driver():
            yield model.arm()
            yield env.timeout(2)
            yield model.configure_timeout(8)
            # the running countdown (5, now at 3 after 2 ticks) is unaffected:
            # 3 more cycles brings it to zero and expiry, not 8.
            yield env.timeout(10)

        env.process(driver())
        env.run(until=20)

        self.assertEqual(model.fsm_state["config_intake"], "IDLE")
        self.assertEqual(model.configured_timeout_cycles, 8)
        self.assertEqual(
            model.get_metrics()["expirations"], 1, "the stored config must not have reset the running countdown"
        )

        # the stored value does take effect on the *next* arm
        env2 = simpy.Environment()
        model2 = make_model(env2, default_timeout_cycles=5)

        captured = []

        def driver2():
            yield model2.configure_timeout(2)
            yield model2.arm()
            # sampled the instant ARM takes effect, before the next countdown
            # tick can decrement it
            captured.append(model2.countdown_cycles)

        env2.process(driver2())
        env2.run(until=10)
        self.assertEqual(captured, [2], "the newly configured timeout must load on arm")

    def test_reset_clears_state_before_first_command(self):
        env = simpy.Environment()
        model = make_model(env)
        self.assertEqual(model.fsm_state["watchdog_main"], "RESET")
        env.run(until=2)
        self.assertEqual(model.fsm_state["watchdog_main"], "DISARMED")

    def test_multiple_arm_disarm_cycles_leave_no_stale_getters(self):
        """Regression test for the request-leak bug class: repeated arm/kick/
        disarm and arm/expire/disarm cycles must never let a stale pending
        getter accumulate on the host command Store."""
        env = simpy.Environment()
        model = make_model(env, default_timeout_cycles=4)

        def driver():
            yield model.arm()
            yield env.timeout(1)
            yield model.kick()
            yield env.timeout(1)
            yield model.disarm()

            yield model.arm()
            yield env.timeout(10)  # expires
            yield model.disarm()

            yield model.arm()
            for _ in range(6):
                yield env.timeout(1)
                yield model.kick()
            yield model.disarm()

        env.process(driver())
        env.run(until=60)

        self.assertEqual(model.fsm_state["watchdog_main"], "DISARMED")
        self.assertEqual(model.get_metrics()["arm_count"], 3)
        self.assertEqual(model.get_metrics()["disarm_count"], 3)
        self.assertEqual(model.get_metrics()["expirations"], 1)
        self.assertEqual(model.get_metrics()["kicks_received"], 7)
        self.assertEqual(
            len(model._host_q.get_queue), 1, "exactly one live getter should remain -- the blocked DISARMED wait"
        )


if __name__ == "__main__":
    unittest.main()
