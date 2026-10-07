"""Latency tracing at the SimPy level: measure processes and queues without per-job timing code.

These are the three options from the "Recording pipeline latency in SimPy without per-job
code" note, built on the same LatencyMonitor as approaches A-D, so budgets and ERROR
logging work unchanged:

- MonitoredEnvironment        a simpy.Environment that carries a LatencyMonitor (env.monitor)
- latency_traced(name_fmt)    decorator, or a wrapper applied at registration    (approaches E, A)
- TracedEnvironment           env.process() traces every process: no IP edits   (approach F)
- TracedStore/TracedResource  time spent waiting in a queue or for a resource    (approach G)

One latency sample per process shape
------------------------------------
- per-job process (returns when its job is done): start -> return.
- `while True` work loop: one *iteration*. Where an iteration starts and ends is the
  loop's boundary:
    store_get (default)  the loop blocks on Store.get() (also inside `get | timeout`).
                         Iteration = get() satisfied -> next get() requested; idle
                         time waiting for input is not counted.
    fsm_idle(fsm, *idle) the loop has an FSM in self.fsm_state[fsm]. Iteration = leaving
                         an idle state -> re-entering one. Use this for polling loops
                         (`yield timeout(1)` every pass) and for loops that wait on a
                         wake-up event but fall straight through when work is pending.
  mode="auto" (default) treats a process as a loop once it reaches its first boundary;
  the setup before that is not counted. mode="job" never splits; mode="loop" always does.

Names and transaction ids are formatted from the generator's *current* local variables
when a sample is recorded, so "{self.name}.{pname}" or "{self.name}.{txn.kind}" both work,
and a loop serving several pipelines can be split by the item it is handling.
"""

from __future__ import annotations

import functools
import inspect
import math
from collections import deque
from pathlib import Path

import simpy
from simpy.events import Condition
from simpy.resources.store import StoreGet

from latency_monitor import DEFAULT_CONFIG, LatencyMonitor


class MonitoredEnvironment(simpy.Environment):
    """simpy.Environment + env.monitor (a LatencyMonitor loaded from the budgets YAML)."""

    def __init__(self, initial_time: float = 0, budgets: Path | str = DEFAULT_CONFIG):
        super().__init__(initial_time)
        self.monitor = LatencyMonitor.from_yaml(self, budgets)


# ---- boundaries --------------------------------------------------------------------------
def store_get(event) -> bool:
    """Default loop boundary: the process is waiting for its next input item."""
    if isinstance(event, StoreGet):
        return True
    return isinstance(event, Condition) and any(store_get(e) for e in event._events)


class fsm_idle:  # noqa: N801 - used like a function: boundary=fsm_idle("bridge", "POLL")
    """Loop boundary from the owner's FSM: an iteration runs from leaving an idle state
    to re-entering one. The owner (self) must keep its states in a dict attribute
    (default `fsm_state`) that is written as `self.fsm_state[fsm] = state`."""

    def __init__(self, fsm: str, *idle_states: str, attr: str = "fsm_state"):
        if not idle_states:
            raise ValueError("fsm_idle needs at least one idle state")
        self.fsm, self.idle, self.attr = fsm, frozenset(idle_states), attr


class _HookedStates(dict):
    """dict that tells listeners about every `d[key] = value`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.listeners = []

    def __setitem__(self, key, value):
        old = self.get(key)
        super().__setitem__(key, value)
        for listener in self.listeners:
            listener(key, old, value)


# ---- core --------------------------------------------------------------------------------
def _locals(gen) -> dict:
    return gen.gi_frame.f_locals if gen.gi_frame is not None else {}


def _txn_id(local_vars) -> object:
    txn = local_vars.get("txn")
    return getattr(txn, "id", txn) if txn is not None else "-"


def _emit(monitor: LatencyMonitor, loc: dict, name_fmt: str, id_fmt: str | None, start: float) -> None:
    txn_id = id_fmt.format(**loc) if id_fmt else _txn_id(loc)
    monitor.record(name_fmt.format(**loc), start, txn_id)


def trace_generator(monitor: LatencyMonitor, gen, name_fmt: str, *, boundary=store_get, mode="auto", id_fmt=None):
    """Proxy generator: forwards every yield, value, interrupt and failure unchanged, and
    records one sample per job (start -> return) or per loop iteration (see module doc)."""
    env = monitor.env
    in_loop = mode == "loop"
    start, span_open = env.now, True
    send, throw = None, None
    loc = dict(_locals(gen))  # snapshot: the frame is gone once the generator finishes
    try:
        while True:
            try:
                event = gen.throw(throw) if throw is not None else gen.send(send)
            except StopIteration as stop:
                if span_open and (in_loop or mode != "loop"):
                    _emit(monitor, loc, name_fmt, id_fmt, start)
                return stop.value
            except Exception:
                if span_open:  # failed / interrupted jobs still count
                    _emit(monitor, loc, name_fmt, id_fmt, start)
                raise
            loc = dict(_locals(gen))
            if mode != "job" and boundary(event):
                if in_loop and span_open:
                    _emit(monitor, loc, name_fmt, id_fmt, start)
                in_loop, span_open = True, False  # setup before the first boundary is not counted
            try:
                send, throw = (yield event), None
            except GeneratorExit:
                raise
            except BaseException as exc:  # simpy.Interrupt or a failed event: pass it on
                send, throw = None, exc
            if not span_open:
                start, span_open = env.now, True
    finally:
        gen.close()


def trace_fsm(monitor: LatencyMonitor, owner, gen, name_fmt: str, boundary: fsm_idle, id_fmt=None):
    """Record one sample each time owner's FSM goes idle -> busy -> idle. Returns gen as is."""
    states = getattr(owner, boundary.attr)
    if not isinstance(states, _HookedStates):
        states = _HookedStates(states)
        setattr(owner, boundary.attr, states)
    start = None

    def on_change(fsm, old, new):
        nonlocal start
        if fsm != boundary.fsm:
            return
        if old in boundary.idle and new not in boundary.idle:
            start = monitor.env.now
        elif new in boundary.idle and old not in boundary.idle and start is not None:
            _emit(monitor, dict(_locals(gen)), name_fmt, id_fmt, start)
            start = None

    states.listeners.append(on_change)
    return gen


def _trace(monitor, owner, gen, name_fmt, boundary, mode, id_fmt):
    if isinstance(boundary, fsm_idle):
        return trace_fsm(monitor, owner, gen, name_fmt, boundary, id_fmt)
    return trace_generator(monitor, gen, name_fmt, boundary=boundary, mode=mode, id_fmt=id_fmt)


# ---- approaches A and E ------------------------------------------------------------------
def latency_traced(name_fmt: str, *, boundary=store_get, mode: str = "auto", id_fmt: str | None = None):
    """Trace a generator function: one sample per job, or per iteration of a work loop.

    As a decorator (approach E):          @latency_traced("{self.name}.{pname}")
    At registration (approach A):         env.process(latency_traced(fmt)(self.worker)(pname))
    A polling or wake-event loop:         @latency_traced("{self.name}.bridge", boundary=fsm_idle("bridge", "POLL"))

    The monitor is env.monitor, with env found on self (or the bound method's object) or
    an `env` argument; it must be a MonitoredEnvironment.
    """

    def decorator(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            a = bound.arguments
            owner = a.get("self", getattr(fn, "__self__", None))
            env = owner.env if owner is not None else a["env"]
            return _trace(env.monitor, owner, fn(*args, **kwargs), name_fmt, boundary, mode, id_fmt)

        return wrapper

    return decorator


# ---- approach F --------------------------------------------------------------------------
class TracedEnvironment(MonitoredEnvironment):
    """env.process() wraps every generator in a transparent proxy that records its latency.

    names maps a generator's __qualname__ to a name format, or to (name format, boundary):
        {"IPBlock._process": "{self.name}.{pname}",
         "Bridge.run": ("{self.name}.bridge", fsm_idle("bridge", "POLL"))}
    Only listed processes are traced. With names=None every process is traced under its
    bare __qualname__ (so all IPBlock._process calls aggregate into one name).

    Limits: only processes started via env.process() are seen; with the default boundary
    a loop that never get()s from a Store is only reported when it returns.
    """

    def __init__(self, names: dict | None = None, **kwargs):
        super().__init__(**kwargs)
        self.names = names

    def process(self, generator):
        spec = self._spec_for(generator)
        if spec is not None:
            name_fmt, boundary = spec
            owner = _locals(generator).get("self")
            generator = _trace(self.monitor, owner, generator, name_fmt, boundary, "auto", None)
        return super().process(generator)

    def _spec_for(self, gen):
        qualname = getattr(gen, "__qualname__", None)
        if self.names is None:
            return (qualname, store_get) if qualname else None
        spec = self.names.get(qualname)
        if spec is None:
            return None
        return (spec, store_get) if isinstance(spec, str) else spec


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
