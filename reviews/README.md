# Review Findings

One file per reviewed IP and review kind: `reviews/<ip>.<kind>.findings.yaml`.
Two kinds exist today:

- `model` — `review_model`'s findings, comparing a generated model and its
  tests against the template they were built from. Contract:
  [agents/model_review_agent.md](../agents/model_review_agent.md).
- `normalization` — `review_normalization`'s findings, comparing a normalized
  DLD against its author source for meaning that changed while the words
  survived. Contract:
  [agents/normalization_review_agent.md](../agents/normalization_review_agent.md).
  Design history in
  [docs/proposals/review_normalization_stage.md](../docs/proposals/review_normalization_stage.md).

These are **tracked**. A finding is a claim about the repo's correctness; it has
to survive until someone resolves it, and it should be visible in the pull
request that does. The gaps report taught that lesson the hard way — durable
content in a regenerated, gitignored file is content that disappears.

## The rule that governs this directory

> **A reviewer may fail a review target. It may never pass one.**

An empty `findings:` list records that someone looked and reported nothing. That
is a weaker claim than "this is faithful", and it must never be read as the
stronger one. For a normalization, the human stamp
(`check_dld_normalization.py --stamp <ip>`) remains the judgement that the
meaning survived; nothing in this directory can substitute for it. Neither
review stage is allowed to fix what it finds — that would be the same agent
writing a change and certifying it, which is the loop these stages exist to
break.

## Resolving a finding

`python tools/check_review_findings.py` requires every recorded finding to be
either:

- **fixed** — change the subject file(s) (the model/tests for a `model`
  finding, the normalized DLD for a `normalization` finding). That changes the
  recorded hash, which marks the review stale and requires a fresh one; or
- **dismissed** — write a line in `decisions/<ip>.md` naming the finding:

  ```markdown
  - **M4 dismissed:** the scrub reads through the same bank port, so the cost is
    charged where the contention occurs. Confirmed against §4.3.
  ```

  The line must match `**<id> dismissed:** <reason>` (or `fixed`) exactly, on
  one line, with nothing between the verb and the closing `**` — two ways this
  fails silently: extra words in that gap make the line invisible to the gate,
  and packing two ids into one line's reason resolves the first and swallows
  the second, so give each finding its own line.

Dismissal costs a sentence on the record, on purpose. A finding that can be
waved away silently is a finding nobody has to think about.

## Writing findings by hand

A finding from a *person* is a first-class object, not just an agent's output.
Record what you would want the next reviewer to catch:

```yaml
ip: <ip>
kind: model                          # or: normalization
template_sha256: <sha256 of templates/<ip>.template.yaml>       # model kind
model_sha256: <sha256 of src/ip_model_automation/<ip>.py>       # model kind
tests_sha256: <sha256 of tests/test_<ip>.py>                    # model kind
findings:
  - id: M1
    class: timing_mismatch
    severity: high                   # high | medium | low
    claim: <what you believe is wrong, in one sentence>
    template: <field and value>
    model: <file:line and value>
    why: <what goes wrong because of it>
```

A `normalization` review's schema swaps `template_sha256`/`model_sha256`/
`tests_sha256` for `source_sha256`/`normalized_sha256`, and its `class` comes
from a different closed vocabulary — see the two agent contracts linked above
for each kind's exact fields and classes. Either way, an unknown class fails the
file rather than being ignored: there is no "other" bucket for vague
observations to collect in, and finding ids must stay unique across an IP's
whole review history (of either kind), because a dismissal names an id and one
line must not clear two different findings.

## Current contents

`arbitration_ip.model.findings.yaml` and `completion_ip.model.findings.yaml`
each carry a round-six `review_model` pass, run as an isolated subagent with no
memory of the session that had just finished fixing round five and no access to
`decisions/*.md` or round five's findings — reading the current template/model/
tests for each IP cold. `completion_ip`'s one finding is dismissed (the same
`qos_config_if` wait-model gap as round five's `M33`, re-filed under a fresh id
by a reviewer that could not know it had already been litigated).
`arbitration_ip`'s two findings have since been fixed, which marks that review
stale — a fresh `review_model` pass is owed before
`tools/check_review_findings.py` reports both IPs current.

No `normalization` review has been run against either live IP yet.
