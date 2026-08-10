# Review decisions: `timer_ip`

Opened 2026-08-10, during the first model review this IP has ever had. Its
absence until now was itself part of finding M2: the template completion for this
IP chose defaults the DLD does not state and recorded none of them, against this
directory's own rule — *choose a conservative default and record it, never invent
silently*. Eleven of the twelve IPs still have no file here.

The DLD is [`dlds/timer_ip_dld.md`](../dlds/timer_ip_dld.md). There is no author
source for it, so it has never been through normalization or a normalization
review.

## Model review findings

**M1 fixed** (`timing_mismatch`, high) — `debug_freeze_process` waited once per
direction, so freezing and resuming each took half the declared latency.

The DLD settles this without a judgement call. §6.8 states the sequence outright:
*"Freeze request must be acknowledged before counters stop"* and *"Resume request
must be acknowledged before counters restart."* The template reads that
faithfully — four transitions at 2 cycles each, priced as separate
`freeze_request` and `freeze_ack` operations — so reaching `FROZEN` costs 4
cycles and returning to `RUN` costs 4. The model paid one of each pair, which
also made the broadcast and the acknowledgement indistinguishable, since only one
of them was ever waited on.

The **model** was corrected. The existing freeze test could not have caught this:
it asserts that the counter holds while frozen and advances after resuming, and
never asserts what either transition cost — the same shape as M4 and M10 on
`sram_ctrl_ip`, an assertion pointed at the wrong observable.

The new test bounds the cost at `>= 4` and `<= 5` rather than `== 4`. The slack
is not latitude in the declared figure: `debug_freeze` polls on a one-unit loop
instead of waiting on an event, so it can notice a request up to a unit late.
Freezing measures 4 and resuming 5 for that reason alone. **The polling is a
separate defect and is deliberately left alone** — a process that samples a flag
is not the handshake the DLD describes, but fixing it is not what M1 reported.

**M3 fixed** (`fsm_structure`, medium) — the tick now comes from outside.

`tick_if` is declared `direction: input` with `requester: peer`, and the DLD has
the prescaler waiting in `WAIT_RAW_TICK` for `raw_tick_observed`. The model
manufactured ticks inside `prescaler_process` from a constructor argument, so the
declared input interface **had no way in at all** and nothing composing this IP
could supply its clock.

Resolved as the author directed: the tick comes from outside, and the internal
generator becomes configuration rather than a second mechanism.

- `tick_if` is a real store. `present_raw_tick()` is the peer's entry point.
- `prescaler_process` waits on it in `WAIT_RAW_TICK`, which is what the declared
  wait model always said it did.
- `internal_tick_driver_process` presents raw ticks on **the same interface**
  every `tick_period`. One path into the prescaler however the ticks arrive.
- `tick_period=None` runs the IP externally driven, with nothing generating ticks
  on its own.

The default keeps `tick_period` generating ticks, so every existing test is
unchanged. That is a compatibility choice, not a claim that self-clocking is what
the DLD describes.

**Worth recording about how this was found.** Nothing mechanical could have
caught it. `check_wait_model_coverage` passes both before and after, because the
FSM does enter `WAIT_RAW_TICK` — the gate checks that a declared wait point is
reached, not what is being waited *for*. A model can sit in the right state
waiting on the wrong thing and satisfy every gate in this repo.

**Not a finding, on inspection:** M3 also reported `tick_functional()` as an
undeclared second mechanism. It is the repo's functional-model idiom, alongside
`ack_functional` on `interrupt_controller_ip`, `step_functional` on
`completion_ip` and `transact_functional` on `axi_interconnect_ip`. It is kept,
with its docstring now stating plainly that it bypasses the FSM and must not be
mixed with a running driver. That templates declare no such entry point for any
IP is a contract gap across all four, not a `timer_ip` defect.

## Still open

**M2** (`fsm_structure`, high) — the template declares `invalid_channel`,
`invalid_mode` and `watchdog_disabled` error conditions and an `ACCESS_ERROR`
state; the model validates none of them and never enters that state.

The two halves need different answers. `ACCESS_ERROR` **is** DLD-declared (§6.1
register access states) and the DLD's timing table prices it — *"Error response:
2 cycles = 4 ns"* — so the document says an error path exists and what it costs.
The three named error conditions appear nowhere in the DLD; grep finds no
"invalid" in the document at all. They were chosen at template completion and
never recorded, which is why this file did not exist.

So: implementing `ACCESS_ERROR` follows the document, while the three conditions
need either a recorded justification here or removal from the template. Awaiting
the author.
