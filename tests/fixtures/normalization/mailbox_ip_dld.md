# Mailbox IP Design-Level Document

## 1. Purpose

The Mailbox IP provides inter-processor communication (IPC) between agents on the
SoC. It exposes a set of independent message channels. A sender agent writes
short messages into a channel FIFO; a receiver agent reads them in order. Each
delivered message raises a doorbell, and doorbells are aggregated into a masked
interrupt toward a target CPU.

The performance model must represent concurrent mailbox processes and the
sequential dependency between message enqueue, doorbell generation, interrupt
delivery, and software clear.

## 2. Scope

In scope:

- Multiple independent message channels.
- Per-channel message FIFO with full/empty flow control.
- Doorbell event generation on message arrival.
- Interrupt aggregation, masking, and software clear.
- Register-programmed channel enable and interrupt mask.

Out of scope:

- Message payload transformation or encryption.
- Shared-memory descriptor rings.
- Cross-die transport.
- Real interrupt controller implementation.

## 3. Context

Mailbox IPs are common SoC IPC blocks. Software on a sender core writes a message
word to a channel; the mailbox stores it and notifies the receiver core through a
doorbell interrupt. The receiver core reads the message and clears the doorbell.

Some mailbox processes are naturally parallel: register access, per-channel push
and pop, doorbell generation, and interrupt aggregation all run concurrently.
Some operations are sequential: a message must be enqueued before its doorbell is
raised, and a doorbell must be aggregated before the interrupt is asserted. The
interrupt remains asserted until software clears it.

## 4. Interfaces

Every interface below states a `Wait model:` block — how the requester (IP1) waits
on the responder (IP2) across that interface: `wait_for_response` (blocks until the
response returns and uses the result), `wait_for_ack_inline` (blocks at the request
site for an ack, then continues the same pipeline), or
`wait_for_ack_before_next_request` (continues after issuing; the ack is collected
before the next command starts). An interface that does not state one is read as
`wait_for_ack_inline`.

### 4.1 Register Interface

Type: APB/AHB/AXI-lite style control interface.

Important registers:

- `CHAN_CTRL[n]`: channel enable and reset.
- `INT_MASK`: per-channel interrupt mask.
- `INT_STATUS`: raw and masked doorbell status.
- `INT_CLEAR`: write-one-to-clear doorbell status.
- `CHAN_STATUS[n]`: FIFO occupancy and full/empty flags.

Fields:

- `addr`
- `write_data`
- `read_data`

Timing:

- Writes update a shadow configuration before taking effect.

Wait model:

- Mode: `wait_for_response`
- Requester: peer
- Waits in: `register_access.READ_STATUS`
- Resumes on: `register_access_complete`
- Response used for: software consumes `read_data` (status, FIFO occupancy) before its next access
- Note: writes are acknowledged in place as shadow configuration updates

### 4.2 Sender Message Interface

Fields:

- `channel_id`
- `message`
- `valid`

Timing:

- A message is accepted only when the target channel FIFO has space.
- A full channel applies backpressure to the sender.

Wait model:

- Mode: `wait_for_ack_inline`
- Requester: peer
- Waits in: `message_push.CHECK_SPACE`
- Resumes on: `fifo_space_available`
- Note: the sender needs no result, only the accept; a full channel backpressures it in place

### 4.3 Receiver Message Interface

Fields:

- `channel_id`
- `message`
- `ready`

Timing:

- The receiver observes messages in FIFO order.
- A read from an empty channel returns no message.

Wait model:

- Mode: `wait_for_response`
- Requester: peer
- Waits in: `message_pop.DELIVER`
- Resumes on: `message_delivered`
- Response used for: the receiver consumes the returned message
- Note: a read from an empty channel returns no message and the pop process waits in `EMPTY_WAIT`

### 4.4 Interrupt Output Interface

Fields:

- `irq_valid`
- `irq_target`
- `irq_level`

Timing:

- Doorbell interrupt is level and remains asserted until software clears status.

Wait model:

- Mode: `wait_for_ack_before_next_request`
- Requester: this IP
- Waits in: `interrupt_notify.WAIT_SW_CLEAR`
- Resumes on: `software_clear`
- Outstanding limit: 1
- Note: the doorbell interrupt does not block the message path; the software clear must be seen before the next interrupt is asserted

## 5. Message Flow

`ENQUEUE`:

- Sender message accepted into channel FIFO when space is available.

`DOORBELL`:

- Enqueue raises a doorbell event for the channel.

`NOTIFY`:

- Unmasked doorbells assert the target interrupt.

`DELIVER`:

- Receiver reads the oldest message and frees FIFO space.

## 6. FSMs and Processes

### 6.1 Register Access FSM

Role: Accept software reads/writes and update shadow configuration.

States:

- `RESET`
- `IDLE`
- `DECODE_ACCESS`
- `WRITE_CONFIG`
- `READ_STATUS`
- `ACCESS_ERROR`

Sequential dependencies:

- Register writes update shadow state before runtime logic observes them.

Parallel interaction:

- Runs independently from channel push/pop processes.

### 6.2 Message Push FSM

Role: Accept sender messages and enqueue them into the channel FIFO.

States:

- `IDLE`
- `ACCEPT_MESSAGE`
- `CHECK_SPACE`
- `ENQUEUE`
- `REJECT_FULL`

Sequential dependencies:

- A message is enqueued only after a space check passes.
- Enqueue precedes doorbell generation.

Parallel interaction:

- One push process per channel; push processes run in parallel across channels.

### 6.3 Message Pop FSM

Role: Deliver the oldest message to the receiver and free FIFO space.

States:

- `IDLE`
- `CHECK_PENDING`
- `DEQUEUE`
- `DELIVER`
- `EMPTY_WAIT`

Sequential dependencies:

- Delivery occurs only when the channel FIFO is non-empty.
- Freeing space may release sender backpressure.

Parallel interaction:

- Runs in parallel with push and interrupt processes.

### 6.4 Doorbell FSM

Role: Convert message arrivals into per-channel doorbell events.

States:

- `IDLE`
- `RAISE_DOORBELL`
- `RECORD_EVENT`
- `WAIT_ACK`

Sequential dependencies:

- Doorbell generation depends on a completed enqueue.
- Doorbell record precedes interrupt aggregation.

Parallel interaction:

- Runs concurrently with push, pop, and interrupt aggregation.

### 6.5 Interrupt Notify FSM

Role: Aggregate doorbells into a masked interrupt output.

States:

- `IDLE`
- `COLLECT_DOORBELLS`
- `APPLY_MASK`
- `ASSERT_IRQ`
- `WAIT_SW_CLEAR`
- `DEASSERT_IRQ`

Sequential dependencies:

- Masking occurs before interrupt assertion.
- Level interrupt deassertion requires software clear.

Parallel interaction:

- Observes doorbells from all channels.

## 7. Process Relationship Summary

Sequential channel message path:

```text
Sender Write
  -> Message Push (enqueue)
  -> Doorbell
  -> Interrupt Notify
  -> Software Clear
Receiver Read
  -> Message Pop (dequeue)
```

Parallel processes:

```text
Register Access
Message Push per channel
Message Pop per channel
Doorbell
Interrupt Notify
```

## 8. Resources and Queues

Queues/events:

- Message FIFO per channel.
- Doorbell event queue.
- Interrupt pending queue.

Resources:

- Register bus access port.
- Interrupt output line.

## 9. Performance Model Requirements

The SimPy model shall include:

- A register-access process.
- A message-push process with full/backpressure behavior.
- A message-pop process with empty behavior.
- A doorbell process.
- An interrupt-aggregation process with mask and clear.
- Metrics for enqueued messages, delivered messages, dropped-on-full messages,
  doorbells, interrupts, masked doorbells, and clear latency.

## 10. SimPy Functionality Requirements

The generated SimPy delay model shall include:

- Channel configuration and enable state.
- Per-channel message FIFO with bounded depth.
- Doorbell generation on enqueue.
- Interrupt status/mask/clear behavior.
- Sender backpressure when a FIFO is full.

All generated timing values must come from this DLD and the reviewed template.

## 11. FSM Timing Model

Clock assumption:

- Bus clock frequency: 500 MHz.
- Cycle time: 2 ns.
- All delays below are hard-coded starter values for model generation.

FSM/process count:

- Total FSM/processes: 5.
- Parallel processes: register access, message push per channel, message pop per
  channel, doorbell, and interrupt notify.
- Sequential dependency: enqueue precedes doorbell; doorbell precedes interrupt.

Per-process timing:

| FSM/process | Runs as | Delay model |
| --- | --- | --- |
| Register Access FSM | Parallel bus process | Decode: 2 cycles = 4 ns. Write config: 2 cycles = 4 ns. Read status: 1 cycle = 2 ns. |
| Message Push FSM | Parallel per-channel process | Accept message: 1 cycle = 2 ns. Space check: 1 cycle = 2 ns. Enqueue: 1 cycle = 2 ns. |
| Message Pop FSM | Parallel per-channel process | Pending check: 1 cycle = 2 ns. Dequeue: 1 cycle = 2 ns. Deliver: 1 cycle = 2 ns. |
| Doorbell FSM | Parallel event process | Raise doorbell: 1 cycle = 2 ns. Record event: 1 cycle = 2 ns. |
| Interrupt Notify FSM | Parallel shared process | Collect doorbells: 2 cycles = 4 ns. Apply mask: 2 cycles = 4 ns. Assert IRQ: 2 cycles = 4 ns. |

End-to-end delay:

- Message enqueue to interrupt:
  `accept 1 + space 1 + enqueue 1 + raise 1 + record 1 + collect 2 + mask 2 + irq 2 = 11 cycles = 22 ns`.

Sequential/parallel relationship:

```text
Sequential message path:
  sender write -> push enqueue -> doorbell -> interrupt notify -> IRQ visible

Parallel:
  all channel push/pop processes
  register access
  doorbell
  interrupt notify
```

## 12. Open Items

- Number of mailbox channels.
- Message width.
- FIFO depth per channel.
- Interrupt target routing policy.
- Whether an overflow raises an error interrupt.
