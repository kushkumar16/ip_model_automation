# Storage Pipeline Subsystem Design-Level Document

## 1. Purpose

The Storage Pipeline Subsystem connects two existing IPs into one command
pipeline: the Arbitration IP selects the next command to issue from its
pending tenants, and the Completion IP tracks that command's QoS-accounted
completion. A host submits a command once, to the subsystem; the subsystem's
own glue processes move it from arbitration into completion without the host
having to drive either member IP directly.

This is a composition document, not a re-specification of either member. The
Arbitration IP's own selection policy and the Completion IP's own token
accounting are unchanged and are governed by their own DLDs; this document
states only the glue between them and the subsystem-level interfaces a host
uses instead of the members' own APIs.

## 2. Scope

In scope:

- Composing the Arbitration IP and the Completion IP as subsystem members.
- A single host-facing command intake that forwards into the Arbitration IP.
- Forwarding every command the Arbitration IP issues into the Completion IP
  for QoS-accounted completion.
- A single host-facing QoS configuration path that forwards into the
  Completion IP's per-tenant token budget.
- Monitoring the Completion IP's pending backlog and throttling the
  Arbitration IP's issue readiness when it grows too large, so a slow
  completion path applies backpressure upstream instead of dropping work.

Out of scope:

- Any change to the Arbitration IP's or the Completion IP's own internal
  selection, credit, or token behavior.
- Modeling more than two members; a third member is a different subsystem.

## 2a. Reset

- Core clock frequency: 500 MHz.
- Reset behavior: clear the subsystem's own glue tracking state (the
  dispatch bridge's issued-command cursor and the backpressure monitor's
  throttle flag) and return every glue FSM to `IDLE`/its first sampling
  state. Neither member IP's own reset is triggered by this subsystem
  resetting; each member IP resets independently, per its own DLD.

## 3. Context

Both a storage controller's front-end scheduler and its back-end completion
accounting are already modeled, separately, as the Arbitration IP and the
Completion IP. A real controller wires them together directly: whatever the
scheduler issues is exactly what completion accounting must track next, and a
back-end that falls behind must be able to slow the front-end down. Today a
caller wanting that whole pipeline has to instantiate both models and write
this glue itself, once per caller, with no reviewed reference for how the two
boundaries interact. This subsystem is that reference.

## 4. Interfaces

Every interface below states a `Wait model:` block — how the requester (IP1)
waits on the responder (IP2) across it — the same convention the member IPs'
own DLDs use.

### 4.1 Command Intake Interface

The host's only way to offer a command to the subsystem. The subsystem
forwards it into the Arbitration IP's own ingress queue on the host's behalf.

Fields:

- `cmd_id`: internal command identifier.
- `kind`: READ or WRITE.
- `port_id`, `tenant_id`, `sq_id`: routed straight through to the Arbitration
  IP unchanged.

Timing:

- The bridge accepts one command at a time from its intake queue.
- Acceptance is acknowledged once the command has actually been handed to the
  Arbitration IP's own ingress queue, not merely queued at the subsystem
  boundary.

Wait model:

- Mode: `wait_for_ack_inline`
- Requester: peer
- Waits in: `intake_bridge.ACCEPT_COMMAND`
- Resumes on: `command_forwarded`
- Note: the host is told the command was accepted, not that it has completed;
  completion is observed separately, through the Completion IP's own record.

### 4.2 QoS Configuration Interface

The host's only way to set a tenant's QoS token budget, forwarded into the
Completion IP's own per-tenant configuration.

Fields:

- `tenant_id`: the tenant being configured.
- `token_budget`: applied to all four of the Completion IP's token buckets
  (read, write, read bandwidth, write bandwidth) alike.

Timing:

- Applied synchronously once the bridge reaches the config state; nothing in
  this subsystem defers it further than the Completion IP's own
  `configure_tenant` already does.

Wait model:

- Mode: `wait_for_ack_inline`
- Requester: peer
- Waits in: `intake_bridge.APPLY_QOS_CONFIG`
- Resumes on: `qos_config_applied`

## 5. Commands

- `SUBMIT_COMMAND`: host submits a command for scheduling and completion.
- `CONFIGURE_QOS`: host sets a tenant's QoS token budget.

## 6. FSMs

### 6.1 Intake Bridge FSM

Role: accept a host command or a QoS config request and forward it to the
right member IP.

States:

- `IDLE`: waiting for the next intake item.
- `ACCEPT_COMMAND`: a command was dequeued; forward it to the Arbitration IP.
- `FORWARD_TO_ARBITRATION`: hand the command to `arbitration_ip.enqueue` and
  acknowledge the host.
- `ACCEPT_QOS_CONFIG`: a QoS config request was dequeued.
- `APPLY_QOS_CONFIG`: apply it via `completion_ip.configure_tenant` and
  acknowledge the host.

Transitions:

- `IDLE -> ACCEPT_COMMAND`: next intake item is a command.
- `ACCEPT_COMMAND -> FORWARD_TO_ARBITRATION`: command accepted for forwarding.
- `FORWARD_TO_ARBITRATION -> IDLE`: command handed to the Arbitration IP.
- `IDLE -> ACCEPT_QOS_CONFIG`: next intake item is a QoS config request.
- `ACCEPT_QOS_CONFIG -> APPLY_QOS_CONFIG`: config request accepted for
  forwarding.
- `APPLY_QOS_CONFIG -> IDLE`: config applied to the Completion IP.

### 6.2 Dispatch Bridge FSM

Role: watch the Arbitration IP's own issued-command record and forward every
newly issued command into the Completion IP.

States:

- `POLL_ISSUED`: sample the Arbitration IP's issued-command record for
  entries not yet forwarded.
- `FORWARD_TO_COMPLETION`: hand one newly issued command to
  `completion_ip.submit` and wait for its accept.

Transitions:

- `POLL_ISSUED -> FORWARD_TO_COMPLETION`: at least one issued command has not
  yet been forwarded.
- `FORWARD_TO_COMPLETION -> POLL_ISSUED`: the Completion IP has accepted the
  command.

### 6.3 Backpressure Monitor FSM

Role: sample the Completion IP's pending backlog and throttle the
Arbitration IP's issue readiness when it grows too large.

States:

- `SAMPLE_BACKLOG`: sum the Completion IP's per-tenant pending queue depths.
- `APPLY_THROTTLE`: the throttle state changed; update the Arbitration IP's
  issue readiness to match.

Transitions:

- `SAMPLE_BACKLOG -> APPLY_THROTTLE`: the backlog crossed the configured
  limit in either direction since the last sample.
- `APPLY_THROTTLE -> SAMPLE_BACKLOG`: the Arbitration IP's issue readiness now
  matches the sampled backlog state.
- `SAMPLE_BACKLOG -> SAMPLE_BACKLOG`: the backlog state is unchanged since the
  last sample; nothing to apply.

## 7. FSM Timing Model

Clock assumption:

- Core clock frequency: 500 MHz.
- Cycle time: 2 ns.
- All delays below are hard-coded starter values for model generation, the
  same convention the two member IPs' own DLDs use.

FSM/process count:

- Total FSM/processes: 3.
- All three run in parallel: the Intake Bridge FSM, the Dispatch Bridge FSM,
  and the Backpressure Monitor FSM are independent loops.
- Sequential dependency: a command the Intake Bridge FSM forwards into the
  Arbitration IP is later observed, once issued, by the Dispatch Bridge FSM —
  the two are decoupled through the Arbitration IP's own issued-command
  record, not a direct handoff.

Per-process timing:

| FSM/process | Runs as | Delay model |
| --- | --- | --- |
| Intake Bridge FSM | Parallel process | Accept command: 1 cycle = 2 ns. Forward to arbitration: 1 cycle = 2 ns. Accept QoS config: 1 cycle = 2 ns. Apply QoS config: 1 cycle = 2 ns. |
| Dispatch Bridge FSM | Parallel process | Poll issued: 1 cycle = 2 ns. Forward to completion: 1 cycle = 2 ns. |
| Backpressure Monitor FSM | Parallel process | Sample backlog: 2 cycles = 4 ns. Apply throttle: 1 cycle = 2 ns. |

End-to-end command path:

- Intake to forwarded: `accept command 1 + forward to arbitration 1 = 2 cycles = 4 ns`,
  before the Arbitration IP's own selection timing begins.
- Issued to forwarded into completion: `poll issued 1 + forward to completion 1 = 2 cycles = 4 ns`,
  before the Completion IP's own scheduling timing begins.
- Backlog sample to throttle applied, when the state changes:
  `sample backlog 2 + apply throttle 1 = 3 cycles = 6 ns`.

## 8. Open Items

- The exact backlog threshold that trips the throttle is a subsystem-level
  configuration knob, not a value either member IP's own DLD states; left as
  configuration rather than a guessed number.
- Whether a third member (e.g. a front-end command classifier) ever joins this
  subsystem is out of scope for this document; see Scope.
