# Review decisions: `sram_ctrl_ip`

Recorded during the template review that promoted
`templates/sram_ctrl_ip.template.yaml`. The DLD is
[`dlds/sram_ctrl_ip_dld.md`](../dlds/sram_ctrl_ip_dld.md), normalized from the
author's `sram_ctrl_ip_dld.src.md`.

## DLD Open Items, and how each was resolved

**RMW to a masked-off bank — error or stall?** *Not decided in the DLD.*
Resolved as a **conservative default**: rejected as an error at accept time
(`request_accept.DECODE_BANK`, command `RMW.error_conditions:
rmw_targets_masked_off_bank`), rather than left to stall indefinitely in a bank
the scheduler will never service. Carried by `functionality_model.invariants` and
test scenario `rmw_to_masked_bank_rejected_at_accept`.

**Should the scrub be suspended under heavy demand load, or merely
deprioritised?** Resolved by modelling **only what the DLD states**: the per-bank
yield in §4.3 ("the scrub ... never occupies a bank that has a queued request
waiting"), via the `per_bank_queue_non_empty -> ecc_scrub.READ_ENTRY` gating
relationship. No global suspend-under-load policy is implemented — that would be
invented behaviour.

**Queue-full backpressure policy when two requesters are starved at once.**
Resolved as the DLD's own stated fallback, simple round robin. No additional
fairness policy added.

**Should uncorrectable errors raise an interrupt, or be left to software
polling?** Resolved as a **conservative default**: software polling only,
surfaced via `rsp_status: UNCORRECTABLE` and the `uncorrectable_count` counter.
No interrupt line is modelled, because the DLD's Interfaces section defines none.
Carried by test scenario `uncorrectable_error_reported_via_status_only`.

## Ambiguities found in review that the DLD did not list

**Writes never reach `RETURN_DATA`.** §4.2's transition table sends
`ISSUE_ACCESS` straight back to `PICK_BANK` "for writes", with no exception for
the write half of an RMW — so neither produces a `response_if` completion, while
§5 said the requester "is told only that the write was accepted". The model
followed the stated transition graph rather than inventing a response path, and
the conflict was raised for author confirmation.

**Resolved 2026-07-28** (PR #22): the DLD now says it outright — only reads
produce a response, a write is complete once the request port has accepted it,
and the read half of an RMW carries that command's response. The model was
already correct; the document was vague.

**`response_if` has no explicit `Resumes on:` label.** Only
`wait_for_ack_before_next_request` requires one per the extraction rules. Filled
conservatively as "rsp_valid presented with rsp_id matching the original req_id",
taken directly from the interface's stated fields.

**`configuration_and_status_if.outstanding_limit` has no value in the DLD.** Set
to `1`, which is forced rather than chosen: the stated wait mode
(`wait_for_ack_before_next_request`) means the writer waits for the prior ack
before issuing the next write, so at most one can be outstanding by construction.

## Values derived rather than stated

**`rmw_merge_write_data: 3 cycles`** is not stated anywhere in the DLD. It is
arithmetic on values that are: the stated 17-cycle RMW total, minus the stated
11-cycle read path, minus the stated 3-cycle write access. The end-to-end
decomposition in `timing_model.end_to_end_paths.uncontended_rmw` sums to the
stated 17. Recorded here because a reader would otherwise have no way to tell a
derived number from a measured one.

## Normalization review findings

**F1 fixed** (`misplaced_unplaced`, medium) — the source's `8.0 Still open`
section ends with a paragraph stating that Figure 3 must be reissued before the
document is approved. Normalization had filed it under `## Unplaced Source
Content`, which the extractor ignores, so an item the document states as blocking
its own sign-off reached no reader downstream of extraction. Moved into
`## 8. Open Items` as a bullet — both the relocation and the bullet formatting
are shape changes, and it now appears in `reports/sram_ctrl_ip.gaps.md` alongside
the four engineering open items.

Worth noting for the next reviewer: as a trailing paragraph it would not have
been extracted even from the right section, because the extractor reads Open
Items as bullets. Placement alone was not enough.

## Model review findings

The model review that produced M1–M11 was built on `feature/model-review`, which
was dropped at `ef47d2b`. `df8f134` recovered the stage machinery and explicitly
none of the model, template, test or decision changes those eleven findings
produced, so **every one of them regressed** and the findings' own text is gone
with the branch — `ef47d2b` is not reachable in any clone and nothing dangles.
What survives is the summary table in
[`docs/proposals/declared_transition_validation.md`](../docs/proposals/declared_transition_validation.md),
which records M5 and M6 as `scenario_gap` without saying what they claimed.

`tools/check_declared_transitions.py` still reproduces M2, M3, M8, M9 and M11 on
`sram_ctrl_ip` today. Those are transition findings and are not addressed here.

### Three scenario gaps, closed by measurement

`scenario_gap` is defined in `agents/model_review_agent.md` as *a template
`test_scenario` whose stated behaviour the test named for it does not actually
exercise*, which makes it re-derivable without the lost text: compare each of the
nine scenarios against what its test asserts. Three came out, all the same shape
— **a contention property that the scenario's own `input_sequence` makes
unobservable, because only one request is ever in flight.**

| Scenario | Declared behaviour nothing exercised |
| --- | --- |
| `rmw_holds_bank_for_read_merge_and_write` | `no_interleaved_access_to_same_bank_during_rmw` |
| `central_and_bank_queue_full_deasserts_ready_until_drain` | `req_ready_deasserted_while_bank_queue_full` |
| `response_backpressure_holds_bank_in_return_data` | `bank_not_started_on_new_access_while_held` |

Two of these are presumably the regressed M5 and M6. **Which two cannot be
established** — the finding text is unrecoverable, and guessing an id is worse
than not carrying one, so they are recorded here by scenario name instead. The
third was never reported.

The measurements were written rather than the claims removed, and each was
mutation-checked, since a test that passes proves nothing until it is shown to
bite:

**RMW holds its bank across both halves — faithful.** With a READ queued behind
it on the same bank, the RMW's two accesses are still the only touches of bank 2
until it completes, and it still completes in the DLD-quoted **17 cycles** with a
competitor waiting. The old test asserted `bank_utilization[2] == 2` on a
single-request run and a comment claimed the stronger property; a scheduler that
released the bank between the read and write halves would have passed it
unchanged. *Mutation-checked:* charging `rmw_merge_latency` one extra cycle, and
dropping the write half's `_touch_bank`, each fail it.

**`req_ready` deasserts exactly while `request_accept` is in `QUEUE_FULL` —
faithful.** The old test submitted all nine requests at `t=0` before `env.run`,
so the entire burst was consumed inside one uninterrupted run and `req_ready` was
sampled once, at the end, already reasserted. It proved reassertion and the stall
counter; the deassertion the scenario is named for went unmeasured, and a model
that never dropped `req_ready` at all would have passed. Sampling every cycle
shows it low in exactly three windows, one per counted stall, and in no cycle
outside `QUEUE_FULL`. *Mutation-checked:* removing both `req_ready` assignments
fails it.

**A held response blocks the next access on that bank — faithful, and on every
other bank too.** See below.

### Response backpressure stalls every bank, not only the held one

Writing the third measurement surfaced this, and it is the one thing here a
reader must not trust the template on.

`response_backpressure_holds_bank_in_return_data` declares
`bank_not_started_on_new_access_while_held` — *another access on **that** bank*.
The model satisfies that, and more: `bank_scheduler_process` is a single
sequential process, so while it waits in `RETURN_DATA` for `rsp_ready` it
services no bank at all. A `WRITE` to bank 0 queued behind a read held on bank 3
is not touched for the whole duration of the hold and completes only after
`rsp_ready` returns.

So the scenario reads as though the other three banks continue independently
under response backpressure, and they do not. Nothing in the template says
otherwise and nothing catches it: `check_template_coverage.py` compares FSM names
and counts, and the transition checker sees only edges, not the bank a stalled
process is failing to serve. The consequence for anyone reading a model's
numbers: **under sustained response backpressure, `bank_utilization`,
`queue_high_water` and per-request latency for banks other than the held one are
figures for a controller that stops entirely, not one that keeps three banks
running.**

This is recorded, not fixed. Which side is wrong is a judgement about intent —
whether the DLD intends per-bank independence under backpressure or a single
shared return path — and `agents/model_review_agent.md` rule 6 puts that with the
author, not with the agent that found it. The declared property is measured; the
undeclared stronger one is written down.
