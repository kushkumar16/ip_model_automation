# Review decisions: `gdma_ip`

Opened 2026-09-08, when work began on correcting this IP's model against its DLD.
There was no file here before, which is the norm rather than the exception —
`complete_template`'s prompt tells the agent to record its conservative defaults
in `decisions/<ip>.md`, and only `sram_ctrl_ip` and `timer_ip` have one.

The DLD is [`dlds/gdma_ip_dld.md`](../dlds/gdma_ip_dld.md). There is no author
source for it, so it has never been normalized or normalization-reviewed.

The starting material was `python tools/check_declared_transitions.py gdma_ip`:
nine transitions the model takes that the template does not declare from states
that *do* have declared exits, and five declared transitions no test ever drives.

## Implemented

**`CHECK_READ_CREDITS`** — the state, its cost and its behaviour are all stated,
so it is modelled.

The DLD declares `CHECK_READ_CREDITS` as a `read_issue` state (§6.4), names
*"per-channel outstanding read credits"* as a resource (§8), prices the check at
*"Credit check: 4 cycles = 8 ns"* (§11), and says what it gates: *"Read issue can
run ahead of write issue until internal buffer or outstanding read limit is
full"* (§6.4). `CH_CFG` carries the per-channel outstanding depth (§3).

`read_issue_process` went straight from `WAIT_DESCRIPTOR` to `ISSUE_READ`, so the
state, the cost and the limit were all absent. It now enters
`CHECK_READ_CREDITS`, pays `credit_check_latency`, and waits when the channel is
at its depth; the credit returns in `read_response_process` once the response has
landed, which is the point the read stops being outstanding.

**The depth itself is left unset.** The DLD states no number — `CH_CFG` says it
is configuration — so `configure_channel(..., outstanding_read_depth=N)` sets it
per channel and an unset channel is unbounded. Same reasoning as `timer_ip`'s
`channel_count`: answer the part that can be answered without inventing a figure
the document leaves open.

## Not implemented — not clear

**`CHECK_PRIORITY` and `NO_ELIGIBLE` are not modelled.** `channel_scheduler`
grants in arrival order, straight from `SCAN_CHANNELS` to `GRANT_CHANNEL`.

The DLD names both states (§6.3) and `CH_CFG` carries a per-channel priority
field (§3), but it never says how that field is used. It does not say whether a
higher number is more urgent or less, how ties break, whether arbitration is
strict-priority, weighted, or round-robin among equals, or what makes a channel
ineligible and routes it to `NO_ELIGIBLE`. §6.3 says only that the scheduler
*"may arbitrate across channels every dispatch tick"*.

Every one of those choices changes which descriptor runs first, so none is made.
`configure_channel` still records `priority` so a later implementation has its
input; nothing reads it. The consequence for a reader: **this model's channel
ordering is arrival order, and any measurement that depends on channel priority
is measuring FIFO.**

**Reads now pipeline**, which is what makes the credit limit above mean anything.

`read_issue_process` held `source_read_port` for the entire read and waited for it
before taking the next descriptor, so peak outstanding reads was 1 whether or not
a depth was configured — and the credit check, correct as it was, could never
fire. §6.4 states the opposite twice: *"Multiple read requests may be
outstanding"*, and read issue *"can run ahead of write issue until internal buffer
or outstanding read limit is full"*.

The split the documents already describe is what fixes it, so nothing here is
invented. §11 prices *"Read request issue"* separately from everything after it,
and the template's `source_read_port` resource carries its own `latency_cycles: 8`
— the memory round trip. So the port is held for the issue only, released, and the
round trip runs behind it in a dispatched process while read issue returns for the
next descriptor.

`UPDATE_READ_POINTER` came along with that restructure: it is a declared
`read_issue` state priced at 2 cycles (`read_pointer_update`) that the model did
not have. It sits between the issue and `READ_DONE`.

The depth knob now changes behaviour, which is the test that it is real. Measured
with the tuned latencies the credit test uses (every stage 1 cycle, read 4,
three descriptors) — **not** with the model defaults:

| depth | peak outstanding | credit stalls |
| --- | --- | --- |
| unset | 2 | 0 |
| 1 | 1 | 2 |
| 2 or 4 | 2 | 0 |

Two earlier figures here were wrong and are corrected above: the stall count at
depth 1 was recorded as 7, and the table was attributed to *default* latencies.
It was never measured at defaults.

**At default latencies the credit check cannot bind at all.** Peak outstanding is
1 for every depth setting, because the 53-cycle descriptor fetch is serialized
ahead of the read path and reads never arrive faster than they retire. The
mechanism is real and the knob works, but a reader sweeping outstanding depth on
a default-configured channel will see no change until the fetch path is also
given realistic concurrency.

## Ruling 2026-09-09: `read_latency` set to the declared 6

Raised as a `timing_mismatch` — the model defaulted to 22 where the template
declares `source_read_issue: 6` — and ruled by the user: **make it 6.**

Investigating it explained where every default in this model came from. Each one
is the sum of a single FSM's declared operations in `timing_model`:
`fetch 5+40+6+2 = 53`, `completion 8+20+4 = 32`, `channel_scan 10+2 = 12`,
`irq 2+2+4 = 8`. So 22 was not arbitrary. It is the only default that lumps *two*
FSMs together:

| declared FSM | operations | total |
| --- | --- | --- |
| `read_issue` | credit_check 4 + source_read_issue 6 + pointer 2 | 12 |
| `read_response` | lookup 4 + status 2 + buffer_write 4 | 10 |

`12 + 10 = 22`, charged in one timeout at the issue site — which is precisely
*why* `read_response` pays nothing below. `write_latency = 20` has the identical
shape (`write_issue 3+6+2` plus `write_response 4+2+3`) and is left alone,
because nothing has split the write path yet, so it is still self-consistent.

Once `credit_check` and `read_pointer` became their own timeouts, the model paid
`4 + 22 + 2 = 28` cycles for a path declaring 12 — **6 cycles double-charged.**
Setting `read_latency = 6` makes `read_issue` cost exactly its declared 12.

**The consequence must not be lost:** of the 16 cycles removed, 6 were the
double-charge and **10 were `read_response`'s costs, riding inside the old 22.**
Until the item below is fixed the read path is now 10 cycles *cheaper* than
declared, where before it was 6 cycles dearer. The two are halves of one repair.

No test caught the change, and that is itself a finding: every test that reaches
a read passes an explicit `read_latency`, so the ones left on the default are all
error paths that never issue one. Measured effect at defaults, six descriptors:
last completion moves 418 → 402, the tail read only, because the serialized fetch
dominates.

## Found while pipelining, not fixed

**`read_response` implements none of its declared costs.** §11 gives it *"Response
lookup: 4 cycles. Response status check: 2 cycles. Buffer write: 4 cycles"*, and
`read_response_process` pays none of them — it moves through the states with no
`env.timeout` at all. Same class as `timer_ip`'s M1, and the same reason it went
unnoticed: nothing measures the read response path. Now also the other half of
the ruling above.

**`RESP_ERROR` is assigned and immediately overwritten.**
`read_response_process` sets the state on a full buffer, counts
`buffer_full_stalls`, then falls straight through to `WRITE_BUFFER` on the next
line — so the state never exists for any observer at any simulated time, and no
error handling runs. The backpressure itself is real (the `put` blocks); the
declared error state is cosmetic.
