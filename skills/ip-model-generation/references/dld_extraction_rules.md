# DLD Extraction Rules

How `tools/dld_to_template.py` turns a `dlds/<ip>_dld.md` into a draft template,
and how a reviewer/LLM completes it. The draft is a starting point, not the
source of truth: the promoted `templates/<ip>.template.yaml` is.

## Source Priority

1. The DLD is the only source for *authoring* the template.
2. If the DLD does not state a behavior, do **not** invent it. Record it in the
   gaps report and choose a labeled, conservative default.
3. Once promoted, the template — not the DLD — is the source of truth for model
   generation (see `model_generation_rules.md`).

## DLD Conventions The Extractor Expects

The extractor is best-effort and degrades to `TODO_REVIEW` when a convention is
absent. It parses these markdown shapes:

- **IP name** — from the DLD filename (`<ip_name>_dld.md`).
- **Description** — first non-empty line under a `## ... Purpose` heading.
- **Interfaces** — `### <n.m> <Name> Interface[s]` headings; fields from a
  `Fields:` bullet list. Interface names are best-effort (`<name>_if`) and are
  *report-only* in coverage — templates may consolidate interface sections.
- **FSMs** — `### <n.m> <Name> FSM` headings; states from a `States:` bullet
  list; purpose from a `Role:`/`Purpose:` line. The snake-cased FSM name is the
  key that must match the template (`Compare/Event FSM` -> `compare_event`).
- **FSM count** — `Total FSM/processes: <N>` line; becomes `fsm_count`.
- **Timing** — the `| FSM/process | Runs as | Delay model |` table. Each
  `"<op>: <c> cycles = <n> ns"` phrase becomes a timing operation for that FSM.
- **Clock** — first `<N> MHz` and `Cycle time: <N> ns` become `clock_mhz` /
  `cycle_time_ns` (defaults 500 / 2 with a gap note if absent).
- **Open Items** — bullets under an `## ... Open Items` heading are copied into
  the gaps report verbatim.

## Fields Usually Left As TODO_REVIEW

The DLD rarely states these directly; the reviewer fills them from DLD behavior:

- `commands` (name, fields, valid/completion/error conditions)
- `test_scenarios` (author from DLD behavior; must cover every FSM)
- `functionality_model.state_variables / apis / invariants`
- FSM `transitions` details, `queues_used`, `resources_used`
- IP-specific contract fields (e.g. `arbitration_ip` requires `topology`,
  `pending_bitmaps`, burst-availability, and burst metrics — the linter enforces
  these; add them from the DLD).

## Completion Checklist

1. Replace every `TODO_REVIEW` in `templates/<ip>.template.draft.yaml`.
2. `python tools/template_lint.py templates/<ip>.template.draft.yaml` — clean.
3. `python tools/check_template_coverage.py templates/<ip>.template.draft.yaml dlds/<ip>_dld.md --strict`
   — FSM names/count match the DLD and no `TODO_REVIEW` remains.
4. Promote the draft to `templates/<ip>.template.yaml`.
5. `python tools/validate_dld_flow.py` — full front-end + model/test gate.
