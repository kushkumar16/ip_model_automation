# storage_pipeline_subsystem — review decisions

## dispatch_bridge's ack to completion_ip.submit

**M1 fixed:** the template's own `connections` table contradicted its own
`fsm_processes[dispatch_bridge].transitions` table for the same hop. The
connections table declared `ack: completion_event, ack_via:
completion_ip.completed -> dispatch_bridge` — wait for the command to
actually complete. The transitions table declared
`FORWARD_TO_COMPLETION -> POLL_ISSUED` on condition
`completion_ip_has_accepted_the_command`, action `submit_into_completion_ip`
— wait only for acceptance into the pending queue. The DLD agrees with the
transitions table, verbatim: "`FORWARD_TO_COMPLETION -> POLL_ISSUED`: the
Completion IP has accepted the [command]" (`dlds/storage_pipeline_subsystem_dld.md`).

The model was already right — it waits on `completion_ip.submit()`'s
returned accept event, matching both the DLD and the transitions table.
The connections table's summary of the same hop was the one thing wrong,
so it is what changed: `ack: accept_event, ack_via: completion_ip.submit
accept -> dispatch_bridge`. `completion_ip` has no per-command completion
event to wait on in the first place (`completed` is an append-only list,
not an event), so implementing the connections table's original claim
literally would have meant inventing a polling mechanism nothing in the
DLD or the FSM's own transition table calls for.

Re-stamped model provenance against the corrected template
(`check_model_provenance.py --stamp storage_pipeline_subsystem`);
`diff_template.py` reports no change, since the connections table sits
outside the fields it tracks (FSMs, timing, interfaces, commands).

## command_intake_if's ack fired one glue cycle late

**M2 fixed:** `command_intake_if`'s transaction `timing_notes` tie the host's
accept to `enqueue_into_arbitration_ip` -- the action the transitions table
declares on `ACCEPT_COMMAND -> FORWARD_TO_ARBITRATION` itself -- not to any
later transition. The model called `accepted.succeed()` one more full
transition later, after `FORWARD_TO_ARBITRATION -> IDLE`'s own
`acknowledge_host` action, acking the host a cycle later than the contract
states. Filed by an isolated review dispatched after
`modeling_backends: [simpy, systemc]` was added to
`storage_pipeline_subsystem.template.yaml` for the SystemC backend pilot.

Fixed by moving `accepted.succeed()` to immediately after
`arbitration.enqueue(payload)`, still inside the same transition's own
cost -- `FORWARD_TO_ARBITRATION -> IDLE` keeps its own declared 1-cycle
cost and its own transition count, just no longer gates the host's ack.
`test_command_ack_completes_at_enqueue_not_after_the_return_transition`
pins the corrected timing, mutation-verified against the original
one-cycle-late charge.

The review judged `qos_configuration_if`'s analogous path (`APPLY_QOS_CONFIG`'s
own `acknowledge_host` transition) correct as written, using it as the
control that makes the command path's deferral stand out. That read did not
survive a second look: see M3 below.

## qos_configuration_if's ack had the identical bug M2 fixed on the sibling path

**M3 fixed:** the very next review round, dispatched after the M2 fix, found
that `qos_configuration_if`'s ack has the identical extra-cycle-late shape
M2 fixed on `command_intake_if` -- `configure_tenant(...)` is called on
entering `APPLY_QOS_CONFIG` (the `qos_config_applied` event the interface
names, and the point its timing_notes bound the ack against: "no further
than the Completion IP's own `configure_tenant` already defers it"), but
`accepted.succeed()` waited for one more full transition,
`APPLY_QOS_CONFIG -> IDLE`. The earlier review's judgment that this path was
"correct as written" was wrong; M2's own fix should have been carried over
here at the time and was not.

Fixed the same way as M2: `accepted.succeed()` moved to immediately after
`configure_tenant(...)`, with `APPLY_QOS_CONFIG -> IDLE` keeping its own
declared cost and transition count.

**M4 fixed:** the same round noted `test_scenarios[1]`
(`qos_config_flows_to_completion_ip`) declares
`expected_performance_properties: [qos_config_applied_promptly]` but no
test ever asserted anything about when the ack fires -- unlike the command
path's own dedicated ack-timing test, this scenario's test would have
stayed green through the entire M3 bug. Added
`test_qos_config_ack_completes_at_apply_not_after_the_return_transition`,
mutation-verified against the original one-cycle-late charge, closing both
M3 and M4 in the same fix.
