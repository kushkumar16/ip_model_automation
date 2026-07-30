# SRAM Controller IP — Design Note

Document ref: SOC-DN-0147
Revision: B
Owner: Memory Subsystem team
Status: issued for review

| Rev | Date | Change |
| --- | --- | --- |
| A | 2026-05-11 | First issue, single bank |
| B | 2026-07-14 | Four banks, ECC scrub added after the rev A review |

## 1.0 Overview

The SRAM Controller IP owns the on-chip buffer SRAM shared by the DMA engines
and the completion path. It accepts read, write and read-modify-write requests
from the fabric, schedules them across four independent banks, and returns data
with ECC checked. It is the block that decides how much of the buffer bandwidth
each requester actually gets, so it is where buffer contention should be
modelled.

This note describes rev B, which adds three banks to the rev A design and
introduces a background scrub process. Bank interleaving is on the low address
bits; the interleave function itself is fixed in hardware and not configurable.

## 2.0 What this note does not cover

The following are explicitly out of scope here and are covered elsewhere:

- The SRAM macro's internal timing closure and physical implementation.
- The ECC code choice (SECDED) and its syndrome encoding — see SOC-DN-0092.
- Power gating of individual banks, which is handled by the power controller.
- Address map assignment, which is a system integration matter.

## 3.0 Ports

### 3.1 Request port

The fabric drives requests into this port. Signals:

| Name | Direction | Meaning |
| --- | --- | --- |
| req_valid | in | request presented |
| req_ready | out | controller can accept this cycle |
| req_id | in | tag returned with the response |
| req_addr | in | byte address |
| req_opcode | in | READ, WRITE or RMW |
| req_bytes | in | transfer length in bytes |
| req_wdata | in | write data |
| req_requester | in | which engine issued it, for accounting |

A request is taken when req_valid and req_ready are both high in the same cycle.
Accepted requests are written into a central request queue with a depth of 16
entries, and are then handed to the per-bank queues, each of which holds 4
entries.

If the central queue is full the controller drops req_ready and the requester
must hold its request until space appears. The requester does not receive any
result at this point — it is waiting only for the controller to take the
request, and it stalls in the accept path's QUEUE_FULL state until an entry
frees up.

### 3.2 Response port

Responses are returned in completion order, which is not necessarily request
order, since banks complete independently. Only reads produce a response: a
write is complete for the requester once the request port has accepted it, and no
response is presented for it. The read half of an RMW produces the response for
that command.

| Name | Direction | Meaning |
| --- | --- | --- |
| rsp_valid | out | response presented |
| rsp_ready | in | requester accepts this cycle |
| rsp_id | out | matching req_id |
| rsp_rdata | out | read data |
| rsp_status | out | OK, CORRECTED or UNCORRECTABLE |

A read requester blocks until its response comes back, because it needs the
returned data before it can proceed; the tag in rsp_id is how it matches the
response to the request it issued. There may be up to 8 requests outstanding
across all banks at once. If rsp_ready is low the controller holds the response
in the bank scheduler's RETURN_DATA state and does not start another access on
that bank.

### 3.3 Configuration and status port

Register writes from software configure:

| Field | Meaning |
| --- | --- |
| scrub_enable | turns the background scrub on |
| scrub_interval_cycles | how often the scrub advances |
| ecc_correct_enable | whether corrections are written back |
| bank_mask | which banks are in service |

Status counters readable by software: corrected_count, uncorrectable_count,
queue_high_water, and per-requester accepted counts. A configuration write is
acknowledged once it has been taken by the scrub process, which applies it at
its next boundary; the writing agent waits for that acknowledgement in
LOAD_CONFIG before issuing another configuration write, and no data comes back
to it.

## 4.0 Internal operation

Three processes run concurrently inside the controller.

### 4.1 Request accept state machine

| State | Meaning |
| --- | --- |
| RESET | queues emptied, counters cleared |
| ACCEPT_IDLE | waiting for req_valid |
| DECODE_BANK | compute target bank from the address |
| PUSH_QUEUE | write the request into the per-bank queue |
| QUEUE_FULL | no room; req_ready deasserted |

Transitions are as follows. RESET goes to ACCEPT_IDLE when reset lifts.
ACCEPT_IDLE goes to DECODE_BANK when a request is presented. DECODE_BANK goes
to PUSH_QUEUE when the target bank queue has room, and to QUEUE_FULL when it
does not. PUSH_QUEUE returns to ACCEPT_IDLE once the entry is written.
QUEUE_FULL returns to ACCEPT_IDLE when an entry drains.

### 4.2 Bank scheduler state machine

One scheduler serves all four banks, visiting them in round robin order. It
skips a bank whose bit is clear in bank_mask.

| State | Meaning |
| --- | --- |
| SCHED_IDLE | no work pending in any bank |
| PICK_BANK | choose the next bank with work |
| ISSUE_ACCESS | drive the access into the SRAM macro |
| WAIT_ECC | data returned, ECC being checked |
| RETURN_DATA | present the response on the response port |
| RMW_MERGE | read-modify-write: merge write data into the read line |

SCHED_IDLE goes to PICK_BANK when any bank queue is non-empty. PICK_BANK goes
to ISSUE_ACCESS once a bank is chosen. ISSUE_ACCESS goes to WAIT_ECC for reads
and for the read half of an RMW, and straight back to PICK_BANK for writes.
WAIT_ECC goes to RMW_MERGE when the request was an RMW, and to RETURN_DATA
otherwise. RMW_MERGE goes to ISSUE_ACCESS to perform the write half.
RETURN_DATA goes back to PICK_BANK when the requester takes the response, and
stays in RETURN_DATA while rsp_ready is low.

### 4.3 ECC scrub state machine

A background process walks the buffer looking for latent single-bit errors.

| State | Meaning |
| --- | --- |
| SCRUB_WAIT | waiting for the next scrub interval |
| LOAD_CONFIG | take any pending configuration change |
| READ_ENTRY | read the next line under scrub |
| CORRECT_ENTRY | write back a corrected line |
| REPORT | update the software-visible counters |

SCRUB_WAIT goes to LOAD_CONFIG when the interval expires. LOAD_CONFIG goes to
READ_ENTRY once configuration is applied. READ_ENTRY goes to CORRECT_ENTRY when
a correctable error is found, and to REPORT otherwise. CORRECT_ENTRY goes to
REPORT once the line is rewritten. REPORT returns to SCRUB_WAIT.

The scrub always yields to demand traffic: it never occupies a bank that has a
queued request waiting.

## 5.0 Transaction types

READ returns data and a status. It occupies the bank for the access and the ECC
check.

WRITE takes data with the request and returns no data; the requester is told
only that the write was accepted, on the request port, and it receives no
response afterwards.

RMW reads the line, merges the new bytes, and writes it back. It holds the bank
for both halves and cannot be interleaved with another access to the same bank
in between.

## 6.0 Timing

The controller runs on the 800 MHz buffer clock, so one cycle is 1.25 ns. All
figures below are the starter values for modelling; they are not silicon
measurements.

| Operation | Cycles |
| --- | --- |
| Accept and decode a request | 2 cycles |
| Push into a bank queue | 1 cycle |
| Queue-full retry | 1 cycle |
| Pick the next bank | 3 cycles |
| SRAM read access | 4 cycles |
| SRAM write access | 3 cycles |
| ECC check | 2 cycles |
| ECC correction writeback | 6 cycles |
| Scrub entry read | 5 cycles |

In nanoseconds, at 1.25 ns per cycle: accept and decode is 2.5 ns, bank pick is
3.75 ns, a read access is 5 ns, a write access is 3.75 ns, and the ECC check is
2.5 ns.

An uncontended read therefore takes 2 + 3 + 4 + 2 = 11 cycles = 13.75 ns from
acceptance to response. A write takes 2 + 3 + 3 = 8 cycles = 10 ns. An RMW pays
the read path, the merge and the write, and is quoted as 17 cycles = 21.25 ns.

The scrub advances once every 4096 cycles by default, which is 5.12 us at this
clock.

Bank conflicts are the dominant queuing effect: two requesters hitting the same
bank serialise behind each other, which is exactly what the model needs to show.

## 7.0 What the performance model must do

The model should be a SimPy delay model with:

- One process for each of the three state machines above.
- A central request queue and four per-bank queues.
- A round-robin bank selection that is deterministic for a given input order.
- Bank occupancy accounting, so utilisation per bank can be reported.
- Latency measured from request acceptance to response acceptance.
- Counters for queue-full stalls, bank conflicts, corrected errors, and
  uncorrectable errors.
- A way to drive the scrub from the test so its interference is observable.

It should not model the SRAM macro internals, the ECC syndrome arithmetic, or
data contents beyond what is needed to tell one response from another.

State that should be visible to a test: bank_queues, outstanding_requests,
corrected_count, uncorrectable_count, and stall_counters.

## 8.0 Still open

- Whether an RMW to a masked-off bank should error or stall — not decided.
- Whether the scrub should be suspended entirely under heavy demand load, or
  merely deprioritised as described above.
- The correct queue-full backpressure policy when two requesters are starved at
  once; rev B leaves this as simple round robin.
- Whether uncorrectable errors should raise an interrupt directly or be left to
  software polling.

Figure 3 in the rev A note (bank interleave diagram) has not been reissued for
four banks and should be regenerated before this document is approved.
