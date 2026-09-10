# Normalization Review Findings

One file per reviewed IP: `reviews/<ip>.findings.yaml`, holding what a reviewer
believes changed *meaning* when an off-shape DLD was reshaped into the form the
extractor reads. See
[docs/proposals/review_normalization_stage.md](../docs/proposals/review_normalization_stage.md).

These are **tracked**. A finding is a claim about the repo's correctness; it has
to survive until someone resolves it, and it should be visible in the pull
request that does. The gaps report taught that lesson the hard way — durable
content in a regenerated, gitignored file is content that disappears.

## The rule that governs this directory

> **A reviewer may fail a normalization. It may never pass one.**

An empty `findings:` list records that someone looked and reported nothing. That
is a weaker claim than "this normalization is faithful", and it must never be
read as the stronger one. The human normalization stamp
(`check_dld_normalization.py --stamp <ip>`) remains the judgement that the
meaning survived; nothing in this directory can substitute for it.

## Resolving a finding

`python tools/check_review_findings.py` requires every recorded finding to be
either:

- **fixed** — change the normalization. That alters the document hashes, which
  marks the review stale and requires a fresh one; or
- **dismissed** — write a line in `decisions/<ip>.md` naming the finding:

  ```markdown
  - **F1 dismissed:** the scrub reads through the same bank port, so the cost is
    charged where the contention occurs. Confirmed against §4.3.
  ```

Dismissal costs a sentence on the record, on purpose. A finding that can be waved
away silently is a finding nobody has to think about.

## Writing findings by hand

The reviewer stage is not built yet, but the format and its gate are — so a
finding from a *person* is a first-class object today. Record what you would want
the next reviewer to catch:

```yaml
ip: <ip>
source_sha256: <sha256 of dlds/<ip>_dld.src.md>
normalized_sha256: <sha256 of dlds/<ip>_dld.md>
findings:
  - id: F1
    class: timing_attachment
    severity: high
    claim: <what you believe is wrong, in one sentence>
    source: <where the source says otherwise>
    normalized: <where the normalized document says it>
    why: <why the difference matters to the generated model>
```

`class` comes from a closed vocabulary (`timing_attachment`,
`wait_model_attachment`, `state_semantics`, `command_effect`,
`misplaced_unplaced`, `open_item_drift`) — an unknown one fails the gate rather
than being ignored, so there is no "other" bucket for vague observations to
collect in.

## Current contents

**No review findings files remain.** Every IP that had been reviewed has since
been retired, so `check_review_findings.py` currently has nothing to check and
passes by having no work to do. That is not a statement that the surviving models
are faithful — no one has reviewed them. It is the weaker claim still: nobody has
looked.
