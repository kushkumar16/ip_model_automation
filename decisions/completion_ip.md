# completion_ip — review decisions

## Token-stall retry cadence

**M15 dismissed:** the model retries a token-starved command once per cycle, and
that is the declared cadence, not a divergence from it.

The finding read `qos.dispatch_tick_ms: 1` together with
`timing_model.clock_mhz: 500` and inferred that `WAIT_TOKENS -> SELECT_TENANT`
should be taken once per 500,000 cycles, making the model's 1-cycle retry roughly
half a million times too eager and `token_stalls` inflated by the same factor.

The template answers the question directly and the inference does not survive it:
the `WAIT_TOKENS -> SELECT_TENANT` transition carries `latency_cycles: 1`. That is
the one place the template states a cost for this edge, and 1 is what the model
charges. `dispatch_tick_ms` describes the QoS accounting window, not the
scheduler's poll interval, and the reviewer said as much — it filed this as
interpretive, noting the 500,000 figure was derived from two fields rather than
read from one.

What a reader must not take from this: `token_stalls` counts scheduler polls
while a command cannot pay, not dispatch opportunities missed. It is a measure of
how long starvation lasted, scaled by the retry latency, so it is not comparable
across runs configured with different `retry_latency`. The functional behaviour
the template declares — `insufficient_token_behavior:
command_remains_pending_until_refill` — holds either way: the command stays
pending until a refill funds it.

## Completion output port latency

The port declares `latency_cycles: 1`, and `end_to_end_paths.no_stall_completion`
declares 17 cycles over `[tenant_select, token_check, completion_emit]` — a list
the port is not in. Charging the port inline would make that path 18 and break a
figure the model otherwise matches exactly, so the two declarations cannot both
be satisfied on the critical path.

They can both be satisfied off it. The scheduler holds the port across the emit
and hands the release to a background process that lets the port's own latency
elapse first. The completion is done at the end of the emit, so the 17-cycle path
is unchanged; the port stays busy one cycle longer, which is where a capacity of
1 is supposed to be felt — in contention, not in the latency of the completion
that just left. Both halves are pinned by
`test_completion_output_port_latency_is_charged_off_the_critical_path`, which
fails if the latency is set to 0 and if the port is released inline.

What a reader must not take from this: the port's declared
`one_completion_per_dispatch_tick` bandwidth is still not modelled. At
`dispatch_tick_ms: 1` and 500 MHz that is one completion per 500,000 cycles,
against a measured back-to-back spacing of 17 — a figure no reading of the
17-cycle path can satisfy either. That contradiction is left standing rather than
resolved in the model's favour.

## Reset, and the part of it that is not modelled

`ip.reset.behavior` is `clear_pending_queues_tokens_and_metrics`, and `accept`
now holds `RESET` for a cycle before taking the declared `RESET -> READY`. Two
of the three are cleared there: pending queues and metrics, along with the
window snapshots, the completed list and the eligible-tenant set.

**Token state is not cleared, deliberately.** In this model `tokens` is written
by `configure_tenant` over `qos_config_if`, and callers apply that before the
simulation starts. A runtime reset that zeroed it would discard the
configuration the run was set up with; one that restored it from `base_tokens`
would undo a caller that had deliberately drained a tenant to exercise
starvation. Either way the reset would be overwriting configuration rather than
modelling a reset, and both were tried before this was written down — the first
emptied every budget in the suite, the second silently refilled tenants that
tests had drained on purpose.

What a reader must not take from this: a reset in the model does not return
token budgets to a known state. If a future DLD separates the reset value of a
token budget from the value configuration supplies, that distinction has to
exist in the model before this can be closed.

## qos_config_if's wait point is not modelled

**M33 dismissed:** `configure_tenant` applies its values at the instant it is
called, not at `refill.REFILL_BASE` as `qos_config_if`'s wait model declares.
This is a real gap between the DLD and the model, not a misreading of either —
but closing it literally breaks the interface it is meant to describe.

`refill_process` only runs when a caller opts in (`start_refill_process`
defaults `False`), and `refill.REFILL_BASE` is reached only once per
`refill_window` — 5,000,000 cycles by default. A literal implementation gates
every `configure_tenant` call behind that: in a run that does not start the
refill process, the wait point is never reached at all, and configuration
never takes effect, silently, forever — not delayed, gone. That is true of
every test in the suite but the two refill-specific ones, and of the pattern
every caller in this codebase uses configure_tenant for: set up tenant state
before `env.run()`, so it is in effect from t=0. `submit()` gates its own
effect on its wait point the same way (`accepted_cmd_if`'s `accept.ENQUEUE`)
and that works, because `accept_process` always runs; there is no equivalent
always-on process for `qos_config_if` to wait on.

The comment this finding quotes, attached to `refill_process`'s `REFILL_BASE`
assignment, claimed to describe this: "the configured budgets take effect
here." It does not — it sits next to `_restore_base_tokens()`, the periodic
top-up that re-copies `base_tokens` into `tokens` for tenants already
configured, unrelated to whether a `configure_tenant` call is pending. Fixed:
the comment now says what the code there actually does.

What a reader must not take from this: `configure_tenant` still has no wait
model at all, and a caller cannot observe when a configuration is "applied"
the way `submit()`'s returned event lets one observe an accept. If a future
DLD gives `qos_config_if` its own always-running acknowledger — not tied to
the refill window a caller may never start — that is what would need to exist
before this can be closed for real.
