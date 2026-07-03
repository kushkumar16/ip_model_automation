# AXI Interconnect IP Design-Level Document

## 1. Purpose

The AXI Interconnect IP routes transactions between multiple AXI masters and
multiple AXI slaves. It performs address decode, arbitration, outstanding
transaction tracking, ordering enforcement, response routing, backpressure
handling, and optional QoS/priority selection.

The performance model must represent multiple concurrent protocol channels:

- Write address channel.
- Write data channel.
- Write response channel.
- Read address channel.
- Read data channel.

Some operations are sequentially dependent, such as address decode before slave
selection and response routing after outstanding ID lookup. Other operations
run in parallel, such as read and write paths, independent master ports, and
independent slave ports.

## 2. Scope

In scope:

- Multiple AXI master ports.
- Multiple AXI slave ports.
- Read and write address decode.
- Per-slave arbitration.
- Outstanding transaction tables.
- ID-based response routing.
- Read/write channel backpressure.
- Per-port buffering.
- Ordering constraints per master ID.
- Optional QoS or priority arbitration.

Out of scope:

- Full AXI signal-level timing.
- Exclusive access semantics.
- Cache coherency.
- Low-power protocol behavior.
- Data payload correctness.

## 3. Context

In a SoC, CPU clusters, DMA engines, accelerators, and peripheral masters share
memory and peripheral targets through an interconnect. The interconnect must
route transactions to the correct target while preserving required ordering and
returning responses to the originating master.

For model generation, transaction payload contents are not required. Required
metadata includes master ID, transaction ID, address, length, burst attributes,
target slave, priority/QoS, and response status.

## 4. Interfaces

### 4.1 Master-Side AXI Interfaces

One logical interface exists per master port.

Channels:

- `AW`: write address.
- `W`: write data.
- `B`: write response.
- `AR`: read address.
- `R`: read data.

Important fields:

- `master_id`
- `axi_id`
- `addr`
- `length`
- `size`
- `burst`
- `qos`
- `prot`
- `valid`
- `ready`

Timing:

- `AW` and `W` may arrive independently.
- `AR` is independent from write channels.
- Backpressure may occur independently per channel.

### 4.2 Slave-Side AXI Interfaces

One logical interface exists per target slave port.

Channels:

- `AW`
- `W`
- `B`
- `AR`
- `R`

Timing:

- Slave readiness controls downstream issue.
- Slave responses are routed back using outstanding transaction metadata.

### 4.3 Configuration Interface

Configuration fields:

- Address map regions.
- Slave enable.
- Arbitration policy.
- Per-master priority or QoS weights.
- Outstanding transaction limit.
- Per-port buffer depth.
- Error response policy for unmapped access.

## 5. Transactions

`READ_TRANSACTION`:

- Master issues `AR`.
- Interconnect decodes address.
- Slave read request is arbitrated and issued.
- Slave returns one or more `R` beats.
- Interconnect routes `R` beats to original master.

`WRITE_TRANSACTION`:

- Master issues `AW`.
- Master sends one or more `W` beats.
- Interconnect decodes address and routes address/data to target slave.
- Slave returns `B`.
- Interconnect routes `B` to original master.

`ERROR_TRANSACTION`:

- Unmapped address or disabled target.
- Interconnect generates decode error response without issuing to slave.

## 6. FSMs and Processes

### 6.1 Write Address Ingress FSM

Role: Accept write addresses from master ports and enqueue decoded write
requests.

States:

- `RESET`
- `WAIT_AW`
- `CAPTURE_AW`
- `DECODE_ADDR`
- `CHECK_OUTSTANDING`
- `ENQUEUE_WRITE_REQ`
- `AW_STALL`
- `AW_ERROR`

Sequential dependencies:

- Address capture precedes decode.
- Decode precedes target slave selection.
- Outstanding credit check precedes enqueue.

Parallel interaction:

- One instance may run per master port.
- Runs in parallel with write data ingress and read address ingress.

### 6.2 Write Data Ingress FSM

Role: Accept write data beats and buffer them until routed to the selected
slave.

States:

- `RESET`
- `WAIT_W`
- `CAPTURE_W_BEAT`
- `BUFFER_W_BEAT`
- `WAIT_LAST`
- `W_STALL`
- `W_ERROR`

Sequential dependencies:

- Data beats for a burst are tracked in order.
- Last beat closes the write data packet for that transaction.

Parallel interaction:

- May run before, after, or concurrently with write address processing.
- Runs independently per master port.

### 6.3 Write Join/Ordering FSM

Role: Match write address and write data streams before slave issue.

States:

- `WAIT_AW_AND_W`
- `CHECK_ORDER`
- `MERGE_WRITE`
- `READY_FOR_SLAVE_ARB`
- `ORDER_STALL`

Sequential dependencies:

- A slave write can issue only when required address and data metadata are
  available.
- Ordering per master ID must be checked before issue.

Parallel interaction:

- Runs in parallel across target slave queues.

### 6.4 Slave Write Arbitration FSM

Role: Select write requests targeting each slave.

States:

- `SCAN_MASTERS`
- `CHECK_POLICY`
- `GRANT_WRITE`
- `ISSUE_AW`
- `ISSUE_W`
- `SLAVE_BACKPRESSURE`
- `NO_WRITE_ELIGIBLE`

Sequential dependencies:

- Grant precedes slave issue.
- `AW` issue and `W` issue must obey configured channel coupling rules.

Parallel interaction:

- One instance runs per slave port.
- Runs in parallel with read arbitration.

### 6.5 Write Response Routing FSM

Role: Route slave `B` responses back to the originating master.

States:

- `WAIT_B`
- `LOOKUP_OUTSTANDING`
- `ROUTE_B`
- `UPDATE_CREDITS`
- `B_STALL`
- `B_ERROR`

Sequential dependencies:

- Response lookup precedes routing.
- Outstanding credit release follows successful response routing.

Parallel interaction:

- One instance may run per slave or shared response fabric.
- Runs in parallel with new write request issue.

### 6.6 Read Address Ingress FSM

Role: Accept read addresses from master ports and enqueue decoded read requests.

States:

- `RESET`
- `WAIT_AR`
- `CAPTURE_AR`
- `DECODE_ADDR`
- `CHECK_OUTSTANDING`
- `ENQUEUE_READ_REQ`
- `AR_STALL`
- `AR_ERROR`

Sequential dependencies:

- Address capture precedes decode.
- Decode and outstanding credit check precede enqueue.

Parallel interaction:

- One instance may run per master port.
- Runs in parallel with all write-path FSMs.

### 6.7 Slave Read Arbitration FSM

Role: Select read requests targeting each slave.

States:

- `SCAN_MASTERS`
- `CHECK_POLICY`
- `GRANT_READ`
- `ISSUE_AR`
- `SLAVE_BACKPRESSURE`
- `NO_READ_ELIGIBLE`

Sequential dependencies:

- Grant precedes slave read address issue.

Parallel interaction:

- One instance runs per slave port.
- Runs in parallel with write arbitration and response routing.

### 6.8 Read Data Routing FSM

Role: Route slave `R` data beats back to the originating master.

States:

- `WAIT_R`
- `LOOKUP_OUTSTANDING`
- `ROUTE_R_BEAT`
- `CHECK_LAST`
- `UPDATE_CREDITS`
- `R_STALL`
- `R_ERROR`

Sequential dependencies:

- Outstanding lookup precedes routing.
- Credit release happens only after final read beat.

Parallel interaction:

- Runs in parallel with new read address issue.
- Multiple slaves may return read data concurrently.

### 6.9 Decode Error Response FSM

Role: Generate local error responses for unmapped or disabled regions.

States:

- `WAIT_ERROR_REQ`
- `BUILD_ERROR_RESP`
- `ROUTE_ERROR`
- `ERROR_RESP_STALL`

Sequential dependencies:

- Error response is generated after address decode failure.

Parallel interaction:

- Runs independently from normal slave issue paths.

## 7. Process Relationship Summary

Sequential write transaction path:

```text
Master AW Capture
  -> Address Decode
  -> Outstanding Check
  -> AW/Data Join
  -> Slave Write Arbitration
  -> Slave AW/W Issue
  -> B Response Lookup
  -> B Response Route
  -> Outstanding Credit Release
```

Sequential read transaction path:

```text
Master AR Capture
  -> Address Decode
  -> Outstanding Check
  -> Slave Read Arbitration
  -> Slave AR Issue
  -> R Response Lookup
  -> R Beat Route
  -> Final Beat Credit Release
```

Parallel processes:

```text
Write Address Ingress per master
Write Data Ingress per master
Write Join per target/order domain
Slave Write Arbitration per slave
Write Response Routing
Read Address Ingress per master
Slave Read Arbitration per slave
Read Data Routing
Decode Error Response
```

## 8. Resources and Queues

Queues:

- Per-master AW ingress queue.
- Per-master W data buffer.
- Per-slave write request queue.
- Per-master AR ingress queue.
- Per-slave read request queue.
- Outstanding transaction table.
- Response routing queues.
- Decode error queue.

Resources:

- Address decoder.
- Per-slave write port.
- Per-slave read port.
- Master response ports.
- Per-master outstanding credits.
- Per-slave outstanding credits.
- Internal buffering capacity.

## 9. Performance Model Requirements

The SimPy model shall include:

- Separate processes for AW, W, B, AR, and R behavior.
- Per-master ingress queues.
- Per-slave arbitration processes.
- Outstanding transaction tables with configurable depth.
- Backpressure on master and slave interfaces.
- Metrics for read latency, write latency, arbitration wait, response routing
  wait, outstanding depth, decode errors, and port utilization.

The performance model shall not model actual data payloads.

## 10. SimPy Functionality Requirements

The generated SimPy delay model shall include enough functionality to validate
the FSM behavior from this DLD without modeling data payloads. The model must
include:

- Address map lookup.
- Transaction acceptance APIs for read and write.
- Deterministic arbitration policy.
- Outstanding ID tracking.
- Response routing.
- Error response generation.
- Ordering checks per master ID.
- Write address/write data join behavior.
- Slave backpressure and outstanding-credit stalls.

## 11. FSM Timing Model

Clock assumption:

- AXI/interconnect clock frequency: 500 MHz.
- Cycle time: 2 ns.
- All delays below are hard-coded starter values for model generation.

FSM/process count:

- Total FSM/processes: 9.
- Parallel channel groups: write address ingress, write data ingress, write
  join/ordering, slave write arbitration, write response routing, read address
  ingress, slave read arbitration, read data routing, and decode error response.
- Sequential dependencies are per transaction; read and write channel groups
  may run in parallel.

Per-process timing:

| FSM/process | Runs as | Delay model |
| --- | --- | --- |
| Write Address Ingress FSM | Parallel per-master process | AW capture: 2 cycles = 4 ns. Address decode: 5 cycles = 10 ns. Outstanding check: 3 cycles = 6 ns. Enqueue: 2 cycles = 4 ns. |
| Write Data Ingress FSM | Parallel per-master process | W beat capture: 1 cycle = 2 ns. Buffer write per beat: 2 cycles = 4 ns. Last-beat close: 1 cycle = 2 ns. |
| Write Join/Ordering FSM | Parallel per-target/order-domain process | AW/W match: 4 cycles = 8 ns. Ordering check: 4 cycles = 8 ns. Merge write: 2 cycles = 4 ns. |
| Slave Write Arbitration FSM | Parallel per-slave process | Master scan/arbitration: 10 cycles = 20 ns. Grant: 2 cycles = 4 ns. AW issue: 2 cycles = 4 ns. W issue: 2 cycles = 4 ns per beat. |
| Write Response Routing FSM | Parallel response process | B capture: 1 cycle = 2 ns. Outstanding lookup: 4 cycles = 8 ns. Route B: 3 cycles = 6 ns. Credit release: 1 cycle = 2 ns. |
| Read Address Ingress FSM | Parallel per-master process | AR capture: 2 cycles = 4 ns. Address decode: 5 cycles = 10 ns. Outstanding check: 3 cycles = 6 ns. Enqueue: 2 cycles = 4 ns. |
| Slave Read Arbitration FSM | Parallel per-slave process | Master scan/arbitration: 10 cycles = 20 ns. Grant: 2 cycles = 4 ns. AR issue: 2 cycles = 4 ns. |
| Read Data Routing FSM | Parallel response process | R capture: 1 cycle = 2 ns. Outstanding lookup: 4 cycles = 8 ns. Route R beat: 3 cycles = 6 ns. Final-beat credit release: 1 cycle = 2 ns. |
| Decode Error Response FSM | Parallel local error process | Build error response: 3 cycles = 6 ns. Route error: 3 cycles = 6 ns. |

End-to-end transaction delay:

- Minimum write control delay excluding slave service and W beat count:
  `AW path 12 + join 10 + write arbitration 14 + B route 9 = 45 cycles = 90 ns`.
- Minimum read control delay excluding slave service and R beat count:
  `AR path 12 + read arbitration 14 + R route 9 = 35 cycles = 70 ns`.
- Arbitration over request queues uses `10 cycles = 20 ns` per slave arbitration
  decision.
- Unmapped access local error delay:
  `decode 5 + error build 3 + route 3 = 11 cycles = 22 ns`.

Sequential/parallel relationship:

```text
Sequential write path:
  AW capture -> decode -> outstanding check
  W capture -> buffer
  AW/W join -> write arbitration -> slave issue -> B lookup -> B route

Sequential read path:
  AR capture -> decode -> outstanding check
    -> read arbitration -> slave issue -> R lookup -> R route

Parallel:
  AW, W, B, AR, and R channel processes
  per-master ingress
  per-slave arbitration
  decode error response
```

## 12. Open Items

- Number of master and slave ports.
- AXI ID width.
- Maximum burst length.
- Outstanding depth per master/slave.
- Address map definition.
- Arbitration policy and QoS weights.
- Whether write address/data must be coupled at slave issue.
