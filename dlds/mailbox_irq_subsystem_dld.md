# Mailbox IRQ Subsystem Design-Level Document

## 1. Purpose

The Mailbox IRQ Subsystem is a connected interrupt-delivery cluster built from two existing IPs: the Mailbox IP as the interrupt source and the Interrupt Controller IP as the delivery fabric. A sender agent writes messages into mailbox channels; each doorbell becomes a level-triggered interrupt source at the controller, which prioritizes and delivers it to a CPU. A modeled software handler acknowledges the delivery, reads the message, clears the doorbell, and signals end-of-interrupt, closing the full message-to-EOI round trip.

The performance model must represent the concurrent member IPs and the glue processes that connect them, including level-triggered semantics (a doorbell stays asserted while unserviced messages remain, so end-of-interrupt re-pends the source) and interrupt-storm throttling (a flooding channel is masked at the controller until it drains).

## 2. Scope

In scope:

- Message intake and forwarding to mailbox channels.
- Bridging mailbox doorbell interrupts to controller level-triggered sources.
- CPU delivery acknowledgement and the software service loop (read, clear, EOI).
- Level reassert behavior when a channel still holds unserviced messages at EOI.
- Interrupt-storm throttling by masking a flooding source at the controller.

Out of scope:

- Internal behavior of the two member IPs (defined by their own DLDs and reviewed templates).
- Real CPU instruction execution (the software handler is a fixed-latency model).
- Inter-processor mailbox topologies beyond one sender and one receiver CPU.
- Message payload interpretation.

## 3. Context

Doorbell interrupts are the canonical mailbox use: a producer writes a message, the consumer is interrupted, reads the message, clears the doorbell, and completes the interrupt. Modeling the connected cluster exposes behavior invisible in single-IP models: message-to-EOI round-trip latency through both IPs, pending collapse (multiple doorbells on one channel collapse to one pending controller source, recovered by level reassert at EOI), and storm scenarios where one flooding channel is masked so it cannot starve the system.

The subsystem treats the two member IP models as internal resources. Its own processes are glue: they move events between member boundaries and model the CPU-side service loop. Some glue processes are naturally parallel: message intake, interrupt bridging, CPU service, software handling, and storm monitoring all run concurrently. Some operations are sequential: a doorbell is bridged only after the mailbox asserts it; the CPU acknowledges only after the controller delivers; the software handler reads and clears only after acknowledgement; EOI follows service completion.

## 4. Interfaces

### 4.1 Host Message Interface

Fields:

- `channel_id`
- `message`

Timing:

- A message is accepted into the intake queue immediately; mailbox FIFO backpressure applies inside the member IP.

### 4.2 CPU Service Interface

Fields:

- `target_id`
- `src_id`

Timing:

- Acknowledge latency and software service latency are modeled as fixed glue delays plus member IP latencies.

### 4.3 Storm Control Interface

Fields:

- `src_id`
- `masked`
- `storm_limit`
- `storm_window`

Timing:

- A source is masked when its outstanding message count reaches the storm limit and unmasked after the throttle window elapses; it is re-masked if it is still flooding (duty-cycle throttling). The release is time-windowed because the controller's pending set collapses a flooding source to one in-flight delivery at a time, so a masked source cannot drain below the limit on its own.

## 5. Interrupt Flow

`SEND`:

- Host message is forwarded into the mailbox channel FIFO, raising a doorbell.

`BRIDGE`:

- The mailbox doorbell interrupt is consumed and asserted as a level-triggered source at the interrupt controller.

`DELIVER`:

- The controller filters, prioritizes, and delivers the source to the CPU target.

`SERVICE`:

- The CPU acknowledges; the software handler reads the message, clears the doorbell when the channel is drained, and issues end-of-interrupt.

`REASSERT`:

- If unserviced messages remain at EOI, the still-asserted level re-pends the source for another delivery round.

`THROTTLE`:

- A channel whose outstanding count reaches the storm limit is masked at the controller for a throttle window, then unmasked and re-masked if still flooding.

## 6. FSMs and Processes

### 6.1 Host Message FSM

Role: Accept host messages and forward them to the mailbox sender interface.

States:

- `IDLE`
- `ACCEPT_MESSAGE`
- `FORWARD_MAILBOX`

Sequential dependencies:

- A message is forwarded only after it is accepted and timestamped for latency tracking.

Parallel interaction:

- Runs concurrently with all member IP processes and other glue processes.

### 6.2 Irq Source Bridge FSM

Role: Consume mailbox doorbell interrupts and assert the mapped level-triggered source at the interrupt controller.

States:

- `IDLE`
- `CONSUME_MAILBOX_IRQ`
- `MAP_SOURCE`
- `ASSERT_LEVEL`

Sequential dependencies:

- A source is asserted only after the mailbox raises the doorbell interrupt.
- Channel-to-source mapping occurs before level assertion.

Parallel interaction:

- Runs concurrently with mailbox interrupt notification and controller source sampling.

### 6.3 Cpu Service FSM

Role: Observe controller deliveries and issue the CPU acknowledge.

States:

- `IDLE`
- `POLL_DELIVERED`
- `ISSUE_ACK`

Sequential dependencies:

- An acknowledge is issued only after the controller delivers the source.

Parallel interaction:

- Runs concurrently with controller delivery and the software handler.

### 6.4 Software Handler FSM

Role: Service acknowledged interrupts: read the message, clear the doorbell when the channel drains, and issue end-of-interrupt.

States:

- `IDLE`
- `POLL_ACKED`
- `READ_MESSAGE`
- `CLEAR_DOORBELL`
- `ISSUE_EOI`

Sequential dependencies:

- The message is read only after acknowledgement.
- The doorbell is cleared and the level deasserted only when no unserviced messages remain on the channel.
- End-of-interrupt follows service completion; a still-asserted level re-pends the source.

Parallel interaction:

- Runs concurrently with the CPU service process and the storm monitor.

### 6.5 Storm Monitor FSM

Role: Mask a flooding source at the controller when its outstanding count reaches the storm limit and unmask it after the throttle window elapses, re-masking if it is still flooding.

States:

- `IDLE`
- `SAMPLE_OUTSTANDING`
- `APPLY_MASK`
- `RELEASE_MASK`

Sequential dependencies:

- Outstanding counts are sampled before a mask is applied.
- A mask is released only after the throttle window elapses.

Parallel interaction:

- Observes the bridge and handler bookkeeping concurrently with all data-path glue.

## 7. Process Relationship Summary

Sequential interrupt path:

```text
Host Message
  -> Host Message (forward)
  -> Mailbox IP (push, doorbell, notify)      [member IP]
  -> Irq Source Bridge (consume + assert level)
  -> Interrupt Controller IP (pend, filter, prioritize, deliver) [member IP]
  -> Cpu Service (ack)
  -> Software Handler (read, clear, EOI)
  -> level reassert if messages remain
```

Parallel processes:

```text
Host Message
Irq Source Bridge
Cpu Service
Software Handler
Storm Monitor
Mailbox / Interrupt Controller member processes
```

## 8. Resources and Queues

Queues/events:

- Message intake queue.
- Per-channel outstanding message counters.
- Per-channel send timestamp queues for latency tracking.

Resources:

- Mailbox IP model instance.
- Interrupt controller IP model instance.
- CPU service context.

## 9. Performance Model Requirements

The SimPy model shall include:

- A host-message process that forwards messages and timestamps them.
- An interrupt-bridge process from mailbox doorbells to controller level sources.
- A CPU-service process that acknowledges deliveries.
- A software-handler process with read, conditional clear/deassert, and EOI.
- A storm-monitor process with mask at the storm limit and time-windowed unmask.
- Metrics for messages sent, interrupts bridged, deliveries observed, acknowledges issued, messages serviced, EOIs issued, level reasserts used, storm throttle events, and message-to-EOI round-trip latency.

## 10. SimPy Functionality Requirements

The generated SimPy delay model shall include:

- Instantiation of the two member IP models with configurable parameters.
- Channel-to-source mapping (channel N maps to controller source N).
- Level-triggered assert/deassert coupling with conditional doorbell clear.
- Outstanding-count bookkeeping shared by the bridge, handler, and monitor.
- Storm masking at the controller with time-windowed release.

All generated timing values must come from this DLD and the reviewed template. Member IP internal timing comes from the member IPs' own reviewed templates.

## 11. FSM Timing Model

Clock assumption:

- Bus clock frequency: 500 MHz.
- Cycle time: 2 ns.
- All delays below are hard-coded starter values for the glue processes only; member IP timing is defined by the member IP templates.

FSM/process count:

- Total FSM/processes: 5.
- Parallel processes: host message, irq source bridge, cpu service, software handler, and storm monitor (plus the member IPs' own processes).
- Sequential dependency: doorbell precedes bridge; delivery precedes ack; ack precedes service; service precedes EOI.

Per-process timing:

| FSM/process | Runs as | Delay model |
| --- | --- | --- |
| Host Message FSM | Parallel intake process | Accept message: 1 cycle = 2 ns. Forward mailbox: 1 cycle = 2 ns. |
| Irq Source Bridge FSM | Parallel bridge process | Consume mailbox irq: 1 cycle = 2 ns. Map source: 1 cycle = 2 ns. Assert level: 1 cycle = 2 ns. |
| Cpu Service FSM | Parallel service process | Poll delivered: 1 cycle = 2 ns. Issue ack: 1 cycle = 2 ns. |
| Software Handler FSM | Parallel handler process | Poll acked: 1 cycle = 2 ns. Read message: 2 cycles = 4 ns. Issue eoi: 1 cycle = 2 ns. |
| Storm Monitor FSM | Parallel monitor process | Sample outstanding: 2 cycles = 4 ns. Apply mask: 1 cycle = 2 ns. |

End-to-end delay:

- Glue overhead per message (excluding member IP internal latency):
  `accept 1 + forward 1 + consume 1 + map 1 + assert 1 + poll 1 + ack 1 + poll 1 + read 2 + eoi 1 = 11 cycles = 22 ns`.
- Total message-to-EOI round-trip latency is dominated by member IP internal timing (mailbox push/doorbell/notify, controller sample/pend/filter/priority/delivery/ack/EOI) and is a measured output of the model, not a fixed constant.

Sequential/parallel relationship:

```text
Sequential interrupt path:
  host message -> mailbox doorbell -> bridge level assert
    -> controller deliver -> cpu ack -> software read + clear + EOI
    -> level reassert while messages remain

Parallel:
  host message
  irq source bridge
  cpu service
  software handler
  storm monitor
  all member IP processes
```

## 12. Open Items

- Depth of the message intake queue.
- Storm limit and throttle window defaults, and whether they should be per-channel.
- Whether the software handler should batch-read all channel messages per EOI or service one message per delivery round (current model: one per round).
- Channel-to-source mapping table (currently identity: channel N is source N).
- Whether storm masking should escalate to disabling the mailbox channel.
