---
name: dld-to-template
description: Parse a hardware IP Design-Level Document (DLD) into a reviewed template.yaml — the DLD-to-template extraction, gap-review, and promotion workflow, independent of what happens after. Use when the task mentions IP DLDs, DLD-to-template extraction, gaps reports, TODO_REVIEW markers, or turning a hardware design document into a structured template. Does not cover generating a model from a template — see the simpy-model-generation or systemc-model-generation skill for that.
---

# DLD -> Template

Turn `dlds/<ip>_dld.md` into a reviewed `templates/<ip>.template.yaml` — the
one, standalone job this skill covers. What happens to that template
afterward (a SimPy model, a SystemC model, or nothing yet) is a separate
skill's concern; this one stops at a template that lints clean, covers the
DLD, and has no `TODO_REVIEW` left.

DLDs vary in format and always omit *something*. Never invent missing
behavior — surface it in the gaps report and record how it was resolved.

## Workflow

1. Extract a draft template and a gaps report from the DLD:

   ```shell
   python tools/dld_to_template.py dlds/<ip_name>_dld.md
   ```

   This writes `templates/<ip_name>.template.draft.yaml` and
   `reports/<ip_name>.gaps.md`.

2. Read `reports/<ip_name>.gaps.md`. Replace every `TODO_REVIEW` marker in
   the draft using only behavior the DLD states. Where the DLD leaves a
   detail open (see its "Open Items"), record how you resolved it in
   `decisions/<ip_name>.md` — **not** in the gaps report, which is
   regenerated on every parse and gitignored, so anything written there is
   lost. Resolving it does not always mean choosing a value: an unstated
   number is left unset and made configuration, a behavior the model must
   reach gets the conservative branch clearly labeled, and a behavior whose
   conservative branch would itself be a guess is not modeled at all.
   `decisions/README.md` states the three.
3. Lint and check DLD coverage of the draft:

   ```shell
   python tools/template_lint.py templates/<ip_name>.template.draft.yaml
   python tools/check_template_coverage.py templates/<ip_name>.template.draft.yaml dlds/<ip_name>_dld.md
   ```

4. When both pass and no `TODO_REVIEW` remains, promote the draft to the
   golden template `templates/<ip_name>.template.yaml`, then re-run
   `check_template_coverage.py ... --strict` on the promoted file.

## Normalizing an off-shape DLD first

If the DLD does not carry the section headings and conventions the extractor
reads (no `## N. FSMs` heading, no `## N. Commands` bullets, prose instead of
a timing table, ...), it needs normalization before step 1: reshaping
*structure* only, never adding, removing, or altering an engineering claim.
See `agents/dld_normalization_agent.md` for that contract and
`references/dld_extraction_rules.md`'s "target shape" for what the extractor
actually expects.

## Rules

- Treat the DLD as the sole source of truth. Do not infer behavior it does
  not state, even when the inference seems obviously correct.
- A gap is either a value (leave unset, make it configuration), a forced
  choice (take the conservative branch, label it), or unmodelable (say so,
  do not guess) — never invented silently. `decisions/README.md` has the
  full rule.
- Never write a resolution to `reports/<ip>.gaps.md` — it is regenerated on
  every parse and gitignored. `decisions/<ip_name>.md` is the durable record.
- Do not promote a draft that still contains `TODO_REVIEW`.

Read `references/dld_extraction_rules.md` for the expected DLD conventions,
the exact section headings/bullet shapes the extractor reads, and the
"stated vs inferred" rule in full.
