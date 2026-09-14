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
