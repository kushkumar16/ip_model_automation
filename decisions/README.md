# Review Decisions

When a DLD does not state something the template needs, the rule used to be
written as *choose a conservative default and record it, never invent silently*.
That is half a rule, and the half it leaves out is the one this repo reaches for
most: often the right move is to choose nothing at all. **This directory is where
the record lives in every case.**

Which of the three applies depends on what the DLD left open.

**A value it does not state** — a depth, a channel count, a threshold. Do not
pick a number. Make it configuration, leave it unset, and record that it is
unset. There is a way not to choose here, so choosing is inventing. A DLD that
lists *"number of channels"* under Open Items has not given you a channel count;
taking an optional bound that defaults to unset keeps the model honest and still
lets a later document decide.

**A behaviour with no null option** — the model reaches that point and has to do
something, and every branch is a choice. Here, and only here, choose the
conservative branch: label it, record it, and record the evidence that makes it
conservative rather than merely convenient. An invalid register write is one of
these: the model is handed it and must do *something*, and rejecting it at the
access is the branch that cannot silently produce wrong numbers — accepting it
quietly configures a working peripheral of the wrong kind.

**A behaviour where even the conservative branch is a guess** — the DLD names a
state but not the policy that reaches it, or not what happens once it is there.
Do not model it. Record it under *Declared but not implemented* below, with what
the model does instead and what a reader must not trust as a result. A gate the
template attaches to one FSM node while the model enforces it a stage upstream is
this; so is a declared operation the model charges nothing for.

*Never invent silently* survives all three and was never the part in question.
What was in question is that "choose a conservative default", read on its own,
instructs you to invent the number the document declined to state — which is the
thing the pipeline's other rule forbids in as many words (`docs/project_overview.md`:
*"fill in only what the DLD states … don't invent"*). The two lines were in
tension wherever they were restated, and the practice had already settled it.

This directory exists because the record used to live in `reports/<ip>.gaps.md`,
which is the wrong place twice over: `dld_to_template.py` regenerates that file on every
parse, and `reports/` is gitignored. So a reviewer's reasoning — the engineering
judgement that explains why a model behaves the way it does — was erased by the
next extraction and never reached the repository. Everything else in the pipeline
is provenance-stamped and reviewable; this was not.

| File | Role | Tracked |
| --- | --- | --- |
| `reports/<ip>.gaps.md` | What the extractor could not derive. Regenerated every parse, so nothing durable belongs in it. | no |
| `decisions/<ip>.md` | Why each gap was resolved the way it was. Written by hand, never generated. | **yes** |

## What belongs here

- Every DLD **Open Item**, and how it was resolved — the default chosen, or the
  decision not to choose one and what carries the gap instead.
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
trust as a result. The example this convention was written from: a template
declared `debug_freeze` gates `counter.COUNT`, the model enforced the freeze one
stage upstream, and `fsm_state["counter"]` therefore reported `COUNT` throughout a
freeze. Counting did stop; the gate was simply not visible where the contract said
to look.

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

**This directory currently holds no IP files at all.** The IPs whose decisions it
carried have been retired, and the examples above are stated in the abstract for
that reason rather than pointing at documents a reader cannot open. The worked
originals are in git history; the conventions do not depend on them.
