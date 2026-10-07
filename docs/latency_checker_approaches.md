# Per-pipeline latency checking in SimPy models

How to add a latency checker to every pipeline of every IP block with as few code changes
as possible, and log an error whenever a pipeline exceeds a configurable latency budget.

Runnable code for every approach is in [`examples/latency_checker/`](../examples/latency_checker/).

## The problem

A model has several IP blocks, and each block has several pipelines. We want, for every
pipeline:

1. The latency of each transaction, measured in simulation time from when the
   transaction enters the pipeline to when it leaves.
2. A budget for each pipeline that can be configured, with a default for pipelines that
   are not listed.
3. A log line at `ERROR` level for every transaction that goes over its budget, plus a
   summary at the end of the run.

Adding timing code inside every pipeline by hand would work, but it is repetitive, easy
to forget for a new pipeline, and spreads the policy across the codebase. The approaches
below put the check in one place.

## The shared part: `LatencyMonitor`

All four approaches use the same monitor, in
[`monitor.py`](../examples/latency_checker/monitor.py). The approaches differ only in
where the start and end times are taken.

```python
mon = LatencyMonitor(env, default_budget=10.0, budgets={"dma.write": 6.0, "crypto.encrypt": 12.0})
mon.record("dma.write", start_time, txn.id)   # computes env.now - start, checks the budget
mon.report()                                  # n / avg / p95 / max / budget / violations per pipeline
```

- **Pipeline names:** pipelines are named `"<ip>.<pipeline>"`, for example `dma.write`.
  Approach B also supports stage names such as `"dma.write.s0"`.
- **Budgets:** budgets are a plain dict, so they can come from YAML, a CLI flag or a test
  fixture.
- **Violations:** each violation is logged through the standard `logging` module on the
  `latency` logger:

  ```
  ERROR [t=472.72] LATENCY BUDGET EXCEEDED pipeline=dma.write txn=36 latency=7.10 budget=6.00 (+1.10)
  ```

  Because it uses `logging`, you can send it to a file, raise it to an exception in
  tests, or silence it with the usual logging configuration.

## The demo scenario

The scenario is the same for every approach:

| IP | Pipelines | Shared resource |
|---|---|---|
| `dma` | `read` (stages 1, 2, 1), `write` (stages 2, 2, 2) | bus, capacity 2 |
| `crypto` | `encrypt` (stages 3, 4), `hash` (stages 2, 2) | core, capacity 3 |

Each stage delay has random jitter, and pipelines in the same IP compete for the shared
resource, so latency includes time spent queueing. Each pipeline gets 40 transactions
with exponential gaps between arrivals.

---

## Approach A: wrap the pipeline when it is registered (recommended)

**File:** [`approach_a_wrapper.py`](../examples/latency_checker/approach_a_wrapper.py)

**Idea:** each pipeline is a generator function that handles one transaction. A wrapper
records `env.now` before running it with `yield from` and checks the latency afterwards.
The wrapper is applied in the one place where pipelines are registered, so no pipeline
body changes.

```python
def wrap_with_latency_check(monitor, name, fn):
    @functools.wraps(fn)
    def wrapper(txn, *args, **kwargs):
        start = monitor.env.now
        try:
            result = yield from fn(txn, *args, **kwargs)
        finally:                       # still recorded if the txn raises or is interrupted
            monitor.record(name, start, getattr(txn, "id", txn))
        return result
    return wrapper
```

**Change to existing code:** one line, in the registration hook.

```diff
 def add_pipeline(self, pname, fn):
-    self.pipelines[pname] = fn
+    self.pipelines[pname] = wrap_with_latency_check(self.monitor, f"{self.name}.{pname}", fn)
```

If there is no central registration hook, you can use `@latency_checked("dma.write")` as
a decorator on each pipeline function instead. That costs one line per pipeline.

**Pros**
- Pipeline code is not touched. New pipelines are covered automatically.
- `yield from` passes through return values, exceptions and interrupts unchanged.
- `try`/`finally` records transactions that fail part-way through.

**Cons**
- Needs each pipeline to be a generator that handles one transaction at a time. Workers
  written as `while True:` loops don't fit; use approach C for those.
- Measures the pipeline end to end only, not individual stages.

---

## Approach B: a `Pipeline` base class with a template `run()`

**File:** [`approach_b_base_class.py`](../examples/latency_checker/approach_b_base_class.py)

**Idea:** a base class owns `run(txn)`, which records the time, calls the subclass's
`stages(txn)`, and checks the latency. Subclasses only implement `stages()`. An optional
`self.stage(name, gen, txn)` helper also times individual stages, but only checks the
stages that have a budget configured.

```python
class Pipeline:
    def run(self, txn):                     # template method, do not override
        start = self.env.now
        try:
            yield from self.stages(txn)
        finally:
            self.monitor.record(self.full_name, start, txn.id)

    def stage(self, sname, gen, txn):       # optional per-stage timing
        start = self.env.now
        yield from gen
        key = f"{self.full_name}.{sname}"
        if key in self.monitor.budgets:
            self.monitor.record(key, start, txn.id)
```

**Change to existing code:** each pipeline is rewritten once as a subclass.

```diff
-def make_stage_pipeline(env, stage_delays, shared):
-    def pipeline(txn):
-        for d in stage_delays:
-            ...
-    return pipeline
+class StagePipeline(Pipeline):
+    def stages(self, txn):
+        for i, d in enumerate(self.stage_delays):
+            yield from self.stage(f"s{i}", self._one_stage(d), txn)
```

**Pros**
- This is the only approach that gives per-stage budgets, for example
  `budgets["dma.write.s0"] = 2.5`.
- One obvious place to add other cross-cutting features later, such as tracing,
  throughput counters or back-pressure statistics.
- New pipelines get the check by subclassing the base class.

**Cons**
- Every existing pipeline has to be restructured once. It is the most intrusive option
  for an existing codebase.
- Best for new models, or for a codebase that is already class-based.

---

## Approach C: probe the pipeline's input and output stores

**File:** [`approach_c_store_probe.py`](../examples/latency_checker/approach_c_store_probe.py)

**Idea:** many SimPy IPs are long-running workers:

```python
while True:
    txn = yield in_q.get()
    ...
    yield out_q.put(txn)
```

There is no per-transaction generator to wrap. Instead, swap the `simpy.Store` at each
edge of the pipeline for a `ProbedStore`. The ingress store records the time when a
transaction is put into it. The egress store looks up that time when the transaction is
put into it and checks the latency.

```python
class ProbedStore(simpy.Store):
    def __init__(self, env, on_put, **kw):
        super().__init__(env, **kw)
        self._on_put = on_put

    def put(self, item):
        self._on_put(item)
        return super().put(item)
```

`LatencyProbe(monitor, "dma.read")` creates the matching `ingress()` and `egress()`
stores for one pipeline and keeps a map from `id(txn)` to its ingress time.

**Change to existing code:** only where the stores are created. Worker code doesn't change.

```diff
-in_q, out_q = simpy.Store(env), simpy.Store(env)
+probe = LatencyProbe(mon, "dma.read")
+in_q, out_q = probe.ingress(env), probe.egress(env)
```

**Pros**
- No change to worker or stage code. Works with free-running `while True:` processes.
- Can span several IPs: put the ingress probe on IP 1's input and the egress probe on
  IP 3's output to check an end-to-end budget across a subsystem.

**Cons**
- Transactions are matched by object identity, so the same transaction object has to
  come out that went in. If a stage creates a new object, carry an ID across and key the
  map on that ID.
- Latency is measured when the transaction is *put* into the egress store. If you want
  time-to-consumer instead, hook `get` rather than `put`.
- A transaction that never comes out stays in the `t_in` map. That is useful for
  detecting hangs, since you can scan for old entries, but it also means memory grows
  while it is stuck.

---

## Approach D: timestamp on the transaction, checked at a common sink

**File:** [`approach_d_txn_timestamp.py`](../examples/latency_checker/approach_d_txn_timestamp.py)

**Idea:** the transaction records its own ingress time (`txn.t_in = env.now`). Whatever
finishes the transaction passes it to `CompletionSink.complete(name, txn)`, which
computes `env.now - txn.t_in` and checks the budget.

**Change to existing code:** two lines: one at ingress, and one at egress for each
pipeline. If you already have one completion IP or egress point, the egress line is
added once.

```diff
 def submit(env, pipeline, txn):
+    txn.t_in = env.now
     return env.process(pipeline(txn))

 def pipeline(txn):
     for d in stage_delays:
         ...
+    sink.complete(name, txn)
```

**Pros**
- The timestamp travels with the transaction across IPs, queues, retries and splits. No
  identity map is needed.
- Fits naturally where the model already has a completion IP or a scoreboard.
- The sink can also record extra fields, such as hop count or retry count.

**Cons**
- The transaction type needs a `t_in` field.
- Every path to completion has to reach the sink. A missed early-exit path silently
  isn't measured.
- A transaction that is interrupted part-way never reaches the sink, unlike approaches A
  and B, which use `finally`.

---

## Comparison

| | A: wrapper | B: base class | C: store probe | D: txn timestamp |
|---|---|---|---|---|
| Changes per existing pipeline | **0** (1 line in the registration hook) | rewrite once | **0** (store creation only) | 1 line, or 0 with a common sink |
| Pipeline style required | generator per txn | class with `stages()` | `Store`-connected workers | any |
| Per-stage budgets | no | **yes** | only by probing between stages | no |
| End-to-end across IPs | no | no | **yes** | **yes** |
| Interrupted or failed txns recorded | **yes** (`finally`) | **yes** (`finally`) | no (stay in the map) | no |
| Risk of missing a pipeline | low (central hook) | low (base class) | medium (store must be swapped) | medium (exit path must call the sink) |

### Which one to pick

- **Pipelines are generators that handle one transaction:** use **A**. It needs the
  fewest changes and covers everything registered through the hook.
- **Pipelines are `while True:` workers connected by `Store`s:** use **C**.
- **You need per-stage budgets, or you are writing new models:** use **B**.
- **You need end-to-end budgets across several IPs, or already have a completion IP:**
  use **D**, or C with probes on the outer edges.

The approaches can be combined because they share one monitor. For example, use A for
per-pipeline budgets and D for one end-to-end budget across the subsystem, each under its
own name.

## Running the examples

```bash
pip install simpy
python examples/latency_checker/run_all.py         # summary report for all four approaches
python examples/latency_checker/run_all.py -v      # also print every ERROR line
python examples/latency_checker/approach_a_wrapper.py   # one approach, with its ERROR lines
```

All four approaches use the same random seed and scenario, so they produce identical
pipeline numbers. That cross-checks that each one measures the same thing:

```
== A: wrapper at registration ==
pipeline                      n     avg     p95     max  budget  viol
crypto.encrypt               40    7.98    9.50   11.41    12.0     0
crypto.hash                  40    4.45    5.44    8.55    10.0     0
dma.read                     40    6.15   11.91   14.30    10.0     4
dma.write                    40    7.51   11.74   12.58     6.0    37

== B: Pipeline base class ==
...same four rows...
dma.write.s0                 40    2.38    3.47    4.86     2.5    12
```

## Practical notes

- **Units:** latency is measured in simulation time, the same units as `env.timeout()`
  and `env.now`. It is not wall-clock time.
- **Default budget:** `default_budget=float("inf")` turns the check off for any pipeline
  without an explicit budget. Use a finite default if every pipeline must have a budget.
- **Failing tests on violations:** after `env.run()`, check
  `assert not mon.violations`. Alternatively, attach a `logging.Handler` that raises on
  `ERROR` to fail at the exact transaction.
- **Warning band:** to warn before the budget is hit, add a `log.warning` at, for
  example, 80% of the budget in `LatencyMonitor.record`. Because every approach goes
  through that method, it is a one-place change.
