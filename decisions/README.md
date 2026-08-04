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

## What does not

- Anything the DLD states plainly. That is not a decision; it is a transcription.
- Model implementation notes. Those belong in the code.

## Format

One file per IP, `decisions/<ip>.md`. No schema is enforced — this is prose meant
to be read by whoever inherits the model. State the gap, the resolution, and the
evidence that makes it conservative rather than invented, and point at the
template field or test scenario that carries it.

An IP with no decisions file has none recorded. That is only correct if its DLD
left nothing open, which is rare — treat a missing file as a question, not as a
clean bill of health.
