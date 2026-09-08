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

## Found while implementing the credit check, not yet ruled on

**Read issue cannot have more than one read outstanding**, so the credit limit
above can never bind. `read_issue_process` is a single sequential process — it
takes a descriptor, issues the read, waits for it, and loops — so peak
outstanding reads is 1 whether or not a depth is configured.

§6.4 states the opposite twice: *"Multiple read requests may be outstanding"* and
*"Read issue can run ahead of write issue until internal buffer or outstanding
read limit is full"*. Running ahead is the behaviour the credit limit exists to
bound, and the model does not do it.

This was not fixed here because it is a restructure rather than an addition — read
issue would have to dispatch reads it does not wait for, and the buffer and write
paths would then see concurrency they have never seen. The credit check is
correct and stated, but until this is settled it is a mechanism that cannot fire,
which is the same shape as an `ACCESS_ERROR` state nothing can reach.

The test asserts the ceiling and says plainly that the structure imposes it today,
so the assertion becomes load-bearing the moment read issue pipelines rather than
silently passing for the wrong reason.
