# Change points: adding a latency checker to the SoC model

[`soc_model.py`](soc_model.py) is the baseline SoC model. It has 3 IP blocks, 7
pipelines between them, and no latency checking. Each `soc_model_approach_X.py` is a
copy of the baseline with one approach applied. This file lists exactly what each copy
changes. All the diffs below are real `diff -u soc_model.py soc_model_approach_X.py`
output.

## The baseline model

| IP block | Shared resources | Pipelines (stages) |
|---|---|---|
| `dma` | `bus`×2 | `read` (addr, fetch, resp), `write` (addr, data, ack) |
| `crypto` | `aes`×2, `sha`×1 | `encrypt` (key, rounds), `decrypt` (key, rounds), `hash` (pad, digest) |
| `codec` | `dsp`×1, `mem`×2 | `encode` (load, transform, store), `decode` (load, inverse, store) |

Each pipeline is wired the same way inside `IPBlock`:

```
submit() ──► in_q ──► _dispatcher ──► _process ──► _run_stage × N ──► out_q ──► _drain
 (ingress)   Store    (worker loop)   (one txn)    (holds a resource)  Store    (egress)
```

Each approach hooks into a different point on this path:

| Approach | Hooks into |
|---|---|
| **A** | the per-transaction handler `_process`, wrapped at `add_pipeline` |
| **B** | the template methods `_process` and `_run_stage`, through a subclass |
| **C** | the `in_q` and `out_q` stores |
| **D** | `submit()` at ingress and `_drain` at egress |
| **E** | `_process`, through a `@latency_traced` decorator |
| **F** | `env.process()` itself, through a `TracedEnvironment`; no `IPBlock` edits |
| **G** | F, plus the resources and `in_q`, to measure waiting time |

Approaches E, F and G come from the note "Recording pipeline latency in SimPy without
per-job code". Its options A, B and C correspond to E, F and G here. They are covered in
their own section below.

## New files, the same for every approach

| File | Purpose |
|---|---|
| [`latency_monitor.py`](latency_monitor.py) | `LatencyMonitor` records latency and logs an `ERROR` when a budget is exceeded. It also holds the helper for each approach: `wrap` (A), `LatencyCheckedMixin` (B) and `LatencyProbe` (C). |
| [`latency_budgets.yaml`](latency_budgets.yaml) | The configurable budgets: a `default_budget` plus overrides keyed `ip.pipeline`, or `ip.pipeline.stage` for approach B. |
| [`latency_trace.py`](latency_trace.py) | The tracing helpers for E, F and G: `MonitoredEnvironment`, `latency_traced`, `TracedEnvironment`, `TracedStore` and `TracedResource`. They report into the same `LatencyMonitor`. |
| [`test_latency_trace.py`](test_latency_trace.py) | Edge-case tests for `latency_trace.py`: interrupts, failed events, return values, work-loop spans, and queue and resource waits. |
| [`run_all.py`](run_all.py) | Runs the baseline and every approach (A–G), then checks that none of them changes the model's behaviour and that all of them measure identical pipeline latencies. |

## Wiring for approaches A–D

Approaches A–D need the monitor created and passed in. These changes are the same in all
four files, so they are shown once here and left out of the per-approach sections:

```diff
+import logging
 import random
 ...
 import simpy
+
+from latency_monitor import LatencyMonitor          # plus the approach's helper, if any

-def build(env: simpy.Environment) -> dict[str, IPBlock]:
+def build(env: simpy.Environment, monitor: LatencyMonitor) -> dict[str, IPBlock]:

 def run():
     ...
-    blocks = build(env)
+    monitor = LatencyMonitor.from_yaml(env)           # budgets come from latency_budgets.yaml
+    blocks = build(env, monitor)
     env.run(until=SIM_TIME)
-    return blocks, None
+    return blocks, monitor

 if __name__ == "__main__":
-    blocks, _ = run()
+    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
+    blocks, monitor = run()
     for ip, block in blocks.items():
         print(ip, block.completed)
+    monitor.report()
```

Approaches A, C and D also pass the monitor into `IPBlock`:

```diff
 class IPBlock:
-    def __init__(self, env: simpy.Environment, name: str, resources: dict[str, int]):
-        self.env, self.name = env, name
+    def __init__(self, env: simpy.Environment, name: str, resources: dict[str, int], monitor: LatencyMonitor):
+        self.env, self.name, self.monitor = env, name, monitor
 ...
     for ip, spec in IP_SPECS.items():
-        block = IPBlock(env, ip, spec["resources"])
+        block = IPBlock(env, ip, spec["resources"], monitor)
```

---

## Approach A: wrap each pipeline's handler when it is registered

**File:** [`soc_model_approach_a.py`](soc_model_approach_a.py)

### Change points

| # | Where (baseline line) | Change |
|---|---|---|
| A1 | `IPBlock.add_pipeline` (L89) | Wrap `_process` for this pipeline with `monitor.wrap(...)` and pass the wrapped handler to the dispatcher. |
| A2 | `IPBlock._dispatcher` (L95, L98) | Accept the handler and call it instead of `self._process`. |

```diff
     def add_pipeline(self, pname: str, stages: list[Stage]) -> None:
         ...
         self.out_q[pname] = simpy.Store(self.env)
-        self.env.process(self._dispatcher(pname))
+        handler = self.monitor.wrap(f"{self.name}.{pname}", functools.partial(self._process, pname))
+        self.env.process(self._dispatcher(pname, handler))
         self.env.process(self._drain(pname))

-    def _dispatcher(self, pname: str):
+    def _dispatcher(self, pname: str, handler):
         while True:
             txn = yield self.in_q[pname].get()
-            self.env.process(self._process(pname, txn))
+            self.env.process(handler(txn))
```

The bodies of `_process` and `_run_stage` don't change, and a new pipeline added through
`add_pipeline` is checked automatically. A transaction that raises or is interrupted is
still recorded.

---

## Approach B: a mixin overrides the IP block's hook methods

**File:** [`soc_model_approach_b.py`](soc_model_approach_b.py)

### Change points

| # | Where (baseline line) | Change |
|---|---|---|
| B1 | after `class IPBlock` (L115) | Declare `LatencyCheckedIPBlock(LatencyCheckedMixin, IPBlock)`. |
| B2 | `build()` (L125) | Instantiate `LatencyCheckedIPBlock` instead of `IPBlock`. |

```diff
+class LatencyCheckedIPBlock(LatencyCheckedMixin, IPBlock):
+    pass
+
+
 def traffic(env: simpy.Environment, block: IPBlock, pname: str, period: float):
 ...
     for ip, spec in IP_SPECS.items():
-        block = IPBlock(env, ip, spec["resources"])
+        block = LatencyCheckedIPBlock(env, ip, spec["resources"], monitor=monitor)
```

`IPBlock` itself doesn't change at all. The mixin, in
[`latency_monitor.py`](latency_monitor.py), overrides `_process` to time each transaction
and `_run_stage` to time each stage. The stage timing is why B is the only approach that
checks per-stage budgets such as `codec.encode.transform: 5.0`. With separate IP classes
(`class DmaIP(IPBlock)`), you would add the mixin to each class's bases.

---

## Approach C: probe the pipeline's input and output stores

**File:** [`soc_model_approach_c.py`](soc_model_approach_c.py)

### Change points

| # | Where (baseline line) | Change |
|---|---|---|
| C1 | `IPBlock.add_pipeline` (L87–88) | Create the `in_q` and `out_q` stores from a `LatencyProbe` instead of plain `simpy.Store`. |

```diff
         self.pipelines[pname] = stages
-        self.in_q[pname] = simpy.Store(self.env)
-        self.out_q[pname] = simpy.Store(self.env)
+        probe = LatencyProbe(self.monitor, f"{self.name}.{pname}")
+        self.in_q[pname] = probe.ingress(self.env)
+        self.out_q[pname] = probe.egress(self.env)
         self.env.process(self._dispatcher(pname))
```

No processing code changes: not `_dispatcher`, `_process`, `_run_stage` or `_drain`.
The ingress store records the time when a transaction is put into it. The egress store
checks the latency when the transaction is put into it. Transactions are matched by
object identity, so the same object has to leave the pipeline that entered it.

---

## Approach D: timestamp on the transaction, checked at completion

**File:** [`soc_model_approach_d.py`](soc_model_approach_d.py)

### Change points

| # | Where (baseline line) | Change |
|---|---|---|
| D1 | `class Txn` (after L29) | Add the field `t_in: float = 0.0`. |
| D2 | `IPBlock.submit` (L92) | Set `txn.t_in = self.env.now` at ingress. |
| D3 | `IPBlock._drain` (L112) | Keep the completed transaction and call `monitor.record(...)` with its `t_in`. |

```diff
 class Txn:
     id: int
+    t_in: float = 0.0
 ...
     def submit(self, pname: str, txn: Txn):
+        txn.t_in = self.env.now
         return self.in_q[pname].put(txn)
 ...
         while True:
-            yield self.out_q[pname].get()
+            txn = yield self.out_q[pname].get()
+            self.monitor.record(f"{self.name}.{pname}", txn.t_in, txn.id)
             self.completed[pname] = self.completed.get(pname, 0) + 1
```

The timestamp travels with the transaction, so this also works when a transaction
crosses IP blocks. In that case, stamp at the first IP's `submit` and record at the last
IP's `_drain`.

---

## Approaches E–G: tracing at the SimPy level

These approaches don't pass a monitor through the model. Instead, the environment
carries it as `env.monitor`. That makes their wiring smaller: `build()` and
`IPBlock.__init__` keep their signatures.

```diff
+import logging
 import random
 ...
 import simpy
+
+from latency_trace import ...                       # the helpers each approach uses

 def run():
     random.seed(SEED)
-    env = simpy.Environment()
+    env = MonitoredEnvironment()                    # E; F and G use TracedEnvironment(...)
     blocks = build(env)
     env.run(until=SIM_TIME)
-    return blocks, None
+    return blocks, env.monitor

 if __name__ == "__main__":
-    blocks, _ = run()
+    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
+    blocks, monitor = run()
     ...
+    monitor.report()
```

### Approach E: `@latency_traced` decorator (the note's option A)

**File:** [`soc_model_approach_e.py`](soc_model_approach_e.py)

| # | Where (baseline line) | Change |
|---|---|---|
| E1 | above `IPBlock._process` (L100) | Add one decorator line. The name is formatted from the call's arguments. |
| E2 | `run()` (L137) | Create a `MonitoredEnvironment` instead of a plain `simpy.Environment`. |

```diff
+    @latency_traced("{self.name}.{pname}")
     def _process(self, pname: str, txn: Txn):
```

This is approach A as a single line. The wrapping happens where the function is defined
instead of where the pipeline is registered, so `add_pipeline` and `_dispatcher` stay
untouched. It also works on `while True` work loops, where it records one sample per
iteration; see [`LOOP_CHANGE_POINTS.md`](LOOP_CHANGE_POINTS.md).

### Approach F: `TracedEnvironment`, with no IP edits (the note's option B)

**File:** [`soc_model_approach_f.py`](soc_model_approach_f.py)

| # | Where (baseline line) | Change |
|---|---|---|
| F1 | `run()` (L137) | Create a `TracedEnvironment` instead of a `simpy.Environment`. |

```diff
-    env = simpy.Environment()
+    env = TracedEnvironment(names={"IPBlock._process": "{self.name}.{pname}"})
```

This is the **only** change to the model. `TracedEnvironment.process()` wraps every
generator in a proxy that passes each yield, value, interrupt and failed event through
unchanged, and records a latency sample for it:

- A **per-job process** gives one sample from start to return.
- A **`while True: x = yield store.get()` loop** gives one sample per trip round the loop.
  Idle time waiting for input is not counted.

`names` maps a process's `__qualname__` to a per-instance name built from its arguments.
Without `names`, every process is traced under its bare `__qualname__`. That works, but
it aggregates all 7 pipelines into one row, so per-pipeline budgets can't be applied:

```
names=None             n     avg      max
IPBlock._dispatcher  350    0.00     0.00    <- loop iterations: hand-off only
IPBlock._drain       350    0.00     0.00
IPBlock._process     350    7.38    25.03    <- all 7 pipelines mixed together
traffic                7  431.67   573.70    <- whole traffic-generator lifetime
```

Limits:
- Only processes started through `env.process()` are seen.
- A loop that never calls `get()` on a `Store` is only reported when it returns.
- Each yield goes through one extra generator, which slows the simulation down slightly.

### Approach G: F plus waiting time on resources and queues (the note's option C)

**File:** [`soc_model_approach_g.py`](soc_model_approach_g.py)

| # | Where (baseline line) | Change |
|---|---|---|
| G1 | `run()` (L137) | Same as F1. |
| G2 | `IPBlock.__init__` (L79) | Create resources as `TracedResource`, which records `<ip>.<resource>.wait` from request to grant. |
| G3 | `IPBlock.add_pipeline` (L87) | Create `in_q` as a `TracedStore`, which records `<ip>.<pipeline>.in_q.wait`, the time an item sits in the queue. |

```diff
-        self.resources = {k: simpy.Resource(env, capacity=c) for k, c in resources.items()}
+        self.resources = {k: TracedResource(env, c, name=f"{name}.{k}") for k, c in resources.items()}
 ...
-        self.in_q[pname] = simpy.Store(self.env)
+        self.in_q[pname] = TracedStore(self.env, name=f"{self.name}.{pname}.in_q")
```

F shows **how long** each pipeline takes, and G shows **where the time goes**. In this
model, the queueing happens at the shared resources and not in the stores:

```
                    n    avg    p95    max  budget  viol
codec.dsp.wait    100   3.61   9.70  12.14     4.0    41   <- bottleneck: codec.encode/decode miss budget
crypto.sha.wait   100   2.51   8.81  12.64     4.0    24   <- bottleneck: crypto.hash misses budget
crypto.aes.wait   200   0.53   3.28   4.80    12.0     0
dma.bus.wait      300   0.37   1.97   3.14    12.0     0
codec.mem.wait    200   0.05   0.33   1.62    12.0     0
*.in_q.wait        50   0.00   0.00   0.00    12.0     0   <- _dispatcher takes items immediately
```

The `in_q` waits are all 0 because `_dispatcher` starts a process for every transaction
as soon as it arrives. In an IP whose worker loop handles one item at a time, such as
the AXI-style `*_process` loops, the Store wait is where backpressure shows up.
`TracedStore` only works with a plain FIFO `Store`. `PriorityStore` and `FilterStore`
take items out of order, which would pair up the wrong timestamps.

---

## Summary

| | A: wrapper | B: mixin | C: store probe | D: txn timestamp | E: decorator | F: TracedEnvironment | G: F + waits |
|---|---|---|---|---|---|---|---|
| Approach's own change points (excluding wiring) | 2 | 2 | 1 | 3 | 1 | **0** | 2 |
| Lines changed in the model (including wiring) | 31 | 21 | 28 | 28 | 16 | **15** | 19 |
| `IPBlock` code touched | `add_pipeline`, `_dispatcher` | **none** | `__init__`, `add_pipeline` | `__init__`, `submit`, `_drain` | decorator line | **none** | `__init__`, `add_pipeline` |
| Per-stage budgets | no | **yes** | no | no | no | no | no |
| Resource and queue wait time | no | no | no | no | no | no | **yes** |
| End-to-end across IPs | no | no | yes, by probing the outer stores | **yes** | no | no | no |
| Interrupted or failed transactions recorded | yes | yes | no | no | yes | yes | yes |
| Covers new processes automatically | registered ones | **yes** | yes | yes | decorated ones | **yes**, if listed in `names` | **yes** |
| `while True` work loops ([details](LOOP_CHANGE_POINTS.md)) | **yes**, one sample per iteration | no, needs hook methods | in → out time only, including queue wait | ingress → completion time only | **yes**, one sample per iteration | **yes**, one sample per iteration | **yes**, plus queue wait |

The line counts include the docstring's first line and `Run:` line, which change in
every file.

**Which one to pick:**
- **F** for the fewest edits. It needs one line where the environment is created, plus
  a `names` entry for each kind of process you want reported per instance.
- **G** when a pipeline misses its budget and you need to know which resource or queue
  is the cause.
- **B** if your IP blocks share a base class with hook methods. It doesn't touch `IPBlock`
  at all, and it is the only option with per-stage budgets.
- **E** if you want to choose explicitly which methods are measured, with one line each.
- **C** if your pipelines are connected by `Store`s. It is a single change point inside
  the IP.
- **A** if pipelines are registered through a central `add_pipeline`-style hook.
- **D** if you need end-to-end budgets that span several IP blocks.

## Running it

```bash
cd examples/soc_latency
python soc_model.py               # baseline: completion counts only
python soc_model_approach_b.py    # one approach: ERROR lines plus the latency report
python run_all.py                 # every report (A-G), plus the cross-check
python -m unittest test_latency_trace   # edge-case tests for E/F/G
python run_all.py -v              # same, plus every ERROR line
```

Sample error lines:

```
ERROR [t=12.20] LATENCY BUDGET EXCEEDED pipeline=dma.write txn=1 latency=10.56 budget=9.00 (+1.56)
ERROR [t=14.82] LATENCY BUDGET EXCEEDED pipeline=codec.encode.transform txn=1 latency=9.35 budget=5.00 (+4.35)
```

Report from every approach. B adds the `codec.encode.transform` row, and G adds the `*.wait` rows shown above:

```
pipeline                        n     avg     p95     max  budget  viol
codec.decode                   50    8.16   14.25   15.38    12.0    12
codec.encode                   50    9.08   15.40   18.83    10.0    16
codec.encode.transform         50    6.90   13.33   16.16     5.0    28   <- approach B only
crypto.decrypt                 50    6.13    8.69   10.11    12.0     0
crypto.encrypt                 50    6.73   10.96   11.34    12.0     0
crypto.hash                    50    9.57   20.01   25.03     8.0    28
dma.read                       50    5.58    9.29   11.13     8.0     6
dma.write                      50    6.40   10.56   12.28     9.0     4
```
