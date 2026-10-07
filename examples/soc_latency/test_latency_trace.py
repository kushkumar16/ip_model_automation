"""Edge cases for latency_trace.py. Run: cd examples/soc_latency && python -m unittest test_latency_trace"""

import logging
import unittest

import simpy

from latency_trace import (
    MonitoredEnvironment,
    TracedEnvironment,
    TracedResource,
    TracedStore,
    fsm_idle,
    latency_traced,
)

logging.getLogger("latency").setLevel(logging.CRITICAL)


class TracedEnvironmentTest(unittest.TestCase):
    def test_per_job_span_and_return_value(self):
        env = TracedEnvironment()

        def job(n):
            yield env.timeout(n)
            return n * 10

        p = env.process(job(3))
        env.run()
        self.assertEqual(p.value, 30)
        (name,) = env.monitor.samples  # names=None: traced under the bare __qualname__
        self.assertTrue(name.endswith("<locals>.job"))
        self.assertEqual(env.monitor.samples[name], [3])

    def test_interrupt_reaches_the_process(self):
        env = TracedEnvironment(names={"worker": "w"})
        seen = []

        def worker():
            try:
                yield env.timeout(10)
            except simpy.Interrupt as i:
                seen.append((env.now, i.cause))
            yield env.timeout(2)

        def interrupter(p):
            yield env.timeout(4)
            p.interrupt("stop")

        worker.__qualname__ = "worker"
        p = env.process(worker())
        env.process(interrupter(p))
        env.run()
        self.assertEqual(seen, [(4, "stop")])
        self.assertEqual(env.monitor.samples["w"], [6])

    def test_failed_event_is_thrown_into_the_process_and_uncaught_errors_propagate(self):
        env = TracedEnvironment(names={"job": "job"})

        def job():
            ev = env.event()
            ev.fail(ValueError("boom"))
            try:
                yield ev
            except ValueError:
                pass
            yield env.timeout(1)
            raise KeyError("unhandled")

        job.__qualname__ = "job"
        env.process(job())
        with self.assertRaises(KeyError):
            env.run()
        self.assertEqual(env.monitor.samples["job"], [1])  # failed job still recorded

    def test_work_loop_records_one_span_per_iteration_excluding_idle(self):
        env = TracedEnvironment(names={"loop": "loop"})
        q = simpy.Store(env)

        def loop():
            yield env.timeout(100)  # setup before the first get(): not counted
            while True:
                item = yield q.get()
                yield env.timeout(item)

        def feed():
            yield env.timeout(200)
            for d in (2, 5):
                yield q.put(d)
                yield env.timeout(50)  # idle gap between items: not counted

        loop.__qualname__ = "loop"
        env.process(loop())
        env.process(feed())
        env.run(until=1000)
        self.assertEqual(env.monitor.samples["loop"], [2, 5])

    def test_names_format_from_arguments_and_unlisted_are_skipped(self):
        env = TracedEnvironment(names={"Blk.run": "{self.name}.{pname}"})

        class Blk:
            name = "dma"

            def run(self, pname):
                yield env.timeout(1)

        Blk.run.__qualname__ = "Blk.run"

        def other():
            yield env.timeout(1)

        env.process(Blk().run("read"))
        env.process(other())
        env.run()
        self.assertEqual(env.monitor.samples, {"dma.read": [1]})


class DecoratorAndQueuesTest(unittest.TestCase):
    def test_decorator(self):
        env = MonitoredEnvironment()

        class Blk:
            def __init__(self):
                self.env, self.name = env, "codec"

            @latency_traced("{self.name}.{pname}")
            def run(self, pname, d):
                yield self.env.timeout(d)
                return "done"

        p = env.process(Blk().run("encode", 4))
        env.run()
        self.assertEqual(p.value, "done")
        self.assertEqual(env.monitor.samples["codec.encode"], [4])

    def test_store_wait_and_resource_wait(self):
        env = MonitoredEnvironment()
        q = TracedStore(env, name="q")
        r = TracedResource(env, 1, name="r")

        def producer():
            yield q.put("a")  # t=0
            yield env.timeout(1)
            yield q.put("b")  # t=1

        def consumer():
            yield env.timeout(5)
            yield q.get()  # a waited 5
            yield q.get()  # b waited 4

        def user(hold):
            with r.request() as req:
                yield req
                yield env.timeout(hold)

        env.process(producer())
        env.process(consumer())
        env.process(user(3))
        env.process(user(3))
        env.run()
        self.assertEqual(env.monitor.samples["q.wait"], [5, 4])
        self.assertEqual(env.monitor.samples["r.wait"], [0, 3])


class LoopTest(unittest.TestCase):
    """latency_traced (A/E) on long-lived `while True` loops."""

    def test_store_loop_one_sample_per_item_idle_excluded(self):
        env = MonitoredEnvironment()

        class Worker:
            def __init__(self):
                self.env, self.name, self.q = env, "w", simpy.Store(env)

            @latency_traced("{self.name}")
            def run(self):
                yield self.env.timeout(100)  # setup: not counted
                while True:
                    txn = yield self.q.get()
                    yield self.env.timeout(txn)

        w = Worker()
        env.process(w.run())

        def feed():
            yield env.timeout(200)
            for d in (2, 5):
                yield w.q.put(d)
                yield env.timeout(50)  # idle gap: not counted

        env.process(feed())
        env.run(until=1000)
        self.assertEqual(env.monitor.samples["w"], [2, 5])

    def test_wrapping_a_bound_method_at_registration(self):
        env = MonitoredEnvironment()

        class Worker:
            def __init__(self):
                self.env, self.name, self.q = env, "w", simpy.Store(env)
                env.process(latency_traced("{self.name}.{lane}")(self.run)("lane0"))

            def run(self, lane):
                while True:
                    txn = yield self.q.get()
                    yield self.env.timeout(txn)

        w = Worker()
        w.q.put(3)
        env.run(until=50)
        self.assertEqual(env.monitor.samples["w.lane0"], [3])

    def test_job_mode_never_reports_a_loop(self):
        env = MonitoredEnvironment()
        q = simpy.Store(env)

        @latency_traced("loop", mode="job")
        def loop(env):
            while True:
                txn = yield q.get()
                yield env.timeout(txn)

        env.process(loop(env))
        q.put(3)
        env.run(until=50)
        self.assertEqual(env.monitor.samples, {})

    def test_name_from_the_current_item(self):
        env = MonitoredEnvironment()
        q = simpy.Store(env)

        @latency_traced("loop.{txn[0]}")
        def loop(env):
            while True:
                txn = yield q.get()
                yield env.timeout(txn[1])

        env.process(loop(env))
        for item in (("rd", 2), ("wr", 4), ("rd", 1)):
            q.put(item)
        env.run(until=50)
        self.assertEqual(env.monitor.samples, {"loop.rd": [2, 1], "loop.wr": [4]})

    def test_get_or_timeout_condition_is_a_boundary(self):
        env = MonitoredEnvironment()
        q = simpy.Store(env)

        @latency_traced("loop")
        def loop(env):
            get = q.get()
            while True:
                result = yield get | env.timeout(10)
                if get in result:
                    yield env.timeout(result[get])
                    get = q.get()

        env.process(loop(env))

        def feed():
            yield env.timeout(25)
            yield q.put(3)

        env.process(feed())
        env.run(until=100)
        self.assertEqual(env.monitor.samples["loop"], [0, 0, 3] + [0] * 7)


class FsmBoundaryTest(unittest.TestCase):
    def test_wake_event_loop_that_falls_through(self):
        """Two items queued together: the loop never waits between them, so only the FSM
        can tell where the first ends. A yield-based boundary would merge them."""
        env = MonitoredEnvironment()

        class Sched:
            def __init__(self):
                self.env, self.name, self.fsm_state, self.pending, self.wake = env, "s", {}, [], None

            def submit(self, d):
                self.pending.append(d)
                if self.wake is not None and not self.wake.triggered:
                    self.wake.succeed()

            @latency_traced("{self.name}", boundary=fsm_idle("main", "IDLE"))
            def run(self):
                self.fsm_state["main"] = "RESET"
                yield self.env.timeout(5)
                while True:
                    self.fsm_state["main"] = "IDLE"
                    if not self.pending:
                        self.wake = self.env.event()
                        yield self.wake
                    txn = self.pending.pop(0)
                    self.fsm_state["main"] = "SERVE"
                    yield self.env.timeout(txn)

        s = Sched()
        env.process(s.run())

        def feed():
            yield env.timeout(20)
            s.submit(3)
            s.submit(4)

        env.process(feed())
        env.run(until=100)
        self.assertEqual(env.monitor.samples["s"], [3, 4])  # RESET -> IDLE is not a sample

    def test_polling_loop_idle_polls_not_counted(self):
        env = MonitoredEnvironment()

        class Bridge:
            def __init__(self):
                self.env, self.name, self.fsm_state, self.mailbox = env, "b", {}, []

            @latency_traced("{self.name}", boundary=fsm_idle("bridge", "POLL"))
            def run(self):
                while True:
                    self.fsm_state["bridge"] = "POLL"
                    yield self.env.timeout(1)
                    if not self.mailbox:
                        continue
                    txn = self.mailbox.pop(0)
                    self.fsm_state["bridge"] = "FORWARD"
                    yield self.env.timeout(txn)

        b = Bridge()
        env.process(b.run())
        b.mailbox.append(6)
        env.run(until=100)
        self.assertEqual(env.monitor.samples["b"], [6])

    def test_traced_environment_with_fsm_boundary(self):
        env = TracedEnvironment(names={"Poller.run": ("{self.name}", fsm_idle("p", "POLL"))})

        class Poller:
            def __init__(self):
                self.env, self.name, self.fsm_state, self.mailbox = env, "p", {}, [2, 7]

            def run(self):
                while True:
                    self.fsm_state["p"] = "POLL"
                    yield self.env.timeout(1)
                    if self.mailbox:
                        txn = self.mailbox.pop(0)
                        self.fsm_state["p"] = "WORK"
                        yield self.env.timeout(txn)

        Poller.run.__qualname__ = "Poller.run"
        env.process(Poller().run())
        env.run(until=100)
        self.assertEqual(env.monitor.samples["p"], [2, 7])


if __name__ == "__main__":
    unittest.main()
