# Packet latency in SimPy: every approach, with examples

How to measure how long each packet takes, for both ways of writing a SimPy IP:

- **Shape 1, a process per packet.** `env.process(self.handle_packet(pkt))` is started
  for every packet, so many packets are in flight at once.
- **Shape 2, a `while True` loop.** One long-lived loop takes packets from a queue and
  handles them one at a time.

Every example below is real, runnable code in `examples/soc_latency/`. Each approach is
a copy of the same baseline model, [`packet_model.py`](packet_model.py), with the
latency check applied, and the diffs shown are real `diff -u` output. The cross-check
runner [`run_packets.py`](run_packets.py) proves each approach measures what this
document says it does.

---

## 1. Summary: which approach to use

| Approach | What it measures | Change for **a process per packet** | Change for **a `while True` loop** | IP code touched |
|---|---|---|---|---|
| **F: `TracedEnvironment`** ⭐ | handling time | 0 lines in the IP | 0 lines in the IP | **none**: one `env =` line plus a name table |
| **E: decorator** | handling time | 1 line | 1 line | one decorator per method |
| A: wrap where the process is started | handling time | 1 changed line | 1 changed line | the `env.process(...)` lines |
| B: subclass overrides | handling time | 4-line subclass | 4-line subclass | none, but the IP class you instantiate changes |
| G: F plus waits | handling time, plus queue and resource wait | as F, plus 1 line per queue or resource | as F, plus 1 line per queue | where queues and resources are created |
| C: probed queues | arrival → departure | 3 lines | 3 lines | where queues are created |
| D: timestamp on the packet | arrival → departure | 1 field, plus 1 line at ingress and 1 at egress | same | `Packet`, `receive()`, `drain()` |

**Recommendation:**
- **F** for the least change. It covers both shapes without touching the IP.
- **E** if you want the measurement visible in the IP's own code.
- Add **G** when a packet goes over budget and you need to know whether it waited in
  the queue or for a resource.
- Use **D** when the budget is from packet arrival to departure across several IPs.

All approaches log every packet that goes over budget:

```
ERROR [t=32.46] LATENCY BUDGET EXCEEDED pipeline=ser.handle txn=11 latency=4.18 budget=4.00 (+0.18)
ERROR [t=106.72] LATENCY BUDGET EXCEEDED pipeline=par.handle txn=51 latency=7.44 budget=6.00 (+1.44)
```

`monitor.report()` prints a table per name with count, avg, p95, max, budget and
violations. The budgets live in [`latency_budgets.yaml`](latency_budgets.yaml):

```yaml
budgets:
  par.handle: 6.0        # handling time, process-per-packet IP
  ser.handle: 4.0        # handling time, while True loop IP
  par.e2e: 6.0           # arrival -> departure (C, D)
  ser.e2e: 8.0
  ser.rx_q.wait: 4.0     # time queued before the loop picks the packet up (G)
  par.engines.wait: 2.0  # time waiting for a free engine (G)
```

---

## 2. What "packet latency" means

There are two different measurements. Pick the one your budget is about.

```
 receive(pkt)            loop / process picks it up               tx_q.put(pkt)
      |----- queue wait (G) -----|------------- handling -------------|
      |                          |<-- handling time: A, B, E, F, G -->|
      |<---------------- arrival -> departure: C, D ----------------->|
```

- **Handling time** is measured by A, B, E, F and G.
  - **Process per packet:** from when `handle_packet` starts to when it returns. This
    includes waiting for a free engine.
  - **`while True` loop:** from `rx_q.get()` handing over the packet to the loop asking
    for its next packet. Idle time between packets is **not** counted.
- **Arrival → departure** is measured by C and D. It runs from `receive()` to the packet
  being put on `tx_q`, so it includes time spent queued in `rx_q`.
- **Queue wait** is measured by G: how long the packet sat in `rx_q`.

`run_packets.py` checks that, for every packet, **arrival → departure = queue wait +
handling time** exactly.

---

## 3. The baseline model

[`packet_model.py`](packet_model.py) has no latency code. Two IPs receive 80 packets
each, of 64, 512 or 1500 bytes.

```python
class ParallelIP:                                   # Shape 1: one process per packet
    def __init__(self, env, name, engines=2):
        self.env, self.name = env, name
        self.engines = simpy.Resource(env, capacity=engines)
        self.rx_q = simpy.Store(env)
        self.tx_q = simpy.Store(env)
        self.sent = []
        env.process(self.dispatcher())
        env.process(self.drain())

    def receive(self, pkt):
        return self.rx_q.put(pkt)

    def dispatcher(self):
        while True:
            pkt = yield self.rx_q.get()
            self.env.process(self.handle_packet(pkt))   # <- one process per packet

    def handle_packet(self, pkt):
        with self.engines.request() as req:
            yield req
            yield self.env.timeout(1)                   # parse header
            yield self.env.timeout(payload_time(pkt))
        yield self.tx_q.put(pkt)

    def drain(self):
        while True:
            pkt = yield self.tx_q.get()
            self.sent.append(pkt.id)


class SerialIP:                                     # Shape 2: one while True loop
    def __init__(self, env, name):
        self.env, self.name = env, name
        self.fsm_state = {}
        self.rx_q = simpy.Store(env)
        self.tx_q = simpy.Store(env)
        self.sent = []
        env.process(self.rx_loop())                     # <- started once, never returns
        env.process(self.drain())

    def rx_loop(self):
        while True:
            self.fsm_state["rx"] = "IDLE"
            pkt = yield self.rx_q.get()
            self.fsm_state["rx"] = "PARSE"
            yield self.env.timeout(1)
            self.fsm_state["rx"] = "PAYLOAD"
            yield self.env.timeout(payload_time(pkt))
            yield self.tx_q.put(pkt)
    # receive() and drain() are the same as in ParallelIP
```

### The wiring every approach shares

Every approach makes these same changes: create the environment with a monitor, return
it, and print the report.

```diff
 import simpy
+
+from latency_trace import MonitoredEnvironment     # F and G import TracedEnvironment instead

 def run():
     random.seed(SEED)
-    env = simpy.Environment()
+    env = MonitoredEnvironment()                    # F and G: TracedEnvironment(names=PACKET_NAMES)
     ips = build(env)
     env.run(until=SIM_TIME)
-    return ips, None
+    return ips, env.monitor

 if __name__ == "__main__":
-    ips, _ = run()
+    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
+    ips, monitor = run()
     print({name: len(ip.sent) for name, ip in ips.items()})
+    monitor.report()
```

`MonitoredEnvironment` is a `simpy.Environment` that carries `env.monitor`, so nothing
has to pass a monitor through the IP constructors. The sections below show only each
approach's own changes.

---

## 4. Reference: hand-written timestamps (what the approaches avoid)

**File:** [`packet_model_manual.py`](packet_model_manual.py). This is what you would have
to write inside every handler and every loop body:

```diff
     def handle_packet(self, pkt: Packet):
+        t0 = self.env.now  # MANUAL
         with self.engines.request() as req:
             ...
         yield self.tx_q.put(pkt)
+        self.env.monitor.record(f"{self.name}.handle", t0, pkt.id)  # MANUAL

     def rx_loop(self):
         while True:
             self.fsm_state["rx"] = "IDLE"
             pkt = yield self.rx_q.get()
+            t0 = self.env.now  # MANUAL
             self.fsm_state["rx"] = "PARSE"
             ...
             yield self.tx_q.put(pkt)
+            self.env.monitor.record(f"{self.name}.handle", t0, pkt.id)  # MANUAL
```

A, B, E, F and G must produce **exactly** these samples without these lines, and
`run_packets.py` checks that they do.

---

## 5. Approach F: `TracedEnvironment` ⭐ recommended

**File:** [`packet_model_approach_f.py`](packet_model_approach_f.py). **IP code
touched: none.**

**Idea:** `TracedEnvironment.process()` wraps every process listed in `names` in a
transparent proxy. Every yield, value, interrupt and failed event passes through
unchanged. The proxy recognises each shape automatically:
- **A process that returns:** one sample from start to return.
- **A loop that waits on `store.get()`:** one sample per pass through the loop, from
  `get()` satisfied to the next `get()` requested.

**Change:** a name table and the line that creates the environment.

```diff
+PACKET_NAMES = {
+    "ParallelIP.handle_packet": "{self.name}.handle",  # one process per packet
+    "SerialIP.rx_loop": "{self.name}.handle",  # while True loop: one sample per packet
+}
+
+
 def run():
     random.seed(SEED)
-    env = simpy.Environment()
+    env = TracedEnvironment(names=PACKET_NAMES)
```

- **Keys** are the function's `__qualname__`.
- **Values** are names formatted from the function's local variables, so
  `"{self.name}.handle"` gives `par.handle` and `ser.handle`.

**Result:**

```
pipeline                        n     avg     p95     max  budget  viol
par.handle                     80    3.58    6.21    8.99     6.0     6
ser.handle                     80    2.31    4.69    4.79     4.0    11
```

**Pros:**
- No IP changes for either shape.
- New IPs are covered by adding one table entry.
- The table can live in a test harness instead of the model.

**Cons:**
- Only processes started with `env.process()` are seen.
- The measurement isn't visible in the IP's code.

**Polling or wake-event loops:** a loop that doesn't wait on `store.get()`, such as one
that polls with `yield timeout(1)` or waits on a wake-up event, needs its iteration
boundary taken from the IP's FSM state:

```python
"MyIP.poll_loop": ("{self.name}.handle", fsm_idle("poll", "IDLE")),
```

---

## 6. Approach E: a decorator on each method

**File:** [`packet_model_approach_e.py`](packet_model_approach_e.py). **IP code touched:
one line per method.**

**Idea:** this is the same proxy as F, applied explicitly with `@latency_traced(...)`.
The decorator is the same for both shapes, because a loop is detected automatically.

```diff
     def dispatcher(self):
         ...
+    @latency_traced("{self.name}.handle")
     def handle_packet(self, pkt: Packet):          # Shape 1: process per packet
         with self.engines.request() as req:
 ...
+    @latency_traced("{self.name}.handle")
     def rx_loop(self):                             # Shape 2: while True loop
         while True:
```

**Result:** identical to F (`par.handle` and `ser.handle` as above).

**Pros:**
- Explicit: reading the IP shows what is measured.
- One line per method.

**Cons:**
- It is still an edit to every IP.
- `env` must be a `MonitoredEnvironment`.

**Polling or wake-event loops:**
`@latency_traced("{self.name}.handle", boundary=fsm_idle("poll", "IDLE"))`.

---

## 7. Approach A: wrap where the process is started

**File:** [`packet_model_approach_a.py`](packet_model_approach_a.py). **IP code touched:
the `env.process(...)` lines.**

**Idea:** the same `latency_traced` wrapper, applied where the process is **started**
instead of where it is defined. This suits a central place that registers or starts
processes.

```diff
     def dispatcher(self):                          # Shape 1: wrap each per-packet process
         while True:
             pkt = yield self.rx_q.get()
-            self.env.process(self.handle_packet(pkt))
+            self.env.process(latency_traced("{self.name}.handle")(self.handle_packet)(pkt))
 ...
         self.sent: list[int] = []                  # Shape 2: wrap the loop once
-        env.process(self.rx_loop())
+        env.process(latency_traced("{self.name}.handle")(self.rx_loop)())
         env.process(self.drain())
```

**Result:** identical to F.

**Pros:**
- The process methods are untouched.
- You choose what to measure at the place where processes are started.

**Cons:**
- The start lines are longer.
- Every place that starts the process needs the change.

---

## 8. Approach B: subclasses override the IP's methods

**File:** [`packet_model_approach_b.py`](packet_model_approach_b.py). **IP code touched:
none.** You instantiate a subclass instead.

**Idea:** leave `ParallelIP` and `SerialIP` as they are. Add subclasses whose overrides
wrap the parent's method, and build the subclasses.

```diff
+class TracedParallelIP(ParallelIP):
+    @latency_traced("{self.name}.handle")
+    def handle_packet(self, pkt: Packet):          # Shape 1
+        return super().handle_packet(pkt)
+
+
+class TracedSerialIP(SerialIP):
+    @latency_traced("{self.name}.handle")
+    def rx_loop(self):                             # Shape 2
+        return super().rx_loop()
+
 ...
 def build(env: simpy.Environment):
-    par = ParallelIP(env, "par")
-    ser = SerialIP(env, "ser")
+    par = TracedParallelIP(env, "par")
+    ser = TracedSerialIP(env, "ser")
```

**Result:** identical to F.

**Pros:**
- The original IP file stays untouched.
- The subclass is the place to add more overrides, such as per-stage timing on a helper
  method.

**Cons:**
- It's the most added code: about 4 lines per method.
- Everywhere the IP is constructed has to use the subclass.

---

## 9. Approach G: F plus queue and resource waits

**File:** [`packet_model_approach_g.py`](packet_model_approach_g.py). **IP code touched:
where queues and resources are created.**

**Idea:** F gives handling time. G also shows **where the time goes**. `TracedStore`
records how long each packet sat in `rx_q`. `TracedResource` records how long each
request waited for a free engine.

```diff
+PACKET_NAMES = { ...same as F... }
-    env = simpy.Environment()
+    env = TracedEnvironment(names=PACKET_NAMES)
 ...
 class ParallelIP:                                  # Shape 1
-        self.engines = simpy.Resource(env, capacity=engines)
-        self.rx_q = simpy.Store(env)
+        self.engines = TracedResource(env, engines, name=f"{name}.engines")
+        self.rx_q = TracedStore(env, name=f"{name}.rx_q")
 ...
 class SerialIP:                                    # Shape 2
-        self.rx_q = simpy.Store(env)
+        self.rx_q = TracedStore(env, name=f"{name}.rx_q")
```

**Result:**

```
pipeline                        n     avg     p95     max  budget  viol
par.engines.wait               80    0.95    3.02    6.01     2.0    14   <- waiting for a free engine
par.handle                     80    3.58    6.21    8.99     6.0     6
par.rx_q.wait                  80    0.00    0.00    0.00    12.0     0   <- dispatcher takes packets immediately
ser.handle                     80    2.31    4.69    4.79     4.0    11
ser.rx_q.wait                  80    1.31    5.35    7.26     4.0     8   <- packets queue while the loop is busy
```

**How to read it:**
- **`ParallelIP`:** packets never queue in `rx_q`. Their extra latency is waiting for
  one of the 2 engines.
- **`SerialIP`:** packets queue in `rx_q` while the loop handles the previous one. That
  queueing is time that handling-time approaches don't see.

**Pros:**
- Explains *why* a packet went over budget.

**Cons:**
- `TracedStore` only works with a plain FIFO `Store`, not `PriorityStore` or
  `FilterStore`.

---

## 10. Approach C: probe the rx and tx queues (arrival → departure)

**File:** [`packet_model_approach_c.py`](packet_model_approach_c.py). **IP code touched:
where queues are created.**

**Idea:** replace `rx_q` and `tx_q` with a probed pair. The probe records the time when a
packet is put on `rx_q`, and checks the latency when the same packet is put on `tx_q`.
It works the same for both shapes, because it never looks at the processes.

```diff
 class ParallelIP:                                  # Shape 1, and the same in SerialIP (Shape 2)
     def __init__(self, env, name, engines=2):
         ...
-        self.rx_q = simpy.Store(env)
-        self.tx_q = simpy.Store(env)
+        probe = LatencyProbe(env.monitor, f"{name}.e2e")
+        self.rx_q = probe.ingress(env)
+        self.tx_q = probe.egress(env)
```

**Result:**

```
pipeline                        n     avg     p95     max  budget  viol
par.e2e                        80    3.58    6.21    8.99     6.0     6
ser.e2e                        80    3.62    8.39    9.90     8.0     5   <- includes rx_q queueing
```

**Pros:**
- No process code changes.
- Includes queue wait.

**Cons:**
- Packets are matched by object identity, so the same `Packet` object must reach
  `tx_q`. That breaks if a stage rebuilds the packet.
- A packet that is dropped stays in the probe's map.

---

## 11. Approach D: timestamp on the packet (arrival → departure)

**File:** [`packet_model_approach_d.py`](packet_model_approach_d.py). **IP code touched:
`Packet`, `receive()`, `drain()`.**

**Idea:** the packet carries its arrival time. Stamp it once at ingress and check it
wherever the packet leaves. Because the timestamp travels with the packet, this is the
approach for budgets that span **several IPs**.

```diff
 @dataclass
 class Packet:
     id: int
     size: int
+    t_in: float = 0.0
 ...
     def receive(self, pkt: Packet):                # both IPs
+        pkt.t_in = self.env.now
         return self.rx_q.put(pkt)
 ...
     def drain(self):                               # both IPs
         while True:
             pkt = yield self.tx_q.get()
             self.sent.append(pkt.id)
+            self.env.monitor.record(f"{self.name}.e2e", pkt.t_in, pkt.id)
```

**Result:** identical to C (`par.e2e` and `ser.e2e` as above).

**Pros:**
- Works across IPs, queues and retries.
- No identity matching.

**Cons:**
- `Packet` needs the field.
- Every exit path must record. A path you forget is silently not measured.

---

## 12. Proof that the approaches agree

`python run_packets.py` runs the baseline, the hand-written reference and all seven
approaches, then checks:

1. Every approach sends **exactly the same packet ids** as the baseline, so the latency
   check doesn't change model behaviour.
2. A, B, E, F and G record **exactly the same handling times** as the hand-written
   reference, for both shapes.
3. C and D record exactly the same arrival-to-departure times.
4. For every packet, **arrival → departure = `rx_q` wait + handling time**.

```
A/B/E/F/G match the hand-written reference, C == D, and e2e = rx_q wait + handling for every packet
```

---

## 13. Running it

```bash
cd examples/soc_latency
python packet_model.py              # baseline, no latency code
python packet_model_approach_f.py   # any single approach: ERROR lines plus the report
python run_packets.py               # all approaches plus the cross-checks
python run_packets.py -v            # same, plus every ERROR line
```

Helper code:
- [`latency_trace.py`](latency_trace.py): `MonitoredEnvironment`, `latency_traced`,
  `fsm_idle`, `TracedEnvironment`, `TracedStore` and `TracedResource`.
- [`latency_monitor.py`](latency_monitor.py): `LatencyMonitor` and `LatencyProbe`.

Tests: `python -m unittest test_latency_trace`.

For the same approaches on a larger multi-IP model, and on polling and wake-event loops,
see [`LATENCY_GUIDE.md`](LATENCY_GUIDE.md).
