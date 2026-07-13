# DMA Subsystem Design-Level Document

## 1. Purpose

The DMA Subsystem is a connected data-mover cluster built from four existing IPs: the GDMA engine, the AXI interconnect, the arbitration IP, and the completion IP. A host submits DMA descriptors; the GDMA engine processes them while the subsystem converts each descriptor into fabric read/write transactions that are routed by the interconnect, scheduled onto the downstream target by the arbitration IP, and accounted through the completion IP's QoS token path. A descriptor is subsystem-complete only when both the engine leg and the fabric leg have finished, at which point the subsystem raises a completion interrupt.

The performance model must represent the concurrent member IPs and the glue processes that connect them, including cross-IP backpressure: fabric congestion must throttle the DMA engine, and completion-queue backlog must throttle arbitration issue.

## 2. Scope

In scope:

- Descriptor intake and fan-out to the GDMA engine and the fabric path.
- Conversion of descriptors into per-descriptor read and write fabric commands.
- Bridging interconnect responses into the arbitration IP's port/tenant/SQ queues.
- Dispatching arbitration-issued commands into the completion IP's QoS path.
- Collecting engine-leg and fabric-leg completion and raising a subsystem interrupt.
- Cross-IP backpressure coupling (fabric congestion to engine readiness, completion backlog to arbitration issue readiness).

Out of scope:

- Internal behavior of the four member IPs (defined by their own DLDs and reviewed templates).
- Cache coherency and address translation.
- Real interrupt controller delivery (the subsystem exposes a level event only).
- Data payload contents.

## 3. Context

SoC data movers rarely operate in isolation: a DMA engine issues transactions into a shared fabric, contends with other masters, and its completions are policed by QoS accounting before software is notified. Modeling the connected cluster exposes behavior invisible in single-IP models: end-to-end descriptor latency through four IPs, fabric backpressure stalling the engine, and QoS token exhaustion delaying completion interrupts.

The subsystem treats the four member IP models as internal resources. Its own processes are glue: they move transactions between member IP boundaries and observe member state to couple backpressure. Some glue processes are naturally parallel: descriptor intake, fabric bridging, downstream dispatch, completion collection, and backpressure monitoring all run concurrently. Some operations are sequential: a descriptor's fabric commands are created after the descriptor is accepted; a fabric response is bridged to arbitration only after the interconnect routes it; a command enters the completion QoS path only after arbitration issues it; the subsystem interrupt fires only after both legs complete.

## 4. Interfaces

### 4.1 Host Descriptor Interface

Fields:

- `desc_id`
- `channel_id`
- `src_addr`
- `dst_addr`
- `length_bytes`

Timing:

- A descriptor is accepted immediately into the intake queue; downstream stalls surface as increased end-to-end latency, not intake rejection.

### 4.2 Subsystem Interrupt Interface

Fields:

- `irq_valid`
- `channel_id`

Timing:

- The completion interrupt for a descriptor fires only after both the engine leg and the fabric leg of that descriptor have completed.

### 4.3 QoS Configuration Interface

Fields:

- `tenant_id`
- `token_budget`

Timing:

- QoS token configuration takes effect before subsequent completion scheduling decisions.

## 5. Descriptor Flow

`INTAKE`:

- Host descriptor is accepted, forwarded to the GDMA engine, and converted into one read and one write fabric command.

`FABRIC_ROUTE`:

- The interconnect decodes and routes each fabric command to the downstream slave and produces a response.

`DOWNSTREAM_SCHEDULE`:

- Routed commands are enqueued into the arbitration IP and issued to the downstream target under its port/tenant/SQ policy.

`QOS_ACCOUNT`:

- Issued commands pass through the completion IP's token accounting and emerge as accounted completions.

`COLLECT`:

- When a descriptor's engine leg and both fabric completions are observed, the subsystem marks it complete and raises the interrupt.

## 6. FSMs and Processes

### 6.1 Host Command FSM

Role: Accept host descriptors, forward them to the GDMA engine, and create the descriptor's read and write fabric commands.

States:

- `IDLE`
- `ACCEPT_DESCRIPTOR`
- `FORWARD_ENGINE`
- `BUILD_FABRIC_COMMANDS`
- `SUBMIT_FABRIC`

Sequential dependencies:

- A descriptor is forwarded to the engine before its fabric commands are submitted.
- Fabric commands are created only for accepted descriptors.

Parallel interaction:

- Runs concurrently with all member IPs and other glue processes.

### 6.2 Fabric Bridge FSM

Role: Observe interconnect responses and enqueue each routed command into the arbitration IP's queues.

States:

- `IDLE`
- `POLL_RESPONSES`
- `MAP_TENANT`
- `ENQUEUE_ARBITRATION`

Sequential dependencies:

- A command is bridged only after the interconnect produces its response.
- Tenant mapping (channel to tenant) occurs before arbitration enqueue.

Parallel interaction:

- Runs concurrently with interconnect routing and arbitration scanning.

### 6.3 Downstream Dispatch FSM

Role: Observe arbitration-issued commands and submit them into the completion IP's QoS path.

States:

- `IDLE`
- `POLL_ISSUED`
- `SUBMIT_COMPLETION`

Sequential dependencies:

- A command enters the completion path only after arbitration issues it.

Parallel interaction:

- Runs concurrently with the arbitration issue pipeline and completion scheduling.

### 6.4 Completion Collector FSM

Role: Match accounted completions and engine completions to descriptors, mark descriptors complete, and raise the subsystem interrupt.

States:

- `IDLE`
- `POLL_COMPLETIONS`
- `MATCH_DESCRIPTOR`
- `MARK_COMPLETE`
- `ASSERT_IRQ`

Sequential dependencies:

- A descriptor is marked complete only when its engine leg and both fabric completions are observed.
- The interrupt is asserted only after the descriptor is marked complete.

Parallel interaction:

- Observes the completion IP and GDMA engine concurrently.

### 6.5 Backpressure Monitor FSM

Role: Mirror fabric congestion into GDMA memory readiness and completion backlog into arbitration issue readiness.

States:

- `IDLE`
- `SAMPLE_FABRIC`
- `APPLY_ENGINE_THROTTLE`
- `SAMPLE_COMPLETION_BACKLOG`
- `APPLY_ISSUE_THROTTLE`

Sequential dependencies:

- Congestion is sampled before a throttle is applied.

Parallel interaction:

- Observes and steers member IPs concurrently with all data-path glue.

## 7. Process Relationship Summary

Sequential descriptor path:

```text
Host Descriptor
  -> Host Command (engine forward + fabric commands)
  -> AXI Interconnect (route)          [member IP]
  -> Fabric Bridge (map + enqueue)
  -> Arbitration IP (schedule + issue) [member IP]
  -> Downstream Dispatch (submit)
  -> Completion IP (QoS account)       [member IP]
  -> Completion Collector (match + IRQ)
GDMA engine leg                        [member IP]
  -> Completion Collector (match + IRQ)
```

Parallel processes:

```text
Host Command
Fabric Bridge
Downstream Dispatch
Completion Collector
Backpressure Monitor
GDMA / Interconnect / Arbitration / Completion member processes
```

## 8. Resources and Queues

Queues/events:

- Descriptor intake queue.
- Per-descriptor fabric command tracking table.
- Pending completion tracking table.

Resources:

- GDMA engine model instance.
- AXI interconnect model instance.
- Arbitration IP model instance.
- Completion IP model instance.
- Subsystem interrupt line.

## 9. Performance Model Requirements

The SimPy model shall include:

- A host-command process that forwards descriptors and creates fabric commands.
- A fabric-bridge process from interconnect responses to arbitration queues.
- A downstream-dispatch process from arbitration issue to completion submission.
- A completion-collector process with per-descriptor leg matching and interrupt assertion.
- A backpressure-monitor process coupling member congestion states.
- Metrics for descriptors submitted, fabric commands issued, arbitration enqueues, completions collected, descriptors completed, subsystem interrupts, fabric backpressure events, completion backlog events, and end-to-end descriptor latency.

## 10. SimPy Functionality Requirements

The generated SimPy delay model shall include:

- Instantiation of the four member IP models with configurable parameters.
- Descriptor-to-fabric-command conversion (one read and one write per descriptor).
- Channel-to-tenant mapping for arbitration and completion QoS.
- Both-legs completion matching before the subsystem interrupt.
- Fabric-congestion-to-engine and backlog-to-arbitration backpressure coupling.

All generated timing values must come from this DLD and the reviewed template. Member IP internal timing comes from the member IPs' own reviewed templates.

## 11. FSM Timing Model

Clock assumption:

- Bus clock frequency: 500 MHz.
- Cycle time: 2 ns.
- All delays below are hard-coded starter values for the glue processes only; member IP timing is defined by the member IP templates.

FSM/process count:

- Total FSM/processes: 5.
- Parallel processes: host command, fabric bridge, downstream dispatch, completion collector, and backpressure monitor (plus the member IPs' own processes).
- Sequential dependency: engine forward precedes fabric submit; route precedes bridge; issue precedes completion submit; both legs precede interrupt.

Per-process timing:

| FSM/process | Runs as | Delay model |
| --- | --- | --- |
| Host Command FSM | Parallel intake process | Accept descriptor: 1 cycle = 2 ns. Forward engine: 1 cycle = 2 ns. Submit fabric: 1 cycle = 2 ns. |
| Fabric Bridge FSM | Parallel bridge process | Poll responses: 1 cycle = 2 ns. Map tenant: 1 cycle = 2 ns. Enqueue arbitration: 1 cycle = 2 ns. |
| Downstream Dispatch FSM | Parallel bridge process | Poll issued: 1 cycle = 2 ns. Submit completion: 1 cycle = 2 ns. |
| Completion Collector FSM | Parallel collector process | Poll completions: 1 cycle = 2 ns. Match descriptor: 1 cycle = 2 ns. Assert IRQ: 1 cycle = 2 ns. |
| Backpressure Monitor FSM | Parallel monitor process | Sample fabric: 2 cycles = 4 ns. Apply throttle: 1 cycle = 2 ns. |

End-to-end delay:

- Glue overhead per descriptor (excluding member IP internal latency):
  `accept 1 + forward 1 + submit 1 + poll 1 + map 1 + enqueue 1 + poll 1 + submit 1 + poll 1 + match 1 + irq 1 = 11 cycles = 22 ns`.
- Total end-to-end descriptor latency is dominated by member IP internal timing (GDMA pipeline, interconnect routing, arbitration scan/issue, completion token scheduling) and is a measured output of the model, not a fixed constant.

Sequential/parallel relationship:

```text
Sequential descriptor path:
  host descriptor -> engine forward + fabric commands -> interconnect route
    -> arbitration enqueue -> arbitration issue -> completion QoS
    -> both-legs match -> subsystem IRQ

Parallel:
  host command
  fabric bridge
  downstream dispatch
  completion collector
  backpressure monitor
  all member IP processes
```

## 12. Open Items

- Depth of the descriptor intake queue.
- Whether a descriptor should generate more than one read/write fabric command pair for large transfers (segmentation policy).
- Channel-to-tenant mapping table size (currently channel N maps to tenant TN).
- Backpressure thresholds (fabric outstanding limit fraction, completion backlog depth).
- Whether the subsystem interrupt supports coalescing across descriptors.
