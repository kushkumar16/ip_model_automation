# Change points: latency for `while True` work loops (approaches A, E and F)

## The problem

A latency wrapper normally measures from a process's start to its return. Every IP in
`src/ip_model_automation/` is built from long-lived `while True` loops that never return.
Wrapped that way, a loop either never reports, or reports one useless sample covering
the whole simulation.

For a loop, the useful sample is **one iteration**, the time it takes to handle one item,
**not counting** idle time spent waiting for the next item. The hard part is knowing
where an iteration starts and ends without adding timestamp code inside the loop.

## The three loop shapes in this repo

| Shape | Real examples | How an iteration's boundary is detected |
|---|---|---|
| **1. Blocks on `store.get()`** | `ArbitrationIP.issue_pipeline`, `policy_update`, `CompletionIP` accept, `StoragePipelineSubsystem.intake_bridge`, `WatchdogIP.config_intake` | **`store_get`** (the default): iteration = `get()` satisfied → next `get()` requested. This also works for `yield get \| timeout` races. |
| **2. Wait on a wake-up event, falling through when work is pending** | `ArbitrationIP.arbiter_main`, `CompletionIP.completion_scheduler` | **`fsm_idle(fsm, *idle_states)`**: iteration = leaving an idle FSM state → re-entering one. The loop doesn't yield between back-to-back items, so a boundary based on yields would merge them. |
| **3. Polls with `timeout(1)`** | `StoragePipelineSubsystem.dispatch_bridge`, `backpressure_monitor` | **`fsm_idle`**: every poll yields, so yields can't tell a poll from work, but the FSM state can. Idle polls are not counted. |

Every IP in the repo already keeps its state in `self.fsm_state[fsm]`, which is the hook
`fsm_idle` uses. `fsm_idle` replaces that dict with a subclass that notifies on each
write. The IP's code, and anything that reads `fsm_state`, keep working unchanged.

## Code

All of this is in [`latency_trace.py`](latency_trace.py):

```python
latency_traced(name_fmt, *, boundary=store_get, mode="auto", id_fmt=None)
```

- **Two uses:** it works as a decorator (approach E) and as a wrapper applied where a
  process is started (approach A).
- **Boundary:** pass `boundary=fsm_idle("bridge", "POLL")` for shapes 2 and 3.
- **Mode:** `mode="auto"` treats a process as a loop from its first boundary on, and
  doesn't count setup before that. A per-job process that never reaches a boundary is
  still timed from start to return. Use `mode="job"` to turn loop detection off.
- **Names:** names and transaction ids are formatted from the loop's **current** local
  variables when the sample is recorded. `"{self.name}.{pname}"` works, and so does
  `"{self.name}.{txn.kind}"`, which splits one loop that serves several kinds of item.
- **Approach F:** `TracedEnvironment(names=...)` takes `(name_fmt, boundary)` pairs, so
  F covers all three shapes too.

## The demo model

[`loop_model.py`](loop_model.py) is the baseline, with no latency code. It has one IP for
each shape: `WorkerIP` (shape 1, with a `read` and a `write` loop), `SchedulerIP` (shape
2) and `BridgeIP` (shape 3). [`loop_model_manual.py`](loop_model_manual.py) is the same
model with hand-written `t0 = env.now` / `record(...)` lines in every loop. It is the
reference the approaches are checked against, and it shows the code they avoid.

Budgets are in [`loop_budgets.yaml`](loop_budgets.yaml). Wiring is the same as for E–G
in [`CHANGE_POINTS.md`](CHANGE_POINTS.md): import, `MonitoredEnvironment` or
`TracedEnvironment` in `run()`, and `monitor.report()` in `__main__`.

---

## Reference: hand-written timestamps, the code A/E/F avoid

**File:** [`loop_model_manual.py`](loop_model_manual.py). This adds 6 lines **inside** the
loop bodies: a start time and a `record()` call in each of the three loops.

```diff
             txn = yield self.in_q[pname].get()
+            t0 = self.env.now  # MANUAL
             self.fsm_state[pname] = "BUSY"
             ...
             self.done[pname].append(txn.id)
+            self.env.monitor.record(f"{self.name}.{pname}", t0, txn.id)  # MANUAL
```

The scheduler and bridge loops get the same pair of lines. Each one also has to be placed
correctly, for example after the fall-through check in the scheduler.

---

## Approach A: wrap each loop where it is started

**File:** [`loop_model_approach_a.py`](loop_model_approach_a.py)

| # | Where (baseline line) | Change |
|---|---|---|
| A1 | `WorkerIP.__init__` (L46) | Wrap `self.worker` with `latency_traced(...)` before starting it. Uses the default `store_get` boundary. |
| A2 | `SchedulerIP.__init__` (L67) | Wrap `self.scheduler` with `boundary=fsm_idle("scheduler", "IDLE")`. |
| A3 | `BridgeIP.__init__` (L95) | Wrap `self.bridge` with `boundary=fsm_idle("bridge", "POLL")`. |

```diff
-            env.process(self.worker(pname, stages))
+            env.process(latency_traced("{self.name}.{pname}")(self.worker)(pname, stages))
 ...
-        env.process(self.scheduler())
+        env.process(latency_traced("{self.name}.scheduler", boundary=fsm_idle("scheduler", "IDLE"))(self.scheduler)())
 ...
-        env.process(self.bridge())
+        env.process(latency_traced("{self.name}.bridge", boundary=fsm_idle("bridge", "POLL"))(self.bridge)())
```

The loop bodies don't change. Only the lines that start the loops do.

---

## Approach E: a decorator on each loop method

**File:** [`loop_model_approach_e.py`](loop_model_approach_e.py)

| # | Where (baseline line) | Change |
|---|---|---|
| E1 | above `WorkerIP.worker` (L51) | Add `@latency_traced("{self.name}.{pname}")`. |
| E2 | above `SchedulerIP.scheduler` (L74) | Add `@latency_traced(..., boundary=fsm_idle("scheduler", "IDLE"))`. |
| E3 | above `BridgeIP.bridge` (L97) | Add `@latency_traced(..., boundary=fsm_idle("bridge", "POLL"))`. |

```diff
+    @latency_traced("{self.name}.{pname}")
     def worker(self, pname: str, stages: list[float]):
 ...
+    @latency_traced("{self.name}.scheduler", boundary=fsm_idle("scheduler", "IDLE"))
     def scheduler(self):
 ...
+    @latency_traced("{self.name}.bridge", boundary=fsm_idle("bridge", "POLL"))
     def bridge(self):
```

That's one added line per loop, with nothing else changed in the IP. This is the smallest
change that stays visible in the IP's own code.

---

## Approach F: `TracedEnvironment`, with no IP edits

**File:** [`loop_model_approach_f.py`](loop_model_approach_f.py)

| # | Where (baseline line) | Change |
|---|---|---|
| F1 | before `run()` (L129) | Add a `LOOP_NAMES` table: the loop's `__qualname__` mapped to a name, or to a `(name, boundary)` pair. |
| F2 | `run()` (L131) | Create a `TracedEnvironment(names=LOOP_NAMES, ...)`. |

```diff
+LOOP_NAMES = {
+    "WorkerIP.worker": "{self.name}.{pname}",
+    "SchedulerIP.scheduler": ("{self.name}.scheduler", fsm_idle("scheduler", "IDLE")),
+    "BridgeIP.bridge": ("{self.name}.bridge", fsm_idle("bridge", "POLL")),
+}
 ...
-    env = simpy.Environment()
+    env = TracedEnvironment(names=LOOP_NAMES, budgets=Path(__file__).with_name("loop_budgets.yaml"))
```

None of the IP classes change. The table can live in a harness or tool instead of the
model file.

---

## Results

`python run_loops.py` runs the baseline, the hand-written reference and A, E and F. It
checks that each approach completes **the same transaction ids** as the baseline and
records **exactly the same samples** as the hand-written reference:

```
pipeline                        n     avg     p95     max  budget  viol
bridge.bridge                  60    3.26    3.98    4.20     4.0     3
dma.read                       60    4.48    5.24    5.26     5.0    12
dma.write                      60    6.59    7.47    7.74     8.0     0
sched.scheduler                60    6.61    7.79    8.30     7.5     7

all loop approaches match the hand-written reference
```

It then shows what is missing without `fsm_idle`. With only the default boundary, the
scheduler and the bridge loops are never reported:

```
== default store_get boundary only ==
dma.read                       60    4.48    5.24    5.26     5.0    12
dma.write                      60    6.59    7.47    7.74     8.0     0
```

Tests are in [`test_latency_trace.py`](test_latency_trace.py)
(`python -m unittest test_latency_trace`). They cover:

- Store loops: one sample per item, with setup and idle time not counted.
- Wrapping a bound method where it is started (approach A).
- `mode="job"` never reporting a loop.
- Per-item names.
- `get | timeout` races.
- A wake-event loop with two items queued back to back. They give two samples, `[3, 4]`,
  where a boundary based on yields would merge them.
- Idle polls not being counted.
- `TracedEnvironment` with an `fsm_idle` boundary.

## Notes and limits

- **Not a pipeline's end-to-end time:** one iteration is the loop's own **service time**
  for one item. The time an item waits in the queue before the loop picks it up is not
  included. Use `TracedStore` (approach G) on the loop's input `Store` to get that, or
  approach D for end-to-end time across several IPs.
- **What a `get | timeout` iteration means:** for a loop like the watchdog countdown, each
  timeout tick is also an iteration, with a latency of about 0.
- **Which FSM values count as idle:** `fsm_idle` needs the IP to write
  `self.fsm_state[fsm] = state`. Writes through `dict.update()` bypass the hook. A
  transition from a start-up state such as `RESET` into the idle state is not counted
  as a sample.
- **Choosing idle states:** list every state in which the loop waits for its next item,
  such as `IDLE` for `arbiter_main`, or `READY` for `CompletionIP`'s accept loop. A
  state that only marks a stall in the middle of an item is not idle.
