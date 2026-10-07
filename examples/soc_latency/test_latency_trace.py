"""Edge cases for latency_trace.py. Run: cd examples/soc_latency && python -m unittest test_latency_trace"""

import logging
import unittest

import simpy

from latency_trace import MonitoredEnvironment, TracedEnvironment, TracedResource, TracedStore, latency_traced

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


if __name__ == "__main__":
    unittest.main()
