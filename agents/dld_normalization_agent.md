# DLD Normalization Agent Contract

Use this contract for any human, LLM, or scripted agent that runs the
`normalize_dld` stage. The stage rewrites an author's DLD into the shape
`tools/dld_to_template.py` reads — **changing structure only, and content not at
all.** Like every other stage, it is agent-agnostic: the gate judges the output,
not the author.

Read this alongside
[dld_extraction_rules.md](../skills/ip-model-generation/references/dld_extraction_rules.md),
which defines the target shape, and
[normalize_dld_stage.md](../docs/proposals/normalize_dld_stage.md), which explains
why the stage is upstream of every other gate and replaces none of them.

## Runtime Requirements (any vendor)

Identical to the [model generation contract](ip_model_generation_agent.md): run
non-interactively, read the prompt from stdin, edit files under the repo root,
and exit when finished. Pass/fail is decided by `check_normalization`, not by
your exit code; while it fails you are re-invoked with the failure log, up to
`max_attempts`.

## Inputs

- Author's DLD: `dlds/<ip_name>_dld.src.md` — the original, in whatever shape it
  was written. **Never edit this file.** For a `.docx` DLD it is the pipeline's
  conversion output, regenerated from the Word document, so an edit there is
  overwritten as well as out of contract.
- Target: `dlds/<ip_name>_dld.md` — the file you write.
- Target shape: `skills/ip-model-generation/references/dld_extraction_rules.md`.

## Responsibilities

Produce `dlds/<ip_name>_dld.md` such that every claim in the source is present,
unchanged, and — where a convention exists for it — in the place the extractor
looks.

### Permitted: shape changes

1. Retitle and renumber headings to `### <n.m> <Name> Interface` /
   `### <n.m> <Name> FSM`.
2. Convert a states table or states prose into a `States:` bullet list, under the
   FSM section that source states it belongs to.
3. Convert scattered timing prose into the
   `| FSM/process | Runs as | Delay model |` table, keeping each
   `"<op>: <c> cycles = <n> ns"` phrase intact.
4. Restructure an interface's *stated* blocking behavior into a `Wait model:`
   bullet block.
5. Move a statement to the section where the extractor looks for it.
6. Add `Total FSM/processes: <N>` only when N is derivable by counting FSM
   sections the source itself defines.
7. Collect the source's open questions under `## Open Items`.
8. Add or remove backticks. Markup is shape — the gate compares names, not the
   formatting that carries them.

### Forbidden: content changes

1. Do not add an FSM, state, interface, command, queue, or timing number the
   source does not state.
2. Do not remove or merge any FSM, state, or interface the source states.
3. Do not move a state to a different FSM than the one the source attaches it to.
4. Do not resolve an ambiguity. An ambiguity is an **Open Item**, not a decision.
5. Do not invent a wait model. If the source never says how a requester waits,
   leave the block out: the extractor stamps `assumed_default`, the gaps report
   says so, and `--strict` refuses promotion. That correctly sends the engineer
   back to the DLD.
6. Do not change any number, unit, or name — renaming for style is a content
   change.
7. Do not derive a new number from stated ones. A source stating `4 cycles` and a
   `2 ns` cycle time does not license writing `8 ns`.
8. Do not paraphrase. Preserve sentences and move them; rewording is how content
   gets quietly lost, and the gate measures it.
9. Do not delete content you cannot place — see below.

### The Unplaced section

Anything mapping to no known convention is copied **verbatim** into a trailing
section, so that normalization is never lossy without saying so:

```markdown
## Unplaced Source Content

> Content preserved from <ip>_dld.src.md that did not map to an extractor
> convention. Review and either place it or confirm it is out of scope.

- Owner: performance modeling; Status: draft, circulated for review.
- Rev C note: the completion path was split out of the scheduler block after the
  rev B review.
```

The extractor ignores this section, so it costs nothing downstream. Document
front matter, revision notes, figure captions, and informative asides belong
here. When a table's annotations have no home in the target shape, prefer
carrying them along on the bullet they describe
(``- `size_kb` — transfer size``) — the extractor reads only the first token, so
annotations are free.

## What The Gate Checks

`python tools/check_dld_normalization.py <ip>` is mechanical and has no judgment.
It is worth knowing exactly what it measures, because a few of its rules are
sharper than they first look:

| Check | What trips it |
| --- | --- |
| Measurement conservation | A value+unit pair present on one side and absent on the other — **in both directions**, so retyping one `2 ns` as `3 ns` fails even in a document stating `2 ns` elsewhere. Spelled-out numbers fold to digits (`two cycles` = `2 cycles`), but nothing else is normalized: `5,000,000` must keep its commas, and `1 cycle` and `1 cycles` are different values. Occurrence *counts* are reported, not failed. |
| Identifier conservation | A name present on one side and absent on the other, in any markup. Dotted forms are grounded part-wise, so `accept.ENQUEUE` is legal when the source names `accept` and `ENQUEUE` separately. The three wait-mode names are exempt — their provenance is checked below instead. |
| FSM parity | A disagreement about a recognized FSM's name, the declared count, or **which FSM a state belongs to**. Comparisons are skipped where the source is too unstructured to expose the structure, which is the case the stage exists to fix. |
| Wait-model provenance | A `Wait model:` block in a section whose source counterpart states no blocking behavior at all. |
| Unplaced accounting | A source block that neither survives largely intact in some normalized block nor keeps nearly all its distinctive vocabulary somewhere in the document. Redistributing a paragraph into labelled bullets is fine; rewording it is what fails. |
| Stamp | A missing or stale human review (see below). |

Self-check before reporting done:

```powershell
python tools\check_dld_normalization.py <ip> --no-stamp-check
python tools\dld_to_template.py dlds\<ip>_dld.md   # the extraction it was all for
```

## What The Gate Cannot Check

Conservation proves nothing was dropped, altered, or invented. It does **not**
prove the meaning survived — attaching a stated timing to the wrong FSM preserves
every number. So the fidelity claim is earned per IP by a human, exactly the way
`check_model_provenance.py` earns its baseline:

```powershell
python tools\check_dld_normalization.py --stamp <ip>
```

**Never stamp your own normalization.** The stamp is a human saying "I read the
diff and the meaning survived"; an agent stamping it converts the one
non-automatable gate in the stage into a no-op.

You are not gated on it, so there is nothing to be gained by trying: your loop
runs against `--no-stamp-check`, and the missing stamp is a separate stage that
pauses the run for a human rather than failing it. Reaching that pause with the
mechanical checks clean **is** your stage passing. Downstream, the promotion gate
(`check_template_coverage.py --strict`) refuses an unstamped normalization, which
is the intended outcome and not something to work around.

## Completion Report

Return:

- the shape changes made, by section
- every claim placed under `## Unplaced Source Content`, and why it had no home
- anything the source leaves ambiguous, which is now an Open Item rather than a
  decision you made
- `check_dld_normalization.py` output summary
- whether extraction of the normalized DLD still degrades to `TODO_REVIEW`
  anywhere, and which convention the source is missing if so
