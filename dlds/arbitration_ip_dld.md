# Arbitration IP Design-Level Document

## 1. Purpose

The Arbitration IP selects NVMe commands from multiple ingress sources and
issues them toward the execution/completion pipeline. It models fairness,
priority, per-tenant eligibility, queue availability, interface backpressure,
and credit constraints. The block does not execute the command payload.

The performance model must capture command selection timing and stalls. The
functional model must capture command ordering, selected source, selected
tenant, and policy decisions.

## 2. Scope

In scope:

- Multi-source command arbitration.
- Hierarchical arbitration across port, tenant, and submission queue.
- Single-port and dual-port selection modes.
- Pending bitmap maintenance at port, tenant, and SQ levels.
- Per-tenant command queues.
- Per-port tenant binding.
- Per-tenant submission queue binding.
- Read/write command type awareness.
- Round-robin, weighted round-robin, and strict priority policies.
- Backpressure from downstream command issue interface.
- Eligibility checks based on queue non-empty, tenant alive, command type, and
  optional token/credit availability.
- Selected command metadata forwarding.

Out of scope:

- NAND/media timing.
- Data payload transformation.
- NVMe protocol bit-accurate command decode.
- Completion generation.

## 3. Context

In an NVMe controller, host commands arrive through submission queues. Internal
front-end logic classifies commands by port, tenant, namespace, stream,
operation type, and QoS group. The Arbitration IP chooses which pending command
is issued next to the execution pipeline.

This model assumes each command has already been normalized into an internal
command descriptor.

The Arbitration IP uses two internal pipelines.

Pipeline 1 performs hierarchical selection:

```text
Port arbitration
  -> Tenant arbitration inside selected port
  -> Submission queue arbitration inside selected tenant
  -> Selected SQ descriptor
```

The hierarchy is required because the same IP may be configured for either a
single host-facing port or two host-facing ports. Each port owns a set of
tenants, and each tenant owns a set of submission queues. Arbitration only
considers hierarchy nodes with pending commands.

Pipeline 2 performs issue preparation:

```text
Selected SQ descriptor
  -> read pending command count
  -> read device/tenant/SQ burst state
  -> compute issue_count = min(pending_cmd_count, device_burst, tenant_burst, sq_burst)
  -> issue request to downstream IP
```

Pipeline 2 does not choose a different SQ. It only determines how many commands
from the selected SQ may be sent forward in the current issue opportunity.

## 4. Interfaces

### 4.1 Ingress Queue Interface

Logical interface per source or tenant.

Fields:

- `valid`: queue contains at least one command.
- `ready`: arbiter can pop a command.
- `port_id`: host port or internal ingress port.
- `tenant_id`: tenant or QoS group.
- `sq_id`: submission queue within the tenant.
- `source_id`: optional source alias for debug/trace.
- `cmd_id`: internal command identifier.
- `opcode`: read, write, admin, flush, or vendor.
- `size_kb`: command transfer size.
- `priority`: optional command priority.
- `age`: optional age counter for starvation prevention.

Timing:

- A pop consumes one command when `valid && ready`.
- Pop-to-issue latency is configurable and defaults to one cycle.
- Queue valid contributes to the SQ pending bitmap.
- Any SQ pending bit set contributes to the tenant pending bitmap.
- Any tenant pending bit set contributes to the port pending bitmap.

### 4.2 Pending Bitmap Interface

The arbiter maintains three levels of pending state.

Port-level pending bitmap:

- One bit per configured port.
- Bit is set when any tenant linked to the port has at least one pending SQ.
- In single-port mode, only `port0_pending` is meaningful.
- In dual-port mode, both `port0_pending` and `port1_pending` participate in
  port arbitration.

Tenant-level pending bitmap:

- One bitmap per port.
- One bit per tenant linked to that port.
- Tenant bit is set when any SQ linked to that tenant has a pending command.
- Tenant bit is cleared when all linked SQs are empty or ineligible.

SQ-level pending bitmap:

- One bitmap per tenant.
- One bit per submission queue linked to that tenant.
- SQ bit is set when the SQ has one or more pending commands.
- SQ bit is cleared when the SQ becomes empty or blocked by ordering rules.

Bitmap update timing:

- SQ pending update: 1 cycle after queue valid/empty change.
- Tenant pending update: 1 cycle after SQ bitmap change.
- Port pending update: 1 cycle after tenant bitmap change.
- Worst-case visibility from SQ command arrival to port pending visibility:
  `3 cycles = 6 ns` at 500 MHz.

### 4.3 Downstream Issue Interface

Fields:

- `issue_valid`
- `issue_ready`
- `cmd_descriptor`
- `selected_port`
- `selected_tenant`
- `selected_sq`
- `pending_cmd_count`
- `device_burst_available`
- `tenant_burst_available`
- `sq_burst_available`
- `issue_count`
- `selection_reason`

Timing:

- If `issue_ready` is low, arbitration may continue evaluating candidates, but
  no command is removed from ingress queues.
- If `issue_ready` is high and a candidate is eligible, one command may be
  issued per arbitration slot per tick.
- In burst mode, `issue_count` may be greater than one. The issued count is the
  minimum of selected SQ pending command count, device burst availability,
  tenant burst availability, and SQ burst availability.

### 4.4 Credit/QoS Interface

Fields:

- `tenant_alive`
- `read_iops_credit`
- `write_iops_credit`
- `read_bw_credit`
- `write_bw_credit`
- `eligible`

Timing:

- Credit checks are sampled during candidate evaluation.
- Credit consumption may be owned by Completion IP or by a shared QoS service.
  In this starter DLD, Arbitration IP performs eligibility check only.

## 5. Commands

Supported abstract command types:

- `READ`: consumes downstream read execution capacity.
- `WRITE`: consumes downstream write execution capacity.
- `FLUSH`: ordered command; may bypass normal data credits but cannot bypass
  ordering barriers.
- `ADMIN`: low-rate control command; may use strict priority or separate queue.

## 6. FSMs

### 6.1 Arbiter Main FSM

States:

- `RESET`: initialize pointers and counters.
- `IDLE`: no eligible command or downstream not ready.
- `PORT_SCAN`: inspect pending ports according to port policy.
- `TENANT_SCAN`: inspect tenants linked to selected port.
- `SQ_SCAN`: inspect SQs linked to selected tenant.
- `GRANT`: select one SQ and pass selected SQ metadata to issue pipeline.
- `STALL`: downstream backpressure or no eligible command.

Transitions:

- `RESET -> IDLE`: reset deasserted.
- `IDLE -> PORT_SCAN`: at least one port pending bit is set.
- `PORT_SCAN -> TENANT_SCAN`: eligible pending port found.
- `PORT_SCAN -> STALL`: no eligible port found.
- `TENANT_SCAN -> SQ_SCAN`: eligible pending tenant found.
- `TENANT_SCAN -> STALL`: no eligible tenant found under selected port.
- `SQ_SCAN -> GRANT`: eligible SQ with pending command found.
- `SQ_SCAN -> STALL`: no eligible SQ found under selected tenant.
- `GRANT -> IDLE`: selected SQ metadata accepted by issue pipeline.
- `STALL -> PORT_SCAN`: readiness or eligibility changes.

### 6.2 Policy Update FSM

States:

- `RESET`: initialize policy state.
- `WAIT_GRANT`: wait for a successful grant.
- `UPDATE_POINTER`: update round-robin pointer or weighted deficit.
- `UPDATE_AGE`: update age/starvation metadata.

### 6.3 Issue Pipeline FSM

States:

- `RESET`: clear selected SQ and burst calculation state.
- `WAIT_SELECTION`: wait for selected SQ from pipeline 1.
- `READ_PENDING_COUNT`: read command count for selected SQ.
- `READ_BURST`: read device, tenant, and SQ burst availability.
- `CALC_ISSUE_COUNT`: compute issue count as minimum available count.
- `ISSUE_REQUEST`: send selected SQ request to downstream IP.
- `ISSUE_STALL`: downstream not ready or computed issue count is zero.
- `UPDATE_BURST`: debit burst state after accepted issue.

Transitions:

- `WAIT_SELECTION -> READ_PENDING_COUNT`: selected SQ valid.
- `READ_PENDING_COUNT -> READ_BURST`: pending count is available.
- `READ_BURST -> CALC_ISSUE_COUNT`: burst state is available.
- `CALC_ISSUE_COUNT -> ISSUE_REQUEST`: issue count is greater than zero and
  downstream is ready.
- `CALC_ISSUE_COUNT -> ISSUE_STALL`: issue count is zero.
- `ISSUE_REQUEST -> UPDATE_BURST`: downstream accepts request.
- `ISSUE_STALL -> READ_PENDING_COUNT`: retry while selected SQ remains pending.
- `UPDATE_BURST -> WAIT_SELECTION`: burst counters debited.

## 7. Arbitration Policies

`ROUND_ROBIN`:

- At port level, visit pending ports in rotating order.
- At tenant level, visit pending tenants within the selected port in rotating
  order.
- At SQ level, visit pending SQs within the selected tenant in rotating order.
- Move the corresponding level pointer after a successful grant.

`WEIGHTED_ROUND_ROBIN`:

- Build an order list based on configured weights at each enabled level.
- Port weights are used only in dual-port mode.
- Tenant weights are based on tenant QoS allocation or configured priority.
- SQ weights are based on SQ class, priority, or configured service share.
- Existing QoS code uses a weighted tenant order derived from base read IOPS.
  Example tenant weights `[1, 4, 2, 1]` produce order
  `[T0, T1, T1, T1, T1, T2, T2, T3]`.

Starter weights:

| Level | Object | Weight | Notes |
| --- | --- | ---: | --- |
| Port | port0 | 1 | Single-port mode uses only port0. |
| Port | port1 | 1 | Dual-port mode default gives both ports equal service. |
| Tenant | T0 | 1 | Low-share tenant. |
| Tenant | T1 | 4 | High-share tenant. |
| Tenant | T2 | 2 | Medium-share tenant. |
| Tenant | T3 | 1 | Low-share tenant. |
| SQ | SQ0 | 4 | High-priority/default IO SQ. |
| SQ | SQ1 | 2 | Medium-priority IO SQ. |
| SQ | SQ2 | 1 | Low-priority/background SQ. |
| SQ | SQ3 | 1 | Low-priority/background SQ. |

`STRICT_PRIORITY`:

- Higher priority command class wins.
- Optional starvation guard raises old commands to eligible high-priority class.

## 8. Hierarchical Arbitration Data Model

The functional model shall represent the following topology:

```text
ports:
  port0:
    tenants: [T0, T1]
  port1:
    tenants: [T2, T3]

tenants:
  T0:
    sqs: [SQ0, SQ1]
  T1:
    sqs: [SQ0, SQ1, SQ2, SQ3]
  T2:
    sqs: [SQ0, SQ1]
  T3:
    sqs: [SQ0]
```

Pending bitmaps:

```text
port_pending_bitmap[port_id]
tenant_pending_bitmap[port_id][tenant_id]
sq_pending_bitmap[tenant_id][sq_id]
```

Bitmap rules:

- A SQ bit is set when the SQ has at least one pending command.
- A tenant bit is the OR-reduction of all SQ bits for that tenant, after
  applying tenant eligibility.
- A port bit is the OR-reduction of all tenant bits linked to that port.
- Arbitration must not scan a lower level if the upper-level pending bit is
  clear.
- Bitmap updates are functional state and must be visible in unit tests.

Command selection output:

```text
selected_port
selected_tenant
selected_sq
selected_cmd
```

No command may be issued unless all selected hierarchy levels have pending bits
set and the command is eligible.

## 9. Issue Pipeline Burst Data Model

Pipeline 2 receives the selected SQ tuple from pipeline 1:

```text
selected_port
selected_tenant
selected_sq
```

It then reads:

```text
pending_cmd_count[selected_port][selected_tenant][selected_sq]
device_burst_available
tenant_burst_available[selected_tenant]
sq_burst_available[selected_tenant][selected_sq]
```

The request size sent to the downstream IP is:

```text
issue_count = min(
  pending_cmd_count,
  device_burst_available,
  tenant_burst_available,
  sq_burst_available
)
```

Rules:

- If `issue_count == 0`, no command is popped and pipeline 2 stalls.
- If `issue_count > 0`, exactly `issue_count` commands are popped from the
  selected SQ and sent as one downstream request.
- Device burst, tenant burst, and SQ burst are each debited by `issue_count`.
- Tenant and SQ burst are independent; the smaller available burst wins.
- Device burst is global and limits all selected SQs.
- Pending bitmaps are updated after command pop.
- Pipeline 1 may continue selecting future SQs only when pipeline 2 can accept
  a new selected SQ descriptor.

## 10. Performance Model Requirements

The SimPy model shall include:

- One process for arbiter main loop.
- One process for issue pipeline.
- One process for policy/accounting update if modeled separately.
- `simpy.Store` for each SQ.
- `simpy.Resource` or custom slot counter for issue bandwidth.
- Port, tenant, and SQ pending bitmaps.
- Port-level arbitration process or stage.
- Tenant-level arbitration process or stage.
- SQ-level arbitration process or stage.
- Pending command count per SQ.
- Device-level, tenant-level, and SQ-level burst counters.
- Burst min calculation in the issue pipeline.
- Backpressure modeling through downstream ready/slot availability.
- Metrics for grants, stalls, no-eligible cycles, queue depth, selected tenant,
  selected port, selected tenant, selected SQ, issue count, burst stalls, and
  downstream requests.

The performance model shall not model command payload data.

## 11. SimPy Functionality Requirements

The generated SimPy delay model shall include enough functionality to validate
the FSM behavior from this DLD. The model must include:

- Arbiter configuration.
- Queue push/pop API.
- Topology configuration for port-to-tenant and tenant-to-SQ linkage.
- Deterministic hierarchical candidate selection.
- Pending bitmap update and query APIs.
- Burst configuration APIs at device, tenant, and SQ level.
- Issue request API carrying selected hierarchy and issue count.
- Policy state update.
- Command descriptor forwarding.
- Debug trace of each grant and stall reason.
- Explicit SimPy processes for arbiter main, issue pipeline, and policy update.
- Query APIs for pending bitmap and issued trace state.

## 12. FSM Timing Model

Clock assumption:

- Core clock frequency: 500 MHz.
- Cycle time: 2 ns.
- All delays below are hard-coded starter values for model generation.

FSM/process count:

- Total FSM/processes: 3.
- Total pipelines: 2.
- Pipeline 1 FSMs: `Arbiter Main FSM`, `Policy Update FSM`.
- Pipeline 2 FSMs: `Issue Pipeline FSM`.
- Parallel processes: `Arbiter Main FSM`, `Policy Update FSM`, and
  `Issue Pipeline FSM` run in parallel.
- Sequential dependency: pipeline 1 selected SQ output is consumed by pipeline
  2. Pipeline 2 computes issue count and sends the downstream request.

Per-process timing:

| FSM/process | Runs as | Delay model |
| --- | --- | --- |
| Arbiter Main FSM | Pipeline 1 process | Bitmap update visibility: 3 cycles = 6 ns. Port scan: 4 cycles = 8 ns. Tenant scan: 6 cycles = 12 ns. SQ scan: 8 cycles = 16 ns. Grant selected SQ: 2 cycles = 4 ns. |
| Policy Update FSM | Parallel helper process triggered by grant | Pointer update: 1 cycle = 2 ns. Age metadata update: 2 cycles = 4 ns. Weighted-order rebuild after config change: 8 cycles = 16 ns. |
| Issue Pipeline FSM | Pipeline 2 process | Selection accept: 1 cycle = 2 ns. Pending count read: 3 cycles = 6 ns. Burst read: 4 cycles = 8 ns. Min burst calculation: 2 cycles = 4 ns. Downstream issue request: 3 cycles = 6 ns. Burst debit: 2 cycles = 4 ns. Downstream backpressure retry interval: 1 cycle = 2 ns. |

End-to-end command issue delay:

- Pipeline 1 no stall, bitmaps already visible:
  `port scan 4 + tenant scan 6 + SQ scan 8 + grant selected SQ 2 = 20 cycles = 40 ns`.
- Pipeline 1 no stall, new command arrival requiring bitmap propagation:
  `bitmap update 3 + port scan 4 + tenant scan 6 + SQ scan 8 + grant selected SQ 2 = 23 cycles = 46 ns`.
- Pipeline 2 no stall after selected SQ arrives:
  `selection accept 1 + pending count read 3 + burst read 4 + min calculation 2 + issue request 3 + burst debit 2 = 15 cycles = 30 ns`.
- End-to-end no-stall selected SQ to downstream request:
  `pipeline 1 20 + pipeline 2 15 = 35 cycles = 70 ns`.
- With policy update included for next selection readiness:
  `20 + max(1, 2) = 22 cycles = 44 ns`.
- If downstream is not ready, add `1 cycle = 2 ns` per retry tick.

Sequential/parallel relationship:

```text
Parallel:
  Arbiter Main FSM: bitmap update -> port scan -> tenant scan -> SQ scan -> selected SQ
  Policy Update FSM: wait grant -> update port/tenant/SQ pointers -> update age
  Issue Pipeline FSM: selected SQ -> pending count -> burst read -> min calculation -> downstream request

Sequential dependency:
  SQ pending -> tenant pending -> port pending -> port selection
    -> tenant selection -> SQ selection -> selected SQ
    -> pending count and burst calculation -> downstream request
  grant accepted -> policy update visible to next scan
```

## 13. Open Items

- Exact number of ingress sources.
- Whether credit consumption happens in Arbitration IP or Completion IP.
- Flush/admin ordering rules.
- Per-clock issue width.
- Starvation guard threshold.
- Exact production port-to-tenant mapping.
- Exact production tenant-to-SQ mapping.
- Exact device, tenant, and SQ burst refill rules.
- Whether pipeline 1 can queue multiple selected SQ descriptors while pipeline
  2 is busy.
