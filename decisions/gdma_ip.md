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

The depth knob now changes behaviour, which is the test that it is real:

| depth | peak outstanding | credit stalls |
| --- | --- | --- |
| unset | 2 | 0 |
| 1 | 1 | 7 |
| 2 or 4 | 2 | 0 |

Peak settles at 2 under default latencies because the 22-cycle issue is longer
than the 8-cycle round trip, so a third read cannot start before the first
returns. That is arithmetic, not a limit — see below.

## Found while pipelining, not fixed

**`read_latency` defaults to 22 and the template declares `source_read_issue` at
6.** Nothing in §11 or the template's `read_issue` operations produces 22. Since
the issue occupancy now bounds how many reads can overlap, this default alone
holds peak outstanding near 2; at the declared 6 the same configuration would
sustain more. It is a `timing_mismatch` in its own right and wants its own
ruling, not a silent correction folded into a restructure.

**`read_response` implements none of its declared costs.** §11 gives it *"Response
lookup: 4 cycles. Response status check: 2 cycles. Buffer write: 4 cycles"*, and
`read_response_process` pays none of them — it moves through the states with no
`env.timeout` at all. Same class as `timer_ip`'s M1, and the same reason it went
unnoticed: nothing measures the read response path.
