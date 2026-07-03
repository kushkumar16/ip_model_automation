# I3C Master IP Design-Level Document

## 1. Purpose

The I3C Master IP is an MIPI I3C bus controller. Software queues private read
and write transfer commands; a command engine arbitrates for the shared
open-drain bus and starts a transfer addressed to a target's dynamic address.
A bit transfer engine shifts address and data bytes over SCL/SDA and samples
ACK/NACK. Independently, an IBI engine watches the bus during idle windows for
in-band interrupt requests raised by targets, arbitrates them onto the bus,
and captures the IBI payload byte. Transfer completion, NACK, and IBI events
are aggregated into a masked interrupt.

The performance model must represent the concurrent command, bit-transfer, and
IBI processes and the sequential dependency between command dispatch, address
send, byte shifting, ACK sampling, and interrupt generation.

## 2. Scope

In scope:

- Register-programmed command queue for private read/write transfers to
  already-assigned dynamic addresses.
- Bit-level SDR transfer engine with per-bit shift timing and ACK/NACK
  sampling.
- In-band interrupt (IBI) detection, arbitration, and payload capture during
  bus-idle windows.
- Interrupt aggregation for transfer-done, NACK, and IBI-received events with
  masking and software clear.

Out of scope:

- Common Command Codes (CCC) processing and dynamic address assignment
  (ENTDAA).
- Multi-master hand-off and secondary master role.
- HDR modes (HDR-DDR, HDR-BT); this model covers SDR mode only.
- Hot-join detection beyond posting an IBI queue entry.

## 3. Context

I3C master controllers manage a shared open-drain bus shared by targets that
support both register-style transfers and target-initiated in-band
interrupts. Software queues a private write or read command; the command
engine arbitrates for bus ownership, issues START and the target's dynamic
address, and hands the transfer to a bit engine that shifts bytes and samples
ACK/NACK. When no host transfer is active, targets may pull SDA during the
bus-idle arbitration window to request service; the IBI engine detects this,
arbitrates it onto the bus, and reads the IBI payload byte.

Some processes are naturally parallel: the command engine can arbitrate the
next command while the bit engine drives the current byte, and the IBI engine
watches for interrupt requests independently. Some operations are sequential:
a command must win arbitration before START is issued, address must be sent
and ACKed before data bytes are shifted, and an IBI request must be arbitrated
and ACKed before its payload byte is read.

## 4. Interfaces

### 4.1 Register Interface

Type: APB/AHB/AXI-lite style control interface.

Important registers:

- `CMD_QUEUE`: push a private read/write transfer command.
- `TX_DATA_QUEUE`: write bytes for a pending write command.
- `RX_DATA_QUEUE`: read bytes captured by a completed read command.
- `IBI_QUEUE`: read captured IBI payload bytes.
- `STATUS`: busy, queue levels, transfer-done.
- `INT_MASK`: interrupt mask.
- `INT_CLEAR`: write-one-to-clear interrupt status.

Fields:

- `addr`
- `write_data`
- `read_data`

Timing:

- Configuration and command writes update shadow state before a transfer
  starts.

### 4.2 I3C Bus Interface

Fields:

- `scl`
- `sda`
- `direction`

Timing:

- Each bit takes one SDR bit period derived from the bus clock divider.
- The bus is open-drain during address and IBI arbitration and push-pull
  during SDR data phases.

### 4.3 Interrupt Output Interface

Fields:

- `irq_valid`
- `irq_vector`
- `irq_level`

Timing:

- Transfer-done, NACK, and IBI-received interrupts are level and remain
  asserted until software clears status.

## 5. Transfer Types

`PRIVATE_WRITE`:

- Software queues bytes; the command engine arbitrates, sends the dynamic
  address with a write direction, and the bit engine shifts queued bytes out.

`PRIVATE_READ`:

- The command engine arbitrates and sends the dynamic address with a read
  direction; the bit engine shifts in bytes driven by the target and captures
  them to the RX queue.

`IBI_REQUEST`:

- A target pulls SDA low during a bus-idle arbitration window; the IBI engine
  detects the request, arbitrates it onto the bus, ACKs it, and reads the
  IBI payload byte.

## 6. FSMs and Processes

### 6.1 Register Access FSM

Role: Accept software reads/writes, load the command and TX data queues, and
serve reads of the RX and IBI queues.

States:

- `RESET`
- `IDLE`
- `DECODE_ACCESS`
- `WRITE_CONFIG`
- `READ_STATUS`
- `ACCESS_ERROR`

Sequential dependencies:

- A command is queued before the command engine can dispatch it.

Parallel interaction:

- Runs independently from the command, bit-transfer, and IBI engines.

### 6.2 Command Engine FSM

Role: Pull the next queued command, arbitrate for the bus, and start a
transfer with START and the target dynamic address.

States:

- `IDLE`
- `ARBITRATE_BUS`
- `ISSUE_START`
- `SEND_ADDRESS`
- `WAIT_ACK`
- `DISPATCH_TRANSFER`

Sequential dependencies:

- Bus arbitration is won before START is issued.
- The address is sent and ACKed before the bit engine shifts data bytes.

Parallel interaction:

- Runs in parallel with the bit transfer engine, which it feeds, and yields
  bus arbitration to the IBI engine when a target request is pending.

### 6.3 Bit Transfer Engine FSM

Role: Shift address and data bits over SCL/SDA and sample ACK/NACK.

States:

- `IDLE`
- `SHIFT_BIT`
- `SAMPLE_BIT`
- `BYTE_COMPLETE`
- `CHECK_ACK`
- `ISSUE_STOP`

Sequential dependencies:

- All bits of a byte are shifted before ACK/NACK is sampled.
- ACK is sampled before the next byte is shifted or STOP is issued.

Parallel interaction:

- Runs concurrently with the command engine, which dispatches transfers to
  it, and the IBI engine, which shares the bus resource.

### 6.4 IBI Engine FSM

Role: Detect an in-band interrupt request during a bus-idle window, arbitrate
it onto the bus, ACK it, and capture the IBI payload byte.

States:

- `IDLE`
- `DETECT_REQUEST`
- `ARBITRATE_IBI`
- `ACK_IBI`
- `READ_IBI_BYTE`
- `NACK_IBI`

Sequential dependencies:

- A request is arbitrated before it is ACKed.
- The IBI is ACKed before its payload byte is read.

Parallel interaction:

- Runs concurrently with the command engine and bit transfer engine, sharing
  the bus resource with them.

### 6.5 Interrupt Control FSM

Role: Aggregate transfer-done, NACK, and IBI-received events into a masked
interrupt output.

States:

- `IDLE`
- `EVALUATE_EVENTS`
- `APPLY_MASK`
- `ASSERT_IRQ`
- `WAIT_SW_CLEAR`
- `DEASSERT_IRQ`

Sequential dependencies:

- Event evaluation precedes masking.
- Level interrupt deassertion requires software clear.

Parallel interaction:

- Observes events from the bit transfer engine and the IBI engine.

## 7. Process Relationship Summary

Sequential command path:

```text
Command Queue Entry
  -> Command Engine (arbitrate + start + address)
  -> Bit Transfer Engine (shift bytes + sample ACK)
  -> Interrupt Control (transfer-done)
  -> Software Clear
```

Sequential IBI path:

```text
Target IBI Request
  -> IBI Engine (detect + arbitrate + ack + read byte)
  -> Interrupt Control (ibi-received)
  -> Software Clear
```

Parallel processes:

```text
Register Access
Command Engine
Bit Transfer Engine
IBI Engine
Interrupt Control
```

## 8. Resources and Queues

Queues/events:

- Command queue.
- TX data byte queue.
- RX data byte queue.
- IBI payload queue.
- Interrupt pending queue.

Resources:

- I3C bus port (shared, open-drain, arbitrated).
- Register bus access port.
- Interrupt output line.

## 9. Performance Model Requirements

The SimPy model shall include:

- A register-access process that loads the command/TX queues and serves
  RX/IBI queue reads.
- A command-engine process with bus arbitration.
- A bit-transfer-engine process with a per-bit shift period and ACK/NACK
  sampling.
- An IBI-engine process with request detection, arbitration, and payload
  capture.
- An interrupt-control process with mask and clear.
- Metrics for commands issued, transmitted bytes, received bytes, transfers
  completed, NACKs, IBI requests, IBIs accepted, IBI bytes captured,
  interrupts, masked interrupts, and clear latency.

## 10. SimPy Functionality Requirements

The generated SimPy delay model shall include:

- Command queue and TX/RX byte queues with bounded depth.
- Bit-level shift delay for address and data bytes.
- ACK/NACK detection on the addressed byte.
- IBI request detection, arbitration, and a bounded IBI payload queue.
- Software interrupt clear behavior.

All generated timing values must come from this DLD and the reviewed
template.

## 11. FSM Timing Model

Clock assumption:

- Bus clock frequency: 500 MHz.
- Cycle time: 2 ns.
- All delays below are hard-coded starter values for model generation.

FSM/process count:

- Total FSM/processes: 5.
- Parallel processes: register access, command engine, bit transfer engine,
  IBI engine, and interrupt control.
- Sequential dependency: arbitration precedes start; address/ACK precedes
  data shift; transfer/IBI completion precedes interrupt.

Per-process timing:

| FSM/process | Runs as | Delay model |
| --- | --- | --- |
| Register Access FSM | Parallel bus process | Decode: 2 cycles = 4 ns. Write config: 2 cycles = 4 ns. Read status: 1 cycle = 2 ns. |
| Command Engine FSM | Parallel transfer process | Arbitrate bus: 1 cycle = 2 ns. Issue start: 1 cycle = 2 ns. Send address: 1 cycle = 2 ns. |
| Bit Transfer Engine FSM | Parallel bit-shift process | Shift bit: 1 cycle = 2 ns. Sample bit: 1 cycle = 2 ns. Check ack: 1 cycle = 2 ns. |
| IBI Engine FSM | Parallel event process | Detect request: 1 cycle = 2 ns. Arbitrate ibi: 1 cycle = 2 ns. Ack ibi: 1 cycle = 2 ns. |
| Interrupt Control FSM | Parallel shared process | Evaluate events: 2 cycles = 4 ns. Apply mask: 2 cycles = 4 ns. Assert IRQ: 2 cycles = 4 ns. |

End-to-end delay:

- Command dispatch to transfer-done interrupt (1-byte write):
  `arbitrate 1 + start 1 + address 1 + shift 8 + sample 1 + check_ack 1 + evaluate 2 + mask 2 + irq 2 = 19 cycles = 38 ns`.

Sequential/parallel relationship:

```text
Sequential command path:
  command queued -> arbitrate + start + address -> shift bytes + check ack
    -> interrupt control -> IRQ visible

Parallel:
  command engine
  bit transfer engine
  ibi engine
  register access
  interrupt control
```

## 12. Open Items

- Depth of the command, TX/RX byte, and IBI payload queues.
- Number of IBI payload bytes captured per accepted request (single MDB byte
  vs. multi-byte).
- NACK retry policy for a rejected private transfer.
- Whether an in-progress bit transfer can be preempted by a pending IBI, or
  whether IBI arbitration only happens during bus-idle windows.
- Dynamic address table size (out of scope for this model; addresses are
  assumed pre-assigned).
