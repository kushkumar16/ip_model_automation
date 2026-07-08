# SPI Master IP Design-Level Document

## 1. Purpose

The SPI Master IP is a serial peripheral interface controller. Software programs
transfer parameters and pushes bytes into a transmit FIFO. A transfer engine
serializes bytes onto the SPI bus, driving the clock and MOSI line while sampling
MISO. Received bytes are pushed into a receive FIFO, and completion or FIFO
threshold conditions raise a masked interrupt.

The performance model must represent the concurrent transmit, shift, and receive
processes and the sequential dependency between byte load, bit shifting, receive
capture, and interrupt generation.

## 2. Scope

In scope:

- Register-programmed baud, mode, and chip-select control.
- Transmit and receive byte FIFOs with threshold flags.
- Byte serialization over the SPI bus with chip-select framing.
- Transfer-done and FIFO-threshold interrupts with masking and clear.

Out of scope:

- Analog pad and slew-rate behavior.
- Multi-master arbitration on the SPI bus.
- DMA descriptor handling.
- Real interrupt controller implementation.

## 3. Context

SPI master controllers are common SoC peripheral blocks. Software configures the
clock rate and transfer mode, asserts a chip-select, and streams bytes through a
transmit FIFO. Each byte is shifted out bit by bit while the peer device shifts a
byte back on MISO. Received bytes are captured into a receive FIFO for software.

Some processes are naturally parallel: the transmit engine can load the next byte
while the shift engine drives the current byte, and the receive engine drains
captured bytes concurrently. Some operations are sequential: a byte must be loaded
before it is shifted, a shift must complete before the received byte is captured,
and completion must be observed before the transfer-done interrupt asserts.

## 4. Interfaces

### 4.1 Register Interface

Type: APB/AHB/AXI-lite style control interface.

Important registers:

- `CTRL`: enable, clock polarity/phase, chip-select select.
- `BAUD`: clock divider.
- `TX_DATA`: write byte into transmit FIFO.
- `RX_DATA`: read byte from receive FIFO.
- `STATUS`: busy, TX/RX FIFO level, transfer-done.
- `INT_MASK`: interrupt mask.
- `INT_CLEAR`: write-one-to-clear interrupt status.

Fields:

- `addr`
- `write_data`
- `read_data`

Timing:

- Configuration writes update shadow state before a transfer starts.

### 4.2 SPI Bus Interface

Fields:

- `sclk`
- `mosi`
- `miso`
- `cs_n`

Timing:

- Each bit takes one shift period derived from the baud divider.
- Chip-select frames a byte transfer.

### 4.3 Interrupt Output Interface

Fields:

- `irq_valid`
- `irq_vector`
- `irq_level`

Timing:

- Transfer-done and threshold interrupts are level and remain asserted until
  software clears status.

## 5. Transfer Modes

`SINGLE_BYTE`:

- One byte shifted per transfer with chip-select framing.

`BURST`:

- Multiple queued bytes shifted back-to-back with chip-select held.

`CONFIG`:

- Software programs baud, mode, and chip-select before a transfer.

## 6. FSMs and Processes

### 6.1 Register Access FSM

Role: Accept software reads/writes, update configuration, and load the TX FIFO.

States:

- `RESET`
- `IDLE`
- `DECODE_ACCESS`
- `WRITE_CONFIG`
- `READ_STATUS`
- `ACCESS_ERROR`

Sequential dependencies:

- Configuration writes update shadow state before a transfer starts.

Parallel interaction:

- Runs independently from the transfer engines.

### 6.2 Transmit Engine FSM

Role: Pull the next byte from the TX FIFO and start a shift.

States:

- `IDLE`
- `LOAD_BYTE`
- `ASSERT_CS`
- `SHIFT_REQUEST`
- `WAIT_SHIFT_DONE`

Sequential dependencies:

- A byte is loaded before a shift is requested.
- Chip-select is asserted before the first byte of a frame.

Parallel interaction:

- Runs in parallel with the shift and receive engines.

### 6.3 Shift Engine FSM

Role: Shift a byte bit by bit over the SPI bus and sample MISO.

States:

- `IDLE`
- `SHIFT_BIT`
- `SAMPLE_MISO`
- `BYTE_COMPLETE`
- `DEASSERT_CS`

Sequential dependencies:

- All bits of a byte are shifted before byte completion.
- Chip-select is deasserted after the last byte of a frame.

Parallel interaction:

- Runs concurrently with transmit load and receive capture.

### 6.4 Receive Engine FSM

Role: Capture shifted-in bytes into the RX FIFO.

States:

- `IDLE`
- `RECEIVE_BYTE`
- `CHECK_SPACE`
- `ENQUEUE`
- `OVERFLOW`

Sequential dependencies:

- A byte is captured only after a shift completes.
- A full RX FIFO raises an overflow condition.

Parallel interaction:

- Runs in parallel with transmit and shift engines.

### 6.5 Interrupt Control FSM

Role: Raise transfer-done and FIFO-threshold interrupts with masking and clear.

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

- Observes completion and FIFO events from the transfer engines.

## 7. Process Relationship Summary

Sequential transfer path:

```text
TX FIFO Byte
  -> Transmit Engine (load)
  -> Shift Engine (bits + sample)
  -> Receive Engine (capture)
  -> Interrupt Control (transfer-done)
  -> Software Clear
```

Parallel processes:

```text
Register Access
Transmit Engine
Shift Engine
Receive Engine
Interrupt Control
```

## 8. Resources and Queues

Queues/events:

- Transmit byte FIFO.
- Shift request queue.
- Receive byte FIFO.
- Interrupt pending queue.

Resources:

- SPI bus port.
- Register bus access port.
- Interrupt output line.

## 9. Performance Model Requirements

The SimPy model shall include:

- A register-access process that loads the TX FIFO.
- A transmit-engine process.
- A shift-engine process with a per-bit shift period.
- A receive-engine process with overflow behavior.
- An interrupt-control process with mask and clear.
- Metrics for transmitted bytes, received bytes, transfers completed, RX
  overflows, interrupts, masked interrupts, and clear latency.

## 10. SimPy Functionality Requirements

The generated SimPy delay model shall include:

- Configuration state (baud divider, mode, chip-select).
- Transmit and receive byte FIFOs with bounded depth.
- Byte serialization with a per-bit shift delay.
- Transfer-done detection and interrupt behavior.
- Software interrupt clear behavior.

All generated timing values must come from this DLD and the reviewed template.

## 11. FSM Timing Model

Clock assumption:

- Bus clock frequency: 500 MHz.
- Cycle time: 2 ns.
- All delays below are hard-coded starter values for model generation.

FSM/process count:

- Total FSM/processes: 5.
- Parallel processes: register access, transmit engine, shift engine, receive
  engine, and interrupt control.
- Sequential dependency: load precedes shift; shift precedes capture; completion
  precedes interrupt.

Per-process timing:

| FSM/process | Runs as | Delay model |
| --- | --- | --- |
| Register Access FSM | Parallel bus process | Decode: 2 cycles = 4 ns. Write config: 2 cycles = 4 ns. Read status: 1 cycle = 2 ns. |
| Transmit Engine FSM | Parallel transfer process | Load byte: 1 cycle = 2 ns. Assert chip-select: 1 cycle = 2 ns. Shift request: 1 cycle = 2 ns. |
| Shift Engine FSM | Parallel bit-shift process | Shift bit: 1 cycle = 2 ns. Sample MISO: 1 cycle = 2 ns. Byte complete: 1 cycle = 2 ns. |
| Receive Engine FSM | Parallel capture process | Receive byte: 1 cycle = 2 ns. Space check: 1 cycle = 2 ns. Enqueue: 1 cycle = 2 ns. |
| Interrupt Control FSM | Parallel shared process | Evaluate events: 2 cycles = 4 ns. Apply mask: 2 cycles = 4 ns. Assert IRQ: 2 cycles = 4 ns. |

End-to-end delay:

- Byte load to transfer-done interrupt (8-bit byte):
  `load 1 + assert_cs 1 + shift 8 + sample 1 + capture 1 + evaluate 2 + mask 2 + irq 2 = 18 cycles = 36 ns`.

Sequential/parallel relationship:

```text
Sequential transfer path:
  tx byte -> transmit load -> shift bits -> receive capture
    -> interrupt control -> IRQ visible

Parallel:
  transmit engine
  shift engine
  receive engine
  register access
  interrupt control
```

## 12. Open Items

- Transmit and receive FIFO depths.
- Supported SPI modes (CPOL/CPHA combinations).
- Number of chip-select lines.
- FIFO threshold levels for interrupts.
- Whether RX overflow raises an error interrupt.
