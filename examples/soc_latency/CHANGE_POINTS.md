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

## New files, the same for every approach

| File | Purpose |
|---|---|
| [`latency_monitor.py`](latency_monitor.py) | `LatencyMonitor` records latency and logs an `ERROR` when a budget is exceeded. It also holds the helper for each approach: `wrap` (A), `LatencyCheckedMixin` (B) and `LatencyProbe` (C). |
| [`latency_budgets.yaml`](latency_budgets.yaml) | The configurable budgets: a `default_budget` plus overrides keyed `ip.pipeline`, or `ip.pipeline.stage` for approach B. |
| [`run_all.py`](run_all.py) | Runs the baseline and all four approaches, then checks that none of them changes the model's behaviour and that all four measure identical latencies. |

## Wiring, the same for every approach

Every approach needs the monitor created and passed in. These changes are the same in
all four files, so they are shown once here and left out of the per-approach sections:

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

## Summary

| | A: wrapper | B: mixin | C: store probe | D: txn timestamp |
|---|---|---|---|---|
| Approach's own change points (excluding wiring) | 2 (`add_pipeline`, `_dispatcher`) | 2 (new class, instantiation) | **1** (`add_pipeline`) | 3 (`Txn`, `submit`, `_drain`) |
| Lines changed in the model (including wiring) | 31 | **21** | 28 | 28 |
| `IPBlock` processing code touched | dispatcher only | **none** | **none** | ingress and egress |
| Per-stage budgets | no | **yes** | no | no |
| End-to-end across IPs | no | no | yes, by probing the outer stores | **yes** |
| Interrupted or failed transactions recorded | yes | yes | no | no |

The line counts include the docstring's first line and `Run:` line, which change in
every file.

**Which one to pick:**
- **B** if your IP blocks share a base class with hook methods. It doesn't touch `IPBlock`
  at all, and it is the only option with per-stage budgets.
- **C** if your pipelines are connected by `Store`s. It is a single change point inside
  the IP.
- **A** if pipelines are registered through a central `add_pipeline`-style hook.
- **D** if you need end-to-end budgets that span several IP blocks.

## Running it

```bash
cd examples/soc_latency
python soc_model.py               # baseline: completion counts only
python soc_model_approach_b.py    # one approach: ERROR lines plus the latency report
python run_all.py                 # all four reports, plus the cross-check
python run_all.py -v              # same, plus every ERROR line
```

Sample error lines:

```
ERROR [t=12.20] LATENCY BUDGET EXCEEDED pipeline=dma.write txn=1 latency=10.56 budget=9.00 (+1.56)
ERROR [t=14.82] LATENCY BUDGET EXCEEDED pipeline=codec.encode.transform txn=1 latency=9.35 budget=5.00 (+4.35)
```

Report from every approach, with the extra `codec.encode.transform` row only from B:

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
