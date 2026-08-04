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
