# Completion IP — Design Notes (rev C)

Owner: performance modeling
Status: draft, circulated for review

## 1 Why This Block Exists

The Completion IP accepts issued NVMe command descriptors, tracks completion
eligibility, applies QoS/token accounting, and emits completion records toward a
host-facing completion queue interface. It is the natural home for performance
delay modeling of per-tenant completion limits, queue occupancy, and completion
backpressure.

The DLD is based on general NVMe command/completion flow and the QoS concepts
already present in the workspace simulation code: per-tenant queues, IOPS and
bandwidth tokens, refill windows, and weighted ordering.

## 2 Coverage

In scope:

- Accepted command tracking.
- Per-tenant completion queues.
- Read/write IOPS and bandwidth token checks.
- Window-based token refill.
- Completion delivery backpressure.
- Latency, throughput, utilization, and stall metrics.

Out of scope:

- Exact NVMe completion queue entry bit fields.
- Data buffer movement.
- PCIe transaction-level modeling.
- NAND/media functional correctness.

## 3 Where It Sits

In an NVMe controller, commands are submitted by the host, executed internally,
and completed through completion queues. The Completion IP model represents the
late pipeline where a command becomes eligible to complete and is returned to
the host if QoS limits and output interface availability allow it.

For performance modeling, command payload content is unnecessary. Required
metadata is command type, transfer size, tenant, timing, and status.

## 4 Signals

### 4.1 Accepted Command Interface

Producer: upstream execution or scheduler block.

| Signal | Notes |
| --- | --- |
| valid | asserted by the producer |
| ready | asserted by this block |
| cmd_id | identifies the descriptor |
| tenant_id | owning tenant |
| source_id | issuing queue |
| opcode | command type |
| size_kb | transfer size |
| arrival_time | for latency accounting |
| nominal_service_latency | supplied or generated |
| status_hint | early status, if known |

A command enters Completion IP when valid && ready.
Accepted commands are placed into a per-tenant pending queue.

The producer needs no result, only the accept; a full pending queue holds the FSM
in BACKPRESSURE and deasserts ready. It waits in the accept process, in ENQUEUE,
and resumes on accept_ready.

### 4.2 QoS Configuration Interface

Configuration per tenant:

- tenant_alive
- read_iops_per_sec
- write_iops_per_sec
- read_bw_kb_per_sec
- write_bw_kb_per_sec

Global configuration:

- window_ms
- dispatch_tick_ms

Configuration is applied by the refill process; the writer waits in REFILL_BASE
until config_applied comes back.

### 4.3 Completion Queue Interface

Consumer: host completion queue or internal completion writer.

| Signal | Notes |
| --- | --- |
| cpl_valid | completion presented |
| cpl_ready | consumer accepts |
| cmd_id | descriptor being completed |
| tenant_id | owning tenant |
| status | completion status |
| latency | measured end to end |

Completion is emitted only when status is ready, required tokens are
available, and output backpressure allows delivery.
If cpl_ready is low, completions remain queued.

This IP is the one that waits here: while cpl_ready is low the scheduler holds in
STALL_OUTPUT and the completion stays queued. It blocks in the completion
scheduler, in EMIT, and resumes on cpl_ready.

## 5 Command Types

READ:

- Requires one read IOPS debit.
- Requires read bandwidth debit proportional to size_kb.

WRITE:

- Requires one write IOPS debit.
- Requires write bandwidth debit proportional to size_kb.

FLUSH:

- Completion ordering command.
- Uses separate configurable latency and may be exempt from bandwidth tokens.

## 6 Processes

Three processes run in parallel. Their model-side identifiers are accept,
completion_scheduler, and refill.

### 6.1 Accept FSM

Role: accept descriptors and enqueue them per tenant.

| State | Meaning |
| --- | --- |
| RESET | clear queues and counters. |
| READY | accept incoming descriptors. |
| ENQUEUE | place command in per-tenant pending queue. |
| BACKPRESSURE | reject/hold incoming command when queue is full. |

Transitions:

- RESET -> READY: reset deasserted.
- READY -> ENQUEUE: incoming valid and queue has capacity.
- READY -> BACKPRESSURE: incoming valid and target queue full.
- ENQUEUE -> READY: enqueue complete.
- BACKPRESSURE -> READY: space becomes available.

### 6.2 Completion Scheduler FSM

Role: pick the next completion and emit it.

| State | Meaning |
| --- | --- |
| RESET | initialize scheduling order. |
| IDLE | wait for pending completion candidates. |
| SELECT_TENANT | choose next tenant based on weighted order. |
| CHECK_TOKENS | verify IOPS/BW tokens. |
| WAIT_TOKENS | no sufficient tokens for selected command. |
| EMIT | send completion to output interface. |
| STALL_OUTPUT | completion output not ready. |

Transitions:

- IDLE -> SELECT_TENANT: at least one pending command exists.
- SELECT_TENANT -> CHECK_TOKENS: candidate command found.
- CHECK_TOKENS -> EMIT: all required tokens available.
- CHECK_TOKENS -> WAIT_TOKENS: tokens unavailable.
- EMIT -> SELECT_TENANT: more candidates and output slots available.
- EMIT -> IDLE: no more candidates.
- EMIT -> STALL_OUTPUT: output not ready.
- STALL_OUTPUT -> EMIT: output ready.
- WAIT_TOKENS -> SELECT_TENANT: next dispatch tick or refill.

### 6.3 Refill FSM

Role: restore per-window tokens and publish window metrics.

| State | Meaning |
| --- | --- |
| WAIT_WINDOW | wait for next refill boundary. |
| ASSESS_USAGE | capture completed count and utilization. |
| REFILL_BASE | restore per-window base tokens. |
| PUBLISH_METRICS | snapshot window metrics. |

Transitions:

- WAIT_WINDOW -> ASSESS_USAGE: window timer expires.
- ASSESS_USAGE -> REFILL_BASE: usage captured.
- REFILL_BASE -> PUBLISH_METRICS: base tokens restored.
- PUBLISH_METRICS -> WAIT_WINDOW: metrics stored.

## 7 QoS Rules

Base tokens:

- Per-window token allocation is derived from per-second rate and window_ms.
- IOPS and bandwidth are tracked independently.
- Bandwidth token cost is derived from transfer size.
- If base tokens are insufficient, the command remains pending until the next
  refill window.

Tenant alive:

- Commands for inactive tenants remain blocked or are drained with an error,
  depending on configuration.

## 8 Performance Model Requirements

The SimPy model shall include:

- Accept process.
- Completion scheduler process.
- Refill process.
- Per-tenant pending queues as simpy.Store.
- Output completion port as capacity-limited resource or store.
- Token accounting state per tenant.
- Window metric snapshots.
- Latency measurement from command arrival to completion emit.
- Stall counters for no tokens, output backpressure, queue full, and tenant
  inactive.

The performance model shall not model data payloads or exact NVMe completion
entry bit layout.

## 9 SimPy Functionality Requirements

The generated SimPy delay model shall include enough functionality to validate
the FSM behavior from this DLD without modeling payload data. The model must
include:

- Tenant configuration API.
- Command accept API that enqueues commands into per-tenant ready queues after
  accept/service latency.
- Deterministic weighted tenant scheduling.
- Completion eligibility and status generation.
- Token debit model using floating-point or fixed-point units.
- Blocking when base tokens are insufficient.
- Output-ready control for completion queue backpressure.
- Deterministic scheduling order.
- Metrics query API.

Minimum state variables:

- tenant_pending_queues
- tenant_tokens
- tenant_alive
- completed_commands
- window_metrics
- stall_counters

## 10 Clocking And Delays

Clock assumption:

- Core clock frequency: 500 MHz.
- Cycle time: 2 ns.
- All delays below are hard-coded starter values for model generation.

The accept path is a parallel ingress process. Accepted command enqueue: 4 cycles
= 8 ns. Queue-full backpressure retry: 1 cycle = 2 ns.

The completion scheduler is a parallel scheduler process. Tenant select: 8 cycles
= 16 ns. Token check: 5 cycles = 10 ns. Completion emit: 4 cycles = 8 ns. Output
stall retry: 1 cycle = 2 ns.

Refill is a parallel periodic process. Refill window: 10 ms = 5,000,000 cycles.
Usage assessment: 20 cycles = 40 ns. Base refill: 10 cycles = 20 ns. Metrics
publish: 10 cycles = 20 ns.

Parallel processes: Accept FSM, Completion Scheduler FSM, and Refill
FSM run in parallel.
Sequential dependency for a command: accept/enqueue must complete before the
scheduler can select the command; token check must pass before emit.
Refill is periodic and parallel, but it gates scheduler progress when tokens
are unavailable.

End-to-end completion delay:

- No stall after command becomes service-ready:
  Accept enqueue 4 + select 8 + token check 5 + emit 4 = 21 cycles = 42 ns.
- Output backpressure adds 1 cycle = 2 ns per retry tick.
- Token starvation adds delay until the next refill boundary, up to
  10 ms = 5,000,000 cycles.

Sequential/parallel relationship:

```text
Parallel:
  Accept FSM
  Completion Scheduler FSM
  Refill FSM

Sequential command path:
  accept -> enqueue -> select tenant -> check tokens -> emit completion

Parallel gating path:
  Refill FSM updates tokens -> scheduler token check can pass
```

## 11 Loose Ends

- Exact completion queue depth.
- Error behavior for inactive tenants.
- Whether command service latency is supplied externally or generated inside
  Completion IP.
- Whether completion scheduler shares arbitration policy code with Arbitration
  IP.

Rev C note: the completion path was split out of the scheduler block after the
rev B review; the tenant weighting text is inherited from that document and is
informative only.
