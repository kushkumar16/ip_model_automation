# GDMA IP Design-Level Document

## 1. Purpose

The Generic DMA (GDMA) IP moves data between memory-mapped endpoints without
CPU participation after descriptor programming. It supports descriptor fetch,
command decode, source reads, destination writes, completion/status update,
interrupt generation, and error handling.

The performance model must represent multiple hardware processes, some
sequentially dependent and some parallel:

- Descriptor fetch must precede descriptor decode.
- Decode must precede data movement for a descriptor.
- Read request generation and write response handling may run in parallel once
  a transfer is active.
- Completion writeback and interrupt generation run after descriptor transfer
  completion, but may overlap with fetch of later descriptors if prefetch is
  enabled.

## 2. Scope

In scope:

- Memory-to-memory copy.
- Descriptor ring processing.
- Multi-channel scheduling.
- Source read interface modeling.
- Destination write interface modeling.
- Outstanding request limits.
- Internal buffering.
- Completion writeback.
- Interrupt generation.
- Error detection and channel halt.

Out of scope:

- Byte-accurate data payload modeling.
- Cache-coherency protocol details.
- Security/IOMMU translation behavior.
- Full AXI signal-level timing.

## 3. Context

The GDMA IP is controlled by software through registers. Software programs a
descriptor ring base, ring size, channel enable, and interrupt configuration.
Each descriptor describes source address, destination address, transfer length,
control flags, and optional completion metadata.

The IP fetches descriptors, issues memory reads from the source address range,
buffers returned data internally, writes data to the destination address range,
and finally writes status for the descriptor.

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

- `CTRL`: enable, reset, halt, prefetch enable.
- `STATUS`: idle, busy, halted, error.
- `DESC_BASE`: descriptor ring base address.
- `DESC_HEAD`: software-owned producer index.
- `DESC_TAIL`: hardware-owned consumer index.
- `RING_SIZE`: descriptor ring entries.
- `INT_ENABLE`: interrupt mask.
- `INT_STATUS`: interrupt pending and clear.
- `CH_CFG`: per-channel priority, outstanding depth, burst size.

Wait model:

- Mode: `wait_for_ack_inline`
- Requester: peer
- Waits in: `channel_control.IDLE`
- Resumes on: `shadow_config_applied`
- Note: control writes are acknowledged as shadow configuration is captured

### 4.2 Descriptor Memory Read Interface

Producer: GDMA descriptor fetch process.

Fields:

- `desc_addr`
- `desc_len`
- `channel_id`
- `read_valid`
- `read_ready`
- `read_data`
- `read_resp`

Timing:

- Descriptor fetch consumes read bandwidth.
- Fetch latency may be independent from data-movement read latency.

Wait model:

- Mode: `wait_for_response`
- Requester: this IP
- Waits in: `descriptor_fetch.WAIT_FETCH_RESP`
- Resumes on: `descriptor_read_response`
- Response used for: the returned descriptor is validated and enqueued, and its fields drive read issue

### 4.3 Source Read Interface

Producer: data read process.

Fields:

- `src_addr`
- `read_len`
- `channel_id`
- `tag`
- `read_valid`
- `read_ready`
- `read_data`
- `read_resp`

Timing:

- Multiple source reads may be outstanding.
- Returned data is placed into an internal data buffer.

Wait model:

- Mode: `wait_for_response`
- Requester: this IP
- Waits in: `read_response.WAIT_READ_RESP`
- Resumes on: `source_read_response`
- Response used for: returned data fills the internal buffer that gates write issue
- Note: reads are split: several tags may be outstanding, so the response is awaited in the response process rather than at the issue site

### 4.4 Destination Write Interface

Producer: data write process.

Fields:

- `dst_addr`
- `write_len`
- `channel_id`
- `tag`
- `write_valid`
- `write_ready`
- `write_data`
- `write_resp`

Timing:

- Writes consume data from the internal buffer.
- Write response may arrive after write data acceptance.

Wait model:

- Mode: `wait_for_ack_before_next_request`
- Requester: this IP
- Waits in: `write_response.WAIT_WRITE_RESP`, `completion_update.WAIT_DESCRIPTOR_DONE`
- Resumes on: `write_response_received`
- Outstanding limit: `CH_CFG.outstanding_depth`
- Note: write data acceptance does not block the write issue process; every outstanding write response must be collected before the descriptor completes and the next one starts

### 4.5 Completion/Interrupt Interface

Fields:

- `completion_write_addr`
- `completion_status`
- `irq_valid`
- `irq_vector`

Timing:

- Completion writeback happens after all descriptor bytes are written and all
  write responses are successful.
- Interrupt may be coalesced across descriptors.

Wait model:

- Mode: `wait_for_ack_before_next_request`
- Requester: this IP
- Waits in: `interrupt_coalescing.WAIT_CLEAR`
- Resumes on: `software_clear`
- Outstanding limit: 1
- Note: the interrupt is asserted without blocking the data path; the next coalescing window starts only after software clears the status

## 5. Descriptor Format

Abstract descriptor fields:

- `valid`
- `channel_id`
- `src_addr`
- `dst_addr`
- `length_bytes`
- `control_flags`
- `completion_addr`
- `interrupt_on_completion`
- `next_descriptor`

Descriptor validity conditions:

- `valid` set.
- `length_bytes > 0`.
- Address alignment satisfies configured burst constraints.
- Channel enabled.

## 6. FSMs and Processes

The GDMA model shall be decomposed into the following processes.

### 6.1 Channel Control FSM

Role: Sequential controller for each DMA channel.

States:

- `RESET`: clear channel state.
- `DISABLED`: wait for software enable.
- `IDLE`: enabled but no available descriptor.
- `ACTIVE`: at least one descriptor in progress.
- `DRAIN`: halt requested; finish or cancel in-flight work.
- `HALTED`: channel stopped due to software halt or error.
- `ERROR`: unrecoverable descriptor or bus error.

Sequential dependencies:

- `DISABLED -> IDLE -> ACTIVE`.
- `ACTIVE -> DRAIN -> HALTED` on halt.
- Any state may enter `ERROR` on fatal error.

Parallel interaction:

- While in `ACTIVE`, descriptor fetch, read issue, write issue, response
  tracking, and completion update processes may run in parallel.

### 6.2 Descriptor Fetch FSM

Role: Fetch descriptors from memory and enqueue decoded descriptors.

States:

- `WAIT_DESC`: wait for software head/tail difference.
- `ISSUE_FETCH`: send descriptor read request.
- `WAIT_FETCH_RESP`: wait for descriptor data.
- `VALIDATE`: check descriptor fields.
- `ENQUEUE_DESC`: place descriptor into active descriptor queue.
- `FETCH_STALL`: descriptor queue full or descriptor memory backpressure.

Sequential dependencies:

- Must complete `VALIDATE` before descriptor enters active queue.
- Must update hardware tail after a descriptor is accepted.

Parallel interaction:

- May fetch descriptor N+1 while data movement for descriptor N is active when
  prefetch is enabled and queue space exists.

### 6.3 Channel Scheduler FSM

Role: Select which channel descriptor is allowed to issue work.

States:

- `SCAN_CHANNELS`
- `CHECK_PRIORITY`
- `GRANT_CHANNEL`
- `NO_ELIGIBLE`

Sequential dependencies:

- Grants are consumed by read/write issue processes.

Parallel interaction:

- Runs independently of descriptor fetch.
- May arbitrate across channels every dispatch tick.

### 6.4 Read Issue FSM

Role: Convert active descriptors into source read requests.

States:

- `WAIT_DESCRIPTOR`
- `CHECK_READ_CREDITS`
- `ISSUE_READ`
- `UPDATE_READ_POINTER`
- `READ_STALL`
- `READ_DONE`

Sequential dependencies:

- Read issue for a descriptor starts after descriptor validation.
- Read pointer must advance in order within a descriptor.

Parallel interaction:

- Read issue can run ahead of write issue until internal buffer or outstanding
  read limit is full.
- Multiple read requests may be outstanding.

### 6.5 Read Response FSM

Role: Accept source read data and push into internal buffer.

States:

- `WAIT_READ_RESP`
- `CHECK_RESP_STATUS`
- `WRITE_BUFFER`
- `RESP_ERROR`

Sequential dependencies:

- Read response must be associated with an outstanding read tag.

Parallel interaction:

- Runs in parallel with read issue and write issue.
- Can backpressure read response if internal buffer is full.

### 6.6 Write Issue FSM

Role: Consume internal buffer data and issue destination writes.

States:

- `WAIT_BUFFER_DATA`
- `CHECK_WRITE_CREDITS`
- `ISSUE_WRITE`
- `UPDATE_WRITE_POINTER`
- `WRITE_STALL`
- `WRITE_DONE`

Sequential dependencies:

- Cannot write bytes that have not returned from source read.
- Write completion for a descriptor requires all bytes to be issued.

Parallel interaction:

- Runs in parallel with read issue and read response.
- May lag behind reads due to destination backpressure.

### 6.7 Write Response FSM

Role: Track destination write responses.

States:

- `WAIT_WRITE_RESP`
- `CHECK_RESP_STATUS`
- `MARK_BYTES_COMPLETE`
- `RESP_ERROR`

Sequential dependencies:

- Descriptor is not complete until all write responses are successful.

Parallel interaction:

- Runs in parallel with write issue and completion update.

### 6.8 Completion Update FSM

Role: Write descriptor completion status and update channel counters.

States:

- `WAIT_DESCRIPTOR_DONE`
- `ISSUE_COMPLETION_WRITE`
- `WAIT_COMPLETION_RESP`
- `UPDATE_STATUS`
- `COMPLETE_ERROR`

Sequential dependencies:

- Starts after write issue and write response completion.
- Interrupt generation depends on completion status update.

Parallel interaction:

- May overlap with descriptor fetch for later descriptors.

### 6.9 Interrupt Coalescing FSM

Role: Generate interrupts based on completion count or timer threshold.

States:

- `IDLE`
- `COUNT_COMPLETIONS`
- `WAIT_COALESCE_TIMER`
- `ASSERT_IRQ`
- `WAIT_CLEAR`

Sequential dependencies:

- Interrupt assertion follows completion status publication.

Parallel interaction:

- Runs in parallel with descriptor/data movement processes.

## 7. Process Relationship Summary

Sequential chain for one descriptor:

```text
Descriptor Fetch
  -> Descriptor Validate
  -> Read Issue
  -> Read Response
  -> Write Issue
  -> Write Response
  -> Completion Update
  -> Optional Interrupt
```

Parallel processes during active transfer:

```text
Descriptor Fetch for next descriptor
Read Issue
Read Response
Write Issue
Write Response
Channel Scheduler
Interrupt Coalescing
```

## 8. Resources and Queues

Queues:

- Descriptor prefetch queue.
- Active descriptor queue per channel.
- Read outstanding table.
- Internal data buffer.
- Write outstanding table.
- Completion pending queue.

Resources:

- Descriptor read port.
- Source read port.
- Destination write port.
- Completion write port.
- Per-channel outstanding read credits.
- Per-channel outstanding write credits.
- Internal buffer capacity.

## 9. Performance Model Requirements

The SimPy model shall include:

- One process per FSM listed above.
- Per-channel descriptor queues.
- `simpy.Store` for internal buffers.
- `simpy.Resource` or custom token objects for memory ports.
- Outstanding read/write tag tables.
- Backpressure from descriptor queue full, data buffer full, read/write port
  unavailable, and completion port unavailable.
- Metrics for throughput, descriptor latency, read stalls, write stalls,
  buffer occupancy, outstanding counts, completion latency, and IRQ rate.

## 10. SimPy Functionality Requirements

The generated SimPy delay model shall include enough functionality to validate
the FSM behavior from this DLD without modeling byte payload data. The model
must include:

- Register model.
- Descriptor validation.
- Channel state machine.
- Deterministic scheduling policy.
- Abstract memory request callbacks.
- Completion status generation.
- Error and halt behavior.
- Internal queue/resource backpressure.
- Interrupt coalescing behavior.

The SimPy model shall not model byte payload data unless explicitly configured.

## 11. FSM Timing Model

Clock assumption:

- Core clock frequency: 500 MHz.
- Cycle time: 2 ns.
- All delays below are hard-coded starter values for model generation.

FSM/process count:

- Total FSM/processes: 9.
- Sequential controller: `Channel Control FSM`.
- Parallel active-transfer processes: descriptor fetch, channel scheduler,
  read issue, read response, write issue, write response, completion update,
  and interrupt coalescing.

Per-process timing:

| FSM/process | Runs as | Delay model |
| --- | --- | --- |
| Channel Control FSM | Sequential per-channel owner | Enable to idle: 4 cycles = 8 ns. Idle to active: 4 cycles = 8 ns. Halt drain decision: 8 cycles = 16 ns. |
| Descriptor Fetch FSM | Parallel with data movement when prefetch enabled | Descriptor issue: 5 cycles = 10 ns. Descriptor memory latency: 40 cycles = 80 ns. Validate: 6 cycles = 12 ns. Enqueue: 2 cycles = 4 ns. |
| Channel Scheduler FSM | Parallel global/per-channel scheduler | Channel scan/arbitration: 10 cycles = 20 ns. Grant publish: 2 cycles = 4 ns. |
| Read Issue FSM | Parallel data-movement process | Credit check: 4 cycles = 8 ns. Read request issue: 6 cycles = 12 ns. Pointer update: 2 cycles = 4 ns. |
| Read Response FSM | Parallel response process | Response lookup: 4 cycles = 8 ns. Response status check: 2 cycles = 4 ns. Buffer write: 4 cycles = 8 ns. |
| Write Issue FSM | Parallel data-movement process | Buffer check: 3 cycles = 6 ns. Write request issue: 6 cycles = 12 ns. Pointer update: 2 cycles = 4 ns. |
| Write Response FSM | Parallel response process | Response lookup: 4 cycles = 8 ns. Status check: 2 cycles = 4 ns. Mark bytes complete: 3 cycles = 6 ns. |
| Completion Update FSM | Sequential after descriptor bytes complete | Completion write issue: 8 cycles = 16 ns. Completion response wait: 20 cycles = 40 ns. Status update: 4 cycles = 8 ns. |
| Interrupt Coalescing FSM | Parallel completion observer | Count completion: 2 cycles = 4 ns. Coalesce timer check: 2 cycles = 4 ns. IRQ assert: 4 cycles = 8 ns. |

Single-descriptor minimum delay:

- Descriptor setup path:
  `5 + 40 + 6 + 2 = 53 cycles = 106 ns`.
- One read chunk issue/response path:
  `4 + 6 + 2 + 4 + 2 + 4 = 22 cycles = 44 ns`.
- One write chunk issue/response path:
  `3 + 6 + 2 + 4 + 2 + 3 = 20 cycles = 40 ns`.
- Completion path:
  `8 + 20 + 4 = 32 cycles = 64 ns`.
- IRQ path after completion:
  `2 + 2 + 4 = 8 cycles = 16 ns`.
- Approximate no-stall descriptor latency for one read/write chunk:
  `53 + max(22, 20) + 32 + 8 = 115 cycles = 230 ns`.

Sequential/parallel relationship:

```text
Sequential descriptor path:
  channel active
    -> descriptor fetch/validate
    -> read/write transfer complete
    -> completion update
    -> optional interrupt

Parallel during active transfer:
  descriptor prefetch for N+1
  channel scheduler
  read issue
  read response
  write issue
  write response
  interrupt coalescing
```

## 12. Open Items

- Number of channels.
- Descriptor size and alignment.
- Read/write burst size.
- Maximum outstanding reads/writes.
- Buffer depth.
- Interrupt coalescing thresholds.
- Error recovery policy.
