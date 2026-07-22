# Interrupt Controller IP Design-Level Document

## 1. Purpose

The Interrupt Controller IP receives interrupt events from SoC peripherals,
applies enable/mask/pending/priority logic, arbitrates among active interrupts,
and delivers interrupt notifications to one or more CPU targets. It also tracks
acknowledge, completion/end-of-interrupt, masking, nesting, and optional
software-generated interrupts.

The performance model must represent multiple concurrent processes:

- Interrupt source sampling.
- Pending bit update.
- Mask/enable filtering.
- Priority resolution.
- CPU target delivery.
- CPU acknowledge and end-of-interrupt handling.
- Register access.

Some operations are sequentially dependent, such as source capture before
pending update and priority resolution before CPU delivery. Other operations
run in parallel, such as source sampling across interrupt lines and independent
CPU target delivery.

## 2. Scope

In scope:

- Multiple interrupt sources.
- Level and edge triggered interrupts.
- Pending, active, mask, and enable state.
- Fixed priority or programmable priority.
- Per-target CPU interrupt outputs.
- Interrupt acknowledge and end-of-interrupt.
- Software-generated interrupts.
- Optional interrupt grouping.
- Optional wakeup output.

Out of scope:

- Full CPU exception entry behavior.
- GIC architectural register compatibility.
- Security state partitioning unless later added.
- Physical interrupt pin electrical behavior.

## 3. Context

In a SoC, peripherals assert interrupt events to notify CPUs about work,
errors, timers, DMA completions, or external signals. The interrupt controller
centralizes these events, filters disabled sources, chooses the highest-priority
eligible interrupt, and delivers it to the configured CPU target.

The model does not need to execute CPU software. It needs to model latency from
interrupt assertion to CPU-visible interrupt, effects of masking, and behavior
of acknowledge/end-of-interrupt flows.

## 4. Interfaces

Every interface below states a `Wait model:` block — how the requester (IP1) waits
on the responder (IP2) across that interface: `wait_for_response` (blocks until the
response returns and uses the result), `wait_for_ack_inline` (blocks at the request
site for an ack, then continues the same pipeline), or
`wait_for_ack_before_next_request` (continues after issuing; the ack is collected
before the next command starts). An interface that does not state one is read as
`wait_for_ack_inline`.

### 4.1 Source Interrupt Interface

One logical input per interrupt source.

Fields:

- `src_id`
- `src_valid`
- `trigger_type`: `LEVEL` or `EDGE`.
- `level_value`
- `edge_pulse`
- `source_group`

Timing:

- Edge sources are latched into pending state on pulse.
- Level sources remain pending while level is asserted, unless masked.

Wait model:

- Mode: `wait_for_ack_inline`
- Requester: peer
- Waits in: `source_sampling.QUEUE_EVENT`
- Resumes on: `event_latched`
- Note: a source consumes no result: an edge is latched, a level stays asserted until cleared

### 4.2 CPU Target Interface

One logical output per CPU target.

Fields:

- `irq_valid`
- `irq_id`
- `irq_priority`
- `irq_group`
- `irq_level`
- `target_id`

Timing:

- Interrupt remains visible while a pending eligible interrupt exists.
- Acknowledge moves selected interrupt from pending to active depending on
  configured mode.

Wait model:

- Mode: `wait_for_ack_before_next_request`
- Requester: this IP
- Waits in: `cpu_delivery.WAIT_ACK`
- Resumes on: `cpu_acknowledge`
- Outstanding limit: 1
- Note: per target one interrupt is in flight: delivery is complete once asserted, but the acknowledge must arrive before the next interrupt is delivered

### 4.3 CPU Acknowledge/EOI Interface

Fields:

- `ack_valid`
- `ack_target_id`
- `ack_irq_id`
- `eoi_valid`
- `eoi_target_id`
- `eoi_irq_id`

Timing:

- Acknowledge returns the currently selected interrupt.
- End-of-interrupt clears active state and allows lower-priority pending
  interrupts to be delivered.

Wait model:

- Mode: `wait_for_response`
- Requester: peer
- Waits in: `acknowledge.RETURN_ACK_ID`
- Resumes on: `ack_id_returned`
- Response used for: the CPU consumes the returned IRQ id to run the matching handler
- Note: EOI carries no result and completes in `end_of_interrupt.EOI_DONE`

### 4.4 Register Interface

Important registers:

- `ENABLE_SET`
- `ENABLE_CLEAR`
- `PENDING_SET`
- `PENDING_CLEAR`
- `ACTIVE_STATUS`
- `PRIORITY[src]`
- `TARGET[src]`
- `CONFIG[src]`
- `MASK[target]`
- `THRESHOLD[target]`
- `SOFT_INT`

Wait model:

- Mode: `wait_for_response`
- Requester: peer
- Waits in: `register_access.READ_STATE`
- Resumes on: `register_access_complete`
- Response used for: software reads pending/active/priority state before deciding what to write

## 5. Interrupt Types

`EDGE_TRIGGERED`:

- A pulse sets pending state.
- Pending remains until acknowledged or explicitly cleared.

`LEVEL_TRIGGERED`:

- Source level sets pending while asserted.
- Pending can reappear after EOI if the level is still asserted.

`SOFTWARE_GENERATED`:

- Register write sets pending for a selected source and target.

`WAKEUP`:

- Optional event that may bypass normal CPU delivery masking for low-power
  wakeup indication.

## 6. FSMs and Processes

### 6.1 Source Sampling FSM

Role: Sample raw interrupt source inputs and generate source events.

States:

- `RESET`
- `SAMPLE_SOURCES`
- `DETECT_EDGE`
- `DETECT_LEVEL`
- `QUEUE_EVENT`
- `SOURCE_STALL`

Sequential dependencies:

- Raw source sample precedes edge/level detect.
- Event queue update follows trigger detection.

Parallel interaction:

- May be modeled as one process scanning all sources.
- May be modeled as one process per source for higher concurrency.

### 6.2 Pending Update FSM

Role: Maintain pending bits based on source events and software writes.

States:

- `RESET`
- `WAIT_EVENT`
- `SET_PENDING`
- `CLEAR_PENDING`
- `REASSERT_LEVEL`
- `PENDING_UPDATE_DONE`

Sequential dependencies:

- Source event or software event precedes pending update.
- Level reassertion is evaluated after EOI/clear.

Parallel interaction:

- Runs in parallel with priority resolution.
- Priority resolver observes stable pending snapshots.

### 6.3 Mask/Enable Filter FSM

Role: Compute eligible interrupts from pending, enable, mask, and threshold
state.

States:

- `WAIT_PENDING_CHANGE`
- `APPLY_ENABLE`
- `APPLY_TARGET_MASK`
- `APPLY_PRIORITY_THRESHOLD`
- `PUBLISH_ELIGIBLE_SET`

Sequential dependencies:

- Enable filtering precedes target mask filtering.
- Threshold filtering follows priority lookup.

Parallel interaction:

- May run per CPU target.
- Runs in parallel with source sampling and register access.

### 6.4 Priority Resolver FSM

Role: Select highest-priority eligible interrupt per CPU target.

States:

- `IDLE`
- `SCAN_ELIGIBLE`
- `COMPARE_PRIORITY`
- `SELECT_WINNER`
- `NO_ELIGIBLE`

Sequential dependencies:

- Eligible set publication precedes priority comparison.
- Winner selection precedes CPU delivery.

Parallel interaction:

- One resolver may run per CPU target.
- Multiple targets can resolve independently.

### 6.5 CPU Delivery FSM

Role: Drive interrupt outputs to CPU targets.

States:

- `IDLE`
- `ASSERT_IRQ`
- `WAIT_ACK`
- `HOLD_LEVEL`
- `DELIVERY_STALL`

Sequential dependencies:

- Priority winner must exist before `ASSERT_IRQ`.
- Acknowledge follows CPU-visible interrupt.

Parallel interaction:

- One delivery FSM per CPU target.

### 6.6 Acknowledge FSM

Role: Handle CPU acknowledge and move interrupt from pending to active.

States:

- `WAIT_ACK`
- `LOOKUP_SELECTED_IRQ`
- `CLEAR_OR_HOLD_PENDING`
- `SET_ACTIVE`
- `RETURN_ACK_ID`
- `ACK_ERROR`

Sequential dependencies:

- CPU acknowledge triggers selected IRQ lookup.
- Active state update precedes ACK ID return.

Parallel interaction:

- Runs in parallel with source sampling.
- Must coordinate with pending update for level-triggered sources.

### 6.7 End-Of-Interrupt FSM

Role: Clear active state and re-evaluate level-triggered interrupts.

States:

- `WAIT_EOI`
- `LOOKUP_ACTIVE`
- `CLEAR_ACTIVE`
- `CHECK_LEVEL_REASSERT`
- `EOI_DONE`
- `EOI_ERROR`

Sequential dependencies:

- Active clear precedes level reassertion check.
- New priority resolution follows EOI completion.

Parallel interaction:

- Runs in parallel with pending update and priority resolution.

### 6.8 Register Access FSM

Role: Accept software register reads/writes.

States:

- `RESET`
- `IDLE`
- `DECODE_ACCESS`
- `WRITE_STATE`
- `READ_STATE`
- `ACCESS_ERROR`

Sequential dependencies:

- Register writes update controller state before eligibility recomputation.

Parallel interaction:

- Runs independently from source sampling.
- Can cause pending, enable, mask, or priority changes while interrupts are
  active.

### 6.9 Software Interrupt FSM

Role: Convert software-generated interrupt writes into pending events.

States:

- `WAIT_SOFT_INT_WRITE`
- `VALIDATE_TARGET`
- `GENERATE_SOFT_EVENT`
- `SOFT_INT_ERROR`

Sequential dependencies:

- Target validation precedes pending event generation.

Parallel interaction:

- Runs in parallel with external source sampling.

## 7. Process Relationship Summary

Sequential delivery path:

```text
Source Sample
  -> Edge/Level Detect
  -> Pending Update
  -> Mask/Enable Filter
  -> Priority Resolution
  -> CPU IRQ Assert
  -> CPU Acknowledge
  -> Active State Update
  -> End Of Interrupt
  -> Optional Level Reassert
```

Parallel processes:

```text
Source Sampling
Pending Update
Mask/Enable Filter per target
Priority Resolver per target
CPU Delivery per target
Acknowledge Handling
End-Of-Interrupt Handling
Register Access
Software Interrupt Generation
```

## 8. Resources and Queues

Queues/events:

- Source event queue.
- Software interrupt event queue.
- Pending update queue.
- Per-target eligible interrupt set.
- CPU acknowledge queue.
- EOI queue.

Resources:

- Register bus access port.
- Pending state array.
- Active state array.
- Priority comparator.
- CPU interrupt output line per target.

## 9. Performance Model Requirements

The SimPy model shall include:

- Source sampling process.
- Pending update process.
- Mask/enable filter process.
- Priority resolver process per CPU target.
- CPU delivery process per CPU target.
- ACK and EOI handling processes.
- Register access process.
- Software interrupt process.
- Metrics for source-to-pending latency, pending-to-delivery latency, ACK
  latency, EOI latency, priority arbitration count, masked interrupt count, and
  reasserted level interrupt count.

The performance model shall represent edge and level trigger behavior without
modeling CPU instruction execution.

## 10. SimPy Functionality Requirements

The generated SimPy delay model shall include enough functionality to validate
the FSM behavior from this DLD. The model must include:

- Register model.
- Pending/active/enable/mask state.
- Priority selection.
- Target routing.
- Acknowledge and EOI APIs.
- Edge and level source update APIs.
- Software interrupt injection API.
- Level-triggered reassertion after EOI.
- Masked interrupt stall/accounting behavior.
- Queue handoff between source sampling, pending update, filtering, priority,
  delivery, ACK, and EOI processes.

## 11. FSM Timing Model

Clock assumption:

- Interrupt controller clock frequency: 500 MHz.
- Cycle time: 2 ns.
- All delays below are hard-coded starter values for model generation.

FSM/process count:

- Total FSM/processes: 9.
- Parallel processes: source sampling, pending update, mask/enable filtering,
  priority resolver per target, CPU delivery per target, acknowledge,
  end-of-interrupt, register access, and software interrupt generation.
- Sequential dependency: source event must update pending state before
  filtering and priority resolution; selected interrupt must be delivered
  before CPU acknowledge; EOI clears active state before level reassertion.

Per-process timing:

| FSM/process | Runs as | Delay model |
| --- | --- | --- |
| Source Sampling FSM | Parallel scanner or per-source process | Source sample: 2 cycles = 4 ns. Edge/level detect: 2 cycles = 4 ns. Queue event: 1 cycle = 2 ns. |
| Pending Update FSM | Parallel state-update process | Set/clear pending: 3 cycles = 6 ns. Level reassert check: 3 cycles = 6 ns. |
| Mask/Enable Filter FSM | Parallel per-target filter | Apply enable: 2 cycles = 4 ns. Apply target mask: 2 cycles = 4 ns. Apply threshold: 2 cycles = 4 ns. Publish eligible set: 1 cycle = 2 ns. |
| Priority Resolver FSM | Parallel per-target resolver | Scan eligible set: 8 cycles = 16 ns. Compare priorities: 4 cycles = 8 ns. Select winner: 1 cycle = 2 ns. |
| CPU Delivery FSM | Parallel per-target delivery process | Assert IRQ: 2 cycles = 4 ns. Delivery visible to CPU: 2 cycles = 4 ns. Hold-level retry/check: 1 cycle = 2 ns. |
| Acknowledge FSM | Parallel CPU response process | ACK lookup: 3 cycles = 6 ns. Pending clear/hold: 2 cycles = 4 ns. Set active: 2 cycles = 4 ns. Return ACK ID: 2 cycles = 4 ns. |
| End-Of-Interrupt FSM | Parallel CPU response process | Active lookup: 3 cycles = 6 ns. Clear active: 2 cycles = 4 ns. Level reassert check: 3 cycles = 6 ns. |
| Register Access FSM | Parallel bus process | Decode access: 2 cycles = 4 ns. Write/read state: 2 cycles = 4 ns. Access error: 2 cycles = 4 ns. |
| Software Interrupt FSM | Parallel software event process | Validate target: 2 cycles = 4 ns. Generate software event: 2 cycles = 4 ns. |

End-to-end interrupt delay:

- External source assert to CPU-visible IRQ:
  `source sample/detect/queue 5 + pending update 3 + filter 7 + priority 13 + delivery 4 = 32 cycles = 64 ns`.
- CPU acknowledge path:
  `ACK lookup 3 + pending clear/hold 2 + set active 2 + return ACK 2 = 9 cycles = 18 ns`.
- EOI path:
  `active lookup 3 + clear active 2 + level reassert check 3 = 8 cycles = 16 ns`.
- Software interrupt to CPU-visible IRQ:
  `software validate/generate 4 + pending update 3 + filter 7 + priority 13 + delivery 4 = 31 cycles = 62 ns`.

Sequential/parallel relationship:

```text
Sequential delivery path:
  source sample -> pending update -> mask/enable filter
    -> priority resolve -> CPU delivery -> ACK -> active state -> EOI

Parallel:
  source sampling
  register access
  software interrupt generation
  per-target filter/resolver/delivery
  ACK and EOI handling
```

## 12. Open Items

- Number of interrupt sources.
- Number of CPU targets.
- Priority width.
- Trigger type reset defaults.
- Whether nested interrupts are supported.
- Whether level interrupts remain pending after ACK or reassert after EOI.
- Wakeup bypass policy.
