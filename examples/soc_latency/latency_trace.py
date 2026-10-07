"""Latency tracing at the SimPy level: measure processes and queues without per-job timing code.

These are the three options from the "Recording pipeline latency in SimPy without per-job
code" note, built on the same LatencyMonitor as approaches A-D, so budgets and ERROR
logging work unchanged:

- MonitoredEnvironment        a simpy.Environment that carries a LatencyMonitor (env.monitor)
- latency_traced(name_fmt)    decorator: one line on a generator method            (approach E)
- TracedEnvironment           env.process() traces every process: no IP edits      (approach F)
- TracedStore/TracedResource  time spent waiting in a queue or for a resource      (approach G)

What one latency sample means for each process shape (TracedEnvironment):
- a per-job process (returns when its job is done): process start -> return
- a long-lived `while True: x = yield store.get()` loop: one trip round the loop, from the
  get() being satisfied to the next get() being requested. Idle time waiting for input is
  not counted.
"""

from __future__ import annotations

import functools
import inspect
import math
from collections import deque
from pathlib import Path

import simpy
from simpy.resources.store import StoreGet

from latency_monitor import DEFAULT_CONFIG, LatencyMonitor


class MonitoredEnvironment(simpy.Environment):
    """simpy.Environment + env.monitor (a LatencyMonitor loaded from the budgets YAML)."""

    def __init__(self, initial_time: float = 0, budgets: Path | str = DEFAULT_CONFIG):
        super().__init__(initial_time)
        self.monitor = LatencyMonitor.from_yaml(self, budgets)


def _txn_id(local_vars) -> object:
    txn = local_vars.get("txn")
    return getattr(txn, "id", txn) if txn is not None else "-"


# ---- approach E --------------------------------------------------------------------------
def latency_traced(name_fmt: str):
    """Decorate a generator function so each call is one latency sample.

    name_fmt is formatted with the call's arguments, e.g. "{self.name}.{pname}". The monitor
    is taken from self.env (or an `env` argument), which must be a MonitoredEnvironment.
    Use it on per-job processes only: on a `while True` loop it times the loop's lifetime.
    """

    def decorator(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            a = bound.arguments
            env = a["self"].env if "self" in a else a["env"]
            return env.monitor.timed(name_fmt.format(**a), fn(*args, **kwargs), _txn_id(a))

        return wrapper

    return decorator


# ---- approach F --------------------------------------------------------------------------
class TracedEnvironment(MonitoredEnvironment):
    """env.process() wraps every generator in a transparent proxy that records its latency.

    names maps a generator's __qualname__ to a name format filled from its arguments, e.g.
    {"IPBlock._process": "{self.name}.{pname}"}. Only listed processes are traced. With
    names=None every process is traced under its bare __qualname__ (so all IPBlock._process
    calls aggregate into one name).

    Limits: only processes started via env.process() are seen; a loop that never get()s
    from a Store is only reported when it returns.
    """

    def __init__(self, names: dict[str, str] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.names = names

    def process(self, generator):
        name = self._name_for(generator)
        if name is not None:
            generator = self._trace(generator, name)
        return super().process(generator)

    def _name_for(self, gen) -> str | None:
        qualname = getattr(gen, "__qualname__", None)
        if self.names is None:
            return qualname
        fmt = self.names.get(qualname)
        return None if fmt is None else fmt.format(**dict(gen.gi_frame.f_locals))

    def _trace(self, gen, name: str):
        """Forward every yield, send and throw unchanged; record spans around them."""
        monitor = self.monitor
        start, span_open, is_loop = self.now, True, False
        send, throw = None, None
        try:
            while True:
                try:
                    event = gen.throw(throw) if throw is not None else gen.send(send)
                except StopIteration as stop:
                    if span_open:
                        monitor.record(name, start, _txn_id(_locals(gen)))
                    return stop.value
                except Exception:
                    if span_open:  # failed / interrupted jobs still count
                        monitor.record(name, start, _txn_id(_locals(gen)))
                    raise
                if isinstance(event, StoreGet):  # a work loop waiting for its next item
                    if is_loop and span_open:
                        monitor.record(name, start, _txn_id(_locals(gen)))
                    is_loop, span_open = True, False  # setup before the first get() is not counted
                try:
                    send, throw = (yield event), None
                except GeneratorExit:
                    raise
                except BaseException as exc:  # simpy.Interrupt or a failed event: pass it on
                    send, throw = None, exc
                if not span_open:
                    start, span_open = self.now, True
        finally:
            gen.close()


def _locals(gen) -> dict:
    return gen.gi_frame.f_locals if gen.gi_frame is not None else {}


# ---- approach G --------------------------------------------------------------------------
class TracedStore(simpy.Store):
    """FIFO simpy.Store that records "<name>.wait": how long each item sat before a get().

    Plain FIFO Store only: PriorityStore/FilterStore take items out of order and would
    mis-pair the timestamps. The env must be a MonitoredEnvironment.
    """

    def __init__(self, env: MonitoredEnvironment, capacity: float = math.inf, *, name: str):
        super().__init__(env, capacity)
        self.name = name
        self._t_in: deque[float] = deque()

    def _do_put(self, event):
        before = len(self.items)
        proceed = super()._do_put(event)
        if len(self.items) > before:
            self._t_in.append(self._env.now)
        return proceed

    def _do_get(self, event):
        before = len(self.items)
        proceed = super()._do_get(event)
        if len(self.items) < before:
            self._env.monitor.record(f"{self.name}.wait", self._t_in.popleft(), getattr(event.value, "id", "-"))
        return proceed


class TracedResource(simpy.Resource):
    """simpy.Resource that records "<name>.wait": time from request() to the grant."""

    def __init__(self, env: MonitoredEnvironment, capacity: int = 1, *, name: str):
        super().__init__(env, capacity)
        self.name = name

    def request(self):
        req = super().request()
        t_req = self._env.now
        req.callbacks.append(lambda _ev: self._env.monitor.record(f"{self.name}.wait", t_req, "-"))
        return req
