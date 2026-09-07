# Review Decisions

When a DLD does not state something the template needs, the rule is *choose a
conservative default and record it, never invent silently*. **This directory is
where that record lives.**

It exists because the record used to live in `reports/<ip>.gaps.md`, which is the
wrong place twice over: `dld_to_template.py` regenerates that file on every
parse, and `reports/` is gitignored. So a reviewer's reasoning — the engineering
judgement that explains why a model behaves the way it does — was erased by the
next extraction and never reached the repository. Everything else in the pipeline
is provenance-stamped and reviewable; this was not.

| File | Role | Tracked |
| --- | --- | --- |
| `reports/<ip>.gaps.md` | What the extractor could not derive. Regenerated every parse, so nothing durable belongs in it. | no |
| `decisions/<ip>.md` | Why each gap was resolved the way it was. Written by hand, never generated. | **yes** |

## What belongs here

- Every DLD **Open Item**, and the default chosen for it.
- Ambiguities found during review that the DLD's own Open Items did not list —
  these are the valuable ones, because they are document defects nobody knew
  about.
- Anything the template states that the DLD does not state directly, including
  values derived by arithmetic from stated ones. If a reader would ask "where did
  that number come from?", the answer belongs here.

## Declared but not implemented

The third thing that belongs here, and the reason it does: **when a template
claims behaviour the model does not have, record it as not implemented with a
reason — do not delete the claim.**

Both documents usually state such a claim faithfully. Removing it makes the
template quietly disagree with its DLD, and nothing catches that:
`check_template_coverage.py` compares FSM names and counts, not timing rows,
gating relationships or command conditions. Removing it from the DLD as well is
worse — it turns a design figure into a description of whatever the model happens
to do, and the requirement is lost rather than deferred.

So the claim stays in both documents and the gap is written down here, under a
heading of its own, saying what the model does instead and what a reader must not
trust as a result. A `timer_ip` example: the template declares `debug_freeze`
gates `counter.COUNT`, the model enforces the freeze one stage upstream, and
`fsm_state["counter"]` therefore reports `COUNT` throughout a freeze. Counting
does stop; the gate is simply not visible where the contract says to look.

Where the gap was raised as a review finding, dismiss it by name in the same
entry so the gate clears:

```markdown
- **M6 dismissed:** <what the model does instead, and the consequence>
```

`check_review_findings.py` matches `**<id> dismissed:**` **exactly**, and the
pattern is unforgiving in two ways that both fail silently:

- Words between the verb and the closing `**` make the line invisible. The gate
  reports the finding as unresolved and says nothing about the malformed
  dismissal sitting above it.
- **One id per line.** The reason is captured greedily to end of line, so
  `**M4 fixed** and **M5 fixed** — …` resolves M4 and swallows M5, which stays
  unresolved with nothing explaining why.

Both of these have already cost a debugging cycle each. When the gate insists a
finding is unresolved and the entry looks right, check the punctuation before
checking the logic.

## What does not

- Anything the DLD states plainly *and the model implements*. That is not a
  decision; it is a transcription.
- Model implementation notes. Those belong in the code.

## Format

One file per IP, `decisions/<ip>.md`. No schema is enforced — this is prose meant
to be read by whoever inherits the model. State the gap, the resolution, and the
evidence that makes it conservative rather than invented, and point at the
template field or test scenario that carries it.

An IP with no decisions file has none recorded. That is only correct if its DLD
left nothing open, which is rare — treat a missing file as a question, not as a
clean bill of health.
