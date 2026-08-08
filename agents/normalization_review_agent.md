# Normalization Review Agent Contract

Use this contract for any human, LLM, or scripted agent running the
`review_normalization` stage. The task is to read a normalized DLD against the
author's source and report where the meaning changed while the words survived.

## The rule that governs this stage

> **You may fail a normalization. You may never pass one.**

Findings are evidence; silence is not. An empty findings list records that a
reviewer looked and reported nothing — a weaker claim than "this normalization is
faithful", and it must never be read as the stronger one. Nothing you write can
approve a document or substitute for the human stamp.

That asymmetry is what makes it safe to put a sampled judgement next to a
deterministic pipeline: a wrong finding costs a person a few minutes, a missed
one leaves the system exactly where it was, and no output of yours can wave
anything through.

It is also why this reviewer does not need a reviewer. The usual objection to an
LLM judging an LLM — correlated blind spots, the same model missing the same
thing twice — is an argument against trusting a *pass*. It has no force against a
*finding*, which is a specific, checkable claim a person can confirm or dismiss
in seconds.

## Why this stage exists

`check_dld_normalization.py` proves nothing was **dropped, altered, or invented**:
every measurement conserved, every identifier conserved, every source block
accounted for. What it cannot prove is that the surviving claims are *attached to
the right things*.

A normalization can preserve `5 cycles` perfectly and hang it on the wrong FSM.
Every gate in this repo stays green, and the generated model runs with the wrong
delay. That is not hypothetical: moving `Scrub entry read: 5 cycles` from the ECC
Scrub FSM's row to the Bank Scheduler's passes the mechanical gate, and the model
would then charge a scrub cost to every bank access.

Per-FSM state parity closed this class for **states**. Nothing closes it for
timings, wait models, or command effects. That residual risk is what the
normalization stamp absorbs, and today a person absorbs it by reading two
documents side by side with no help.

## Inputs

- Source (**the author's original**): `dlds/<ip>_dld.src.md`
- Normalized: `dlds/<ip>_dld.md`

The template is deliberately **not** an input. A disagreement between the
normalized DLD and the template it produced is a different review's job
(`review_model` reads the template against the model; template promotion is
gated by `check_template_coverage.py`). Read the two documents and nothing else.

## What to report

| Class | The failure |
| --- | --- |
| `timing_attachment` | A stated delay attached to the wrong FSM or operation. The headline case; nothing mechanical catches it. |
| `wait_model_attachment` | A `Wait model:` block on the wrong interface, or a mode that does not match the blocking language it was derived from. |
| `state_semantics` | A state name preserved while the behaviour described for it changed. |
| `command_effect` | A command's effect, error condition, or ordering claim altered in the reshape. |
| `misplaced_unplaced` | Content parked under `## Unplaced Source Content` that had a perfectly good home, which quietly removes it from extraction. |
| `open_item_drift` | A DLD Open Item whose recorded resolution in `decisions/<ip>.md` does not match what the template actually says. |

The vocabulary is closed and `tools/check_review_findings.py` enforces it. An
unknown class fails the file rather than being ignored — there is no "other"
bucket for vague observations to collect in.

## Rules

1. **Report nothing if you find nothing.** An empty list is the expected result
   for a faithful normalization. Do not pad; every weak finding costs the same
   human attention as a real one and spends the credibility of the next.
2. Cite both sides. A finding without a source location and a normalized location
   is not checkable, and will be rejected.
3. Do not report wording, ordering, formatting, or house style. A normalization
   is *supposed* to reshape the document; that is its purpose, not a defect.
4. Detail the source leaves open is not a finding. The normalization is allowed
   to be silent where the author was silent — an unstated value belongs in
   `## Open Items`, and the gaps report, not here.
5. Severity: `high` if a generated model would behave or be timed wrongly,
   `medium` if a reader or a downstream extraction would be misled, `low`
   otherwise.
6. **Do not fix anything.** This stage reads and reports. Fixing here would mean
   the same agent writing and approving a change, which is the loop this exists
   to break.

## Output

Write `reviews/<ip>.normalization.findings.yaml`:

```yaml
ip: <ip>
kind: normalization
source_sha256: <sha256 of dlds/<ip>_dld.src.md>
normalized_sha256: <sha256 of dlds/<ip>_dld.md>
findings:
  - id: F1
    class: timing_attachment
    severity: high
    claim: <one sentence>
    source: <section and row>
    normalized: <section and row>
    why: <what goes wrong because of it>
```

The hashes make the review a statement about two specific documents. Edit either
and the review is stale, and the gate says so rather than letting an old clean
review vouch for text that has changed. Finding ids must be unique across all of
an IP's reviews, because a dismissal names an id and one line must not clear two
different findings — so do not reuse an id already present in
`reviews/<ip>.model.findings.yaml`.

Write long fields as folded block scalars (`>-`). A good finding quotes the
document, and a plain scalar containing a colon breaks the YAML parse.

## What happens next

`tools/check_review_findings.py` requires every finding to be **fixed** — which
changes a document, marks the review stale, and requires a fresh one — or
**dismissed by name** in `decisions/<ip>.md` with a reason. Dismissal costs a
sentence on the record, deliberately: a finding that can be waved away silently
is one nobody has to think about.

Neither is yours to do. You report; a person decides, and that person still signs
the normalization stamp. This stage does not reduce what they are signing. It
gives them a better-informed place to start.
