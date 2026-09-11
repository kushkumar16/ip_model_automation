# arbitration_ip — review decisions

## Issue port latency

`issue_slots` declares `capacity: 1` and `latency_cycles: 1`, while
`end_to_end_paths.new_command_to_issue` declares 38 cycles over eleven named
operations that do not include the port. Both cannot hold on the critical path:
charging the port inline makes that path 39, and the model measures 38 exactly.

They can both hold off it. The issue pipeline takes the slot before the
downstream request, holds it across the request, and hands the release to a
background process that lets the port's own latency elapse first. The command
leaves at the end of the request, so the 38-cycle path is unchanged; the port
stays busy one cycle longer, which is where a capacity of 1 is supposed to be
felt — in contention, not in the latency of the command that just left.

This is the same ruling already applied to `completion_ip`'s
`completion_output_port`, and the implementation mirrors it deliberately so the
two IPs do not drift apart on the same question.

Pinned by `test_arbitration_issue_slot_latency_is_charged_off_the_critical_path`
and `test_arbitration_issue_slots_serialises_competing_requesters`, which fail
if the latency is set to 0, if the slot is released inline, or if the declared
capacity is raised.

What a reader must not take from this: the port's declared
`one_command_per_dispatch_tick` bandwidth is still not modelled, and it
contradicts the template's own `issue_count_rule` and the
`burst_min_issue_count` scenario, which expect several commands per dispatch.
That contradiction is left standing rather than resolved in the model's favour.

## Capacity, and what already modelled it

The declared capacity of 1 was not unmodelled before this change: the depth-1
handoff store between `arbiter_main` and `issue_pipeline` already limited the
arbiter to one selection ahead. What was missing was only the port's latency.
The resource added here is the port itself, and the handoff store remains the
separate thing it always was.

## Sampling of `issue_ready`

**M28 dismissed:** `issue_ready` changes only at transaction boundaries in the
real design, so sampling it before the request and again at the end of it is the
whole of the contract, and the model is correct as written.

The finding measured, accurately, that a deassertion which goes low and returns
high strictly inside the 3-cycle `ISSUE_REQUEST` window produces no
`ISSUE_STALL`, pops the command on schedule, and leaves
`output_backpressure_cycles` at 0. Read literally, the interface note — "while
`issue_ready` is low the pipeline holds in `ISSUE_STALL`" — covers that case.
It is not a case the signal can produce, and "low" there means low at a
boundary.

What a reader must not take from this: `output_backpressure_cycles` counts
cycles spent holding at a boundary, not cycles during which some downstream
signal was momentarily deasserted. The model would need per-cycle sampling
inside `ISSUE_REQUEST` to report the latter, and the declared 3-cycle
`downstream_issue_request` would have to become an interruptible window rather
than an atomic cost — neither of which the design calls for.

A deassertion that *persists* to the end of a request is a different case and is
modelled: see the `ISSUE_REQUEST -> ISSUE_STALL` transition and its retry from
`READ_PENDING_COUNT`.
