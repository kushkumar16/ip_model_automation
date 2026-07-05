import unittest

import simpy

from ip_model_automation.ip import MailboxIrqSubsystemModel


FAST_CONTROLLER = {
    "sample_latency": 1,
    "pending_latency": 1,
    "filter_latency": 1,
    "priority_latency": 1,
    "delivery_latency": 1,
    "ack_latency": 1,
    "eoi_latency": 1,
    "register_latency": 1,
    "software_latency": 1,
}


def make_subsystem(env, **overrides):
    kwargs = {"controller_kwargs": dict(FAST_CONTROLLER)}
    for key, value in overrides.items():
        if key.endswith("_kwargs"):
            kwargs.setdefault(key, {}).update(value)
        else:
            kwargs[key] = value
    return MailboxIrqSubsystemModel(env, **kwargs)


class TestMailboxIrqSubsystemModel(unittest.TestCase):
    def test_single_message_round_trip(self):
        env = simpy.Environment()
        subsystem = make_subsystem(env)
        subsystem.configure_channel(0)
        subsystem.send(0, "hello")
        env.run(until=200)
        self.assertEqual(subsystem.metrics["messages_sent"], 1)
        self.assertEqual(subsystem.metrics["irqs_bridged"], 1)
        self.assertGreaterEqual(subsystem.metrics["deliveries_observed"], 1)
        self.assertGreaterEqual(subsystem.metrics["acks_issued"], 1)
        self.assertEqual(subsystem.metrics["messages_serviced"], 1)
        self.assertEqual(subsystem.metrics["eois_issued"], 1)
        self.assertGreater(subsystem.metrics["round_trip_latency"], 0)
        self.assertEqual(subsystem.serviced[0][2], "hello")
        self.assertNotIn(0, self.doorbells(subsystem))

    @staticmethod
    def doorbells(subsystem):
        return set(subsystem.mailbox.doorbell_status)

    def test_multi_message_level_reassert(self):
        env = simpy.Environment()
        subsystem = make_subsystem(env)
        subsystem.configure_channel(0)
        for index in range(3):
            subsystem.send(0, f"m{index}")
        env.run(until=600)
        self.assertEqual(subsystem.metrics["messages_serviced"], 3)
        self.assertEqual(subsystem.metrics["eois_issued"], 3)
        self.assertGreaterEqual(subsystem.metrics["level_reasserts_used"], 1)
        self.assertGreaterEqual(subsystem.controller.metrics["reasserted_level_irqs"], 1)
        self.assertEqual(subsystem.outstanding[0], 0)

    def test_storm_throttle_masks_flooding_channel(self):
        env = simpy.Environment()
        subsystem = make_subsystem(env, storm_limit=2)
        subsystem.configure_channel(0)
        for index in range(4):
            subsystem.send(0, f"m{index}")
        env.run(until=1500)
        self.assertGreaterEqual(subsystem.metrics["storm_throttle_events"], 1)
        self.assertGreaterEqual(subsystem.metrics["storm_mask_releases"], 1)
        self.assertEqual(subsystem.metrics["messages_serviced"], 4)
        self.assertEqual(subsystem.outstanding[0], 0)
        self.assertNotIn(0, subsystem.controller.masked)

    def test_masked_channel_is_isolated(self):
        env = simpy.Environment()
        subsystem = make_subsystem(env)
        subsystem.configure_channel(0, masked=True)
        subsystem.configure_channel(1)
        subsystem.send(0, "blocked")
        subsystem.send(1, "allowed")
        env.run(until=300)
        self.assertGreaterEqual(subsystem.mailbox.metrics["masked_doorbells"], 1)
        self.assertEqual(subsystem.metrics["messages_serviced"], 1)
        self.assertEqual(subsystem.serviced[0][1], 1)
        self.assertEqual(subsystem.serviced[0][2], "allowed")
        delivered_sources = {src for _t, src in subsystem.controller.delivered}
        self.assertNotIn(0, delivered_sources)


if __name__ == "__main__":
    unittest.main()
