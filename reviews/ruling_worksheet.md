# Ruling worksheet — 15 model findings, 2026-09-10

Every finding in `reviews/*.model.findings.yaml` must be **fixed** or **dismissed
by name** before `check_review_findings.py` passes. This file lays out, for each,
what a fix actually touches and what a dismissal would have to assert. **It makes
no rulings** — that is deliberate, and it is the same reason the reviewer could
not fix what it found.

Nothing here is a recommendation except where it says so explicitly.

## Before you start: two mechanical traps

A dismissal is a line in `decisions/<ip>.md` matching `**M<n> dismissed:**`
**exactly**. Both of these fail silently:

- Words between the verb and the closing `**` make the line invisible. The gate
  reports the finding unresolved and says nothing about the malformed dismissal
  above it.
- **One id per line.** The reason is captured greedily to end of line, so
  `**M4 fixed** and **M5 fixed** — …` resolves M4 and swallows M5.

And a fix changes a subject file, which changes its hash, which marks the review
stale and **owes a fresh review**. That is intended: a finding fixed without
re-review is a claim nobody has checked. Amending a *finding's wording* does not
mark it stale — only template, model and tests are hashed.

## Rule this one first

**`arbitration_ip` M8 — `scan_latency` zeroes three declared latencies.**
It is the common cause beneath M4 and M7, and the reason `TENANT_SCAN`, `SQ_SCAN`
and `GRANT` look unoccupied. At model defaults those states *are* occupied; the
tests make them vanish. Ruling M8 first changes what M4 and M7 even mean.

- **Fix touches:** `arbitration_ip.py:81-85` (stop zeroing what wasn't asked
  about, or rename the parameter to say what it does), then all seven tests in
  `tests/test_arbitration_ip.py`, which currently pass `scan_latency=1`
  everywhere. Expect the timing assertions to move.
- **A dismissal must assert:** that a single knob silently discarding three of
  four declared timing values is acceptable, and that the timing model being
  unexercised in every existing test is a known and accepted gap.

## Group A — the template contradicts itself

The model cannot be judged until the contract is settled. These are design
decisions, not model defects, and each needs a template edit either way.

| id | the contradiction | what settling it decides |
| --- | --- | --- |
| `completion_ip` M3 | `parallel_processes` **and** a `sequential_path` through those FSMs **and** a 21-cycle serial-sum total. All three cannot hold. | Whether 21 is wrong (accept overlaps the scheduler's idle select, so 17 is right), or whether the FSMs were meant to chain and the model's concurrency is wrong. |
| `completion_ip` M6 | `completion_output_port` declared as a resource; no SimPy resource exists. | Whether the port is real contention that must be modelled, or a declaration to remove. |
| `arbitration_ip` M5 | A declared tenant order (`T0,T1,T1,T1,T1,T2,T2,T3`) that the template's own `hierarchy: [port, tenant, sq]` with equal port weights cannot produce. | Whether the scenario or the hierarchy is the mistake. Note M6 below means no test would have caught either. |

- **Fix touches:** the template first, then whichever of model or scenario the
  ruling makes wrong. Two-step by nature.
- **A dismissal must assert:** which of the contradictory claims is
  non-normative, and record it so the next reader does not re-derive the same
  conflict.

## Group B — model disagrees, template plainly right

Fixes, not decisions. Listed with what each touches.

| id | fix touches | a dismissal would have to assert |
| --- | --- | --- |
| `completion_ip` M1 | `completion_ip.py:100-111` — hold `ENQUEUE` across a yield, and give `input_q` a capacity so the declared inline ack can actually block. | That an interface's declared blocking behaviour need not be observable anywhere. |
| `completion_ip` M4 | The `STALL_OUTPUT` branch order in `completion_scheduler` — it is entered from `CHECK_TOKENS` before tokens are checked or debited. | That the declared entry condition for the state is not binding. |
| `arbitration_ip` M1 | `arbitration_ip.py:277-289` — the three scans run unconditionally, so `STALL` is always entered from `SQ_SCAN`, never the declared `PORT_SCAN → STALL`. 18 cycles are charged before a no-port-pending stall is recorded. | That the declared transition is decorative and the extra 18 cycles on an idle arbiter are acceptable. |
| `arbitration_ip` M3 | Adding credit/eligibility state — currently there is none, so `qos_credit_if`'s declared `wait_for_response` samples nothing and the grant ignores credit entirely. | That per-tenant QoS credit is out of scope for this model, and that no configuration can express an ineligible tenant. |
| `arbitration_ip` M4 | `policy_update` — `weighted_order_rebuild` (8 cyc) is charged nowhere; measured span is 3 cycles, not 11. **Rule M8 first**, this may be a symptom. | That the declared 8 cycles are not intended to be charged. |

## Group C — observability only

No number is wrong. What is wrong is that a state, and any trace or coverage
claim naming it, exists at no simulated time. Cheapest group to dismiss — but
read the note below before doing so.

| id | what is unobservable |
| --- | --- |
| `completion_ip` M2 | `refill.ASSESS_USAGE`, `REFILL_BASE`, `PUBLISH_METRICS` — three of four declared `refill` states, assigned back to back in `refill_once`. |
| `completion_ip` M5 | `accept.BACKPRESSURE` is entered on an undeclared condition (tenant not alive) and never on the declared one (queue full). |
| `completion_ip` M7 | `completion_scheduler.SELECT_TENANT` on the completion path. |
| `arbitration_ip` M2 | `arbiter_main.RESET` and `arbiter_main.IDLE`. Amended after filing — see the finding; the original overstated it for two other FSMs. |

- **Fix touches:** a `yield` where the state is meant to hold, or the template's
  state list if the state was never meant to be a distinct step.
- **A dismissal must assert:** that `fsm_state` is not a supported observable for
  these states, and — this is the part that costs something — that any
  `test_scenario` naming them in `fsm_coverage` is claiming coverage no assertion
  could provide. `qos_config_if`'s declared wait point sits on
  `refill.REFILL_BASE`, so dismissing M2 also dismisses that interface's wait
  model being observable.

## Group D — the tests

| id | what is wrong | fix touches |
| --- | --- | --- |
| `arbitration_ip` M6 | `test_arbitration_weighted_tenant_selection_under_port` passes identically at `{T0:1,T1:4}`, `{1,1}`, `{4,1}`, `{1,9}` and `{99,1}`. **Verified by re-running it under all five.** It cannot fail if WRR were plain round-robin. | The test: assert an order that changes with weight. The burst drains `T1` in one grant, so the pointer is never revisited — the stimulus needs more than one round. |
| `arbitration_ip` M7 | Both scenarios declaring latency-applied properties are implemented by tests that override those latencies to 1 — and via `scan_latency`, to 0. **Rule M8 first.** | Either the tests, or the scenarios' declared properties. |

**The one recommendation in this document:** M6 is worth acting on before any
ruling elsewhere. A weighted-round-robin test that passes at inverted weights is
not a weak test, it is an absent one, and it is why M5 went unnoticed. It also
costs nothing to fix — no template decision is involved.

## Not a finding, but on the same ledger

`check_wait_model_coverage.py` reports four declared wait points as assigned but
never observably occupied, and **does not fail on them**. That was left as a
report rather than a hard failure deliberately: they are a standing question, not
a regression, and making a green gate red is a decision. Two of the four are
already covered by `completion_ip` M1 and M2; the arbitration two are artifacts
of M8. Turning the report into a failure is a one-line change once M8 and M1/M2
are ruled.
